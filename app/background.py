import hashlib
import io
import os
import threading

import numpy as np
import onnxruntime as ort
from PIL import Image, ImageFilter

from .errors import AppError
from .image_processor import open_and_validate  # no cycle: image_processor never imports background

# ISNet ("isnet-general-use") ONNX weights — rembg's own recommended general-purpose
# model, fetched from rembg's model release (the same file rembg itself downloads).
# Used directly via onnxruntime instead of importing the rembg package, because
# rembg's top-level __init__ unconditionally imports pymatting, which pulls in
# numba/llvmlite. On this machine an Application Control (WDAC) policy blocks
# llvmlite's native DLL, so that import chain cannot be used here. This module
# reimplements just ISNet's known pre/post-processing (resize to 1024x1024,
# mean=0.5/std=1.0 normalize, single forward pass, resize mask back).
#
# ISNet replaced the older, smaller U2Net here specifically because U2Net tends to
# assign partial/low-confidence alpha to OTHER salient-looking objects in a busy
# scene (other people, signage, etc.) instead of isolating just the main subject —
# it "sees" several things as foreground at once. That shows up as faint ghosting
# of background objects instead of a clean cutout. ISNet's higher input resolution
# (1024x1024 vs 320x320) and training data produce a much more confident, more
# separated mask, and the thresholding below removes what low-confidence noise
# still gets through.
_MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx"
_MODEL_SHA256 = "60920e99c45464f2ba57bee2ad08c919a52bbf852739e96947fbb4358c0d964a"
_MODEL_DIR = os.path.join(os.path.expanduser("~"), ".imageapp", "models")
_MODEL_PATH = os.path.join(_MODEL_DIR, "isnet-general-use.onnx")
_INPUT_SIZE = (1024, 1024)
_MEAN = (0.5, 0.5, 0.5)
_STD = (1.0, 1.0, 1.0)

# Mask refinement: pixels at/below LOW_CONFIDENCE are treated as background noise
# (dropped to fully transparent) instead of left as a faint ghost; pixels at/above
# HIGH_CONFIDENCE are treated as certainly foreground (pushed to fully opaque).
# Values in between are linearly stretched across that range, which sharpens the
# separation between the true subject and everything else. A small blur afterward
# re-softens the now-harder edge so it doesn't look aliased/jagged.
_LOW_CONFIDENCE = 40
_HIGH_CONFIDENCE = 200
_EDGE_FEATHER = 2.0

# Low-resolution size used only for connected-component analysis (see
# _largest_component_mask) — cheap enough for a pure-Python flood fill, since the
# actual edge detail comes from the full-resolution mask this only gates.
_COMPONENT_ANALYSIS_SIZE = (128, 128)

_session = None
_session_lock = threading.Lock()

# Bounds concurrent CPU-bound ONNX inference calls. _session_lock only guards one-time session
# construction — without this, N simultaneous remove_background=True requests would all run
# 1024x1024 inference at once with no queueing, degrading latency for all of them instead of
# serializing predictably. Sized to half the available cores, leaving headroom for the rest of
# the Flask process (request handling, Pillow encode/decode) under concurrent load.
_INFERENCE_CONCURRENCY = max(1, (os.cpu_count() or 2) // 2)
_inference_semaphore = threading.Semaphore(_INFERENCE_CONCURRENCY)
# How long a request waits for an inference slot before giving up and returning 503 instead of
# queueing indefinitely behind a burst of concurrent background-removal requests.
_INFERENCE_WAIT_TIMEOUT_SECONDS = 30


class BackgroundRemovalError(AppError):
    status_code = 500
    code = "background_removal_failed"


class BackgroundRemovalBusyError(AppError):
    status_code = 503
    code = "background_removal_busy"


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_model() -> str:
    """Returns a path to model weights whose SHA-256 matches the pinned value. A cached file is
    re-hashed (this runs once per process, since the ONNX session is memoized) so a tampered or
    corrupted file in the user-writable model directory is never handed to onnxruntime."""
    if os.path.exists(_MODEL_PATH):
        if _file_sha256(_MODEL_PATH) == _MODEL_SHA256:
            return _MODEL_PATH
        os.remove(_MODEL_PATH)  # fails verification: discard and fetch a fresh copy below
    os.makedirs(_MODEL_DIR, exist_ok=True)
    try:
        import pooch

        pooch.retrieve(
            _MODEL_URL,
            f"sha256:{_MODEL_SHA256}",
            fname=os.path.basename(_MODEL_PATH),
            path=_MODEL_DIR,
            progressbar=False,
        )
    except Exception as exc:
        raise BackgroundRemovalError(
            "Could not download the background-removal model. Check your internet "
            "connection and try again."
        ) from exc
    return _MODEL_PATH


def _get_session() -> ort.InferenceSession:
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                model_path = _ensure_model()
                try:
                    _session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
                except Exception as exc:
                    raise BackgroundRemovalError(
                        "The background-removal model failed to load. Try deleting "
                        f"'{_MODEL_PATH}' and retrying."
                    ) from exc
    return _session


def _largest_component_mask(binary: np.ndarray) -> np.ndarray:
    """4-connectivity flood fill over a small boolean array; returns a boolean array
    keeping only the largest connected True region. A generic saliency model often
    flags several distinct objects in a busy scene (each internally consistent, so
    thresholding alone can't tell them apart) — this keeps the single largest one,
    which in practice is almost always the intended subject."""
    h, w = binary.shape
    labels = np.zeros((h, w), dtype=np.int32)
    best_label, best_size, current_label = 0, 0, 0

    for start_y in range(h):
        for start_x in range(w):
            if not binary[start_y, start_x] or labels[start_y, start_x] != 0:
                continue
            current_label += 1
            size = 0
            stack = [(start_y, start_x)]
            labels[start_y, start_x] = current_label
            while stack:
                cy, cx = stack.pop()
                size += 1
                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if 0 <= ny < h and 0 <= nx < w and binary[ny, nx] and labels[ny, nx] == 0:
                        labels[ny, nx] = current_label
                        stack.append((ny, nx))
            if size > best_size:
                best_size = size
                best_label = current_label

    if best_label == 0:
        return binary
    return labels == best_label


def _gate_to_largest_subject(mask: Image.Image, target_size) -> Image.Image:
    """Downsamples the mask to analyze connected components cheaply, keeps only the
    largest one (dilated slightly so thin extremities of the subject aren't clipped),
    and uses that as a gate on the full-resolution mask — zeroing out any other
    separate object the model also treated as salient."""
    small = mask.resize(_COMPONENT_ANALYSIS_SIZE, Image.Resampling.LANCZOS)
    binary = np.array(small) > 127
    if not binary.any():
        return mask

    keep = _largest_component_mask(binary)
    keep_img = Image.fromarray((keep * 255).astype("uint8"), mode="L")
    keep_img = keep_img.filter(ImageFilter.MaxFilter(5))  # bridge small gaps/occlusions
    keep_full = keep_img.resize(target_size, Image.Resampling.LANCZOS)

    mask_arr = np.array(mask).astype(np.float32)
    gate_arr = np.array(keep_full).astype(np.float32) / 255.0
    gated = (mask_arr * gate_arr).clip(0, 255).astype("uint8")
    return Image.fromarray(gated, mode="L")


def _refine_mask(mask: Image.Image) -> Image.Image:
    """Sharpens the raw model mask: drops low-confidence pixels to transparent,
    pushes high-confidence pixels to fully opaque, linearly stretches the rest,
    then feathers the result slightly so edges stay smooth rather than jagged."""
    arr = np.array(mask).astype(np.float32)
    span = max(1, _HIGH_CONFIDENCE - _LOW_CONFIDENCE)
    arr = np.clip((arr - _LOW_CONFIDENCE) / span, 0.0, 1.0) * 255.0
    refined = Image.fromarray(arr.astype("uint8"), mode="L")
    if _EDGE_FEATHER > 0:
        refined = refined.filter(ImageFilter.GaussianBlur(_EDGE_FEATHER))
    return refined


def _predict_mask(rgb_img: Image.Image) -> Image.Image:
    session = _get_session()
    resized = rgb_img.resize(_INPUT_SIZE, Image.Resampling.LANCZOS)
    arr = np.array(resized).astype(np.float32)
    arr = arr / max(float(arr.max()), 1e-6)

    normalized = np.zeros_like(arr)
    for i in range(3):
        normalized[:, :, i] = (arr[:, :, i] - _MEAN[i]) / _STD[i]
    tensor = normalized.transpose((2, 0, 1))[None].astype(np.float32)

    input_name = session.get_inputs()[0].name
    if not _inference_semaphore.acquire(timeout=_INFERENCE_WAIT_TIMEOUT_SECONDS):
        raise BackgroundRemovalBusyError(
            "Background removal is busy handling other requests right now. Try again shortly."
        )
    try:
        outputs = session.run(None, {input_name: tensor})
    finally:
        _inference_semaphore.release()
    pred = outputs[0][:, 0, :, :]
    lo, hi = float(pred.min()), float(pred.max())
    pred = (pred - lo) / max(hi - lo, 1e-6)
    pred = np.squeeze(pred)

    mask = Image.fromarray((pred.clip(0, 1) * 255).astype("uint8"), mode="L")
    mask = mask.resize(rgb_img.size, Image.Resampling.LANCZOS)
    mask = _gate_to_largest_subject(mask, rgb_img.size)
    return _refine_mask(mask)


def remove_background_bytes(original_bytes: bytes) -> bytes:
    """Runs ISNet foreground segmentation on the original image and returns PNG
    bytes with the background made transparent. Pure function: same input bytes
    always produce the same output (given the same cached model weights)."""
    img = open_and_validate(original_bytes).convert("RGB")
    mask = _predict_mask(img)
    rgba = img.convert("RGBA")
    rgba.putalpha(mask)

    buf = io.BytesIO()
    rgba.save(buf, format="PNG")
    return buf.getvalue()
