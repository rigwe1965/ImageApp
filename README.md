# Image Optimizer

A Flask-based image optimization tool: upload an image once, then adjust quality, format,
sharpening, blur, brightness/contrast/saturation, exposure, grayscale, rotation, flip, resize
(including an interactive drag-to-crop tool), metadata, and background removal/replacement
settings repeatedly — every change is recomputed fresh from the original upload, never from a
previous output, so nothing ever re-compresses an already-compressed result.

## Setup

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## Run (development)

```
python run.py
```

Open http://127.0.0.1:5000/

## Run (production-style, via waitress)

```
waitress-serve --host=127.0.0.1 --port=5000 --call wsgi:create_app
```

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | dev key | Flask secret key |
| `MAX_CONTENT_LENGTH` | 100 MB | Whole-request size cap (Flask, triggers 413) |
| `MAX_SINGLE_FILE_SIZE` | 25 MB | Per-file size cap |
| `MAX_IMAGE_DIMENSION_PIXELS` | 64,000,000 | Reject images with more total pixels than this |
| `MAX_BATCH_FILES` | 30 | Max files per batch upload |
| `IMAGEAPP_TEMP_DIR` | `%TEMP%\imageapp` | Where original images are stored on disk |
| `SESSION_TTL_SECONDS` | 2700 (45 min) | Idle session lifetime before cleanup |
| `CLEANUP_INTERVAL_SECONDS` | 300 (5 min) | How often the cleanup sweep runs |
| `LOG_LEVEL` | INFO | Log verbosity |
| `LOG_DIR` | `logs` | Log file directory |

## Architecture

The uploaded image is stored once as an immutable "master" — a hybrid of an in-memory cache
(fast repeated access) backed by a temp file on disk (durable source of truth, survives cache
eviction). Every settings change, preset application, auto-optimize call, reset, or download
re-runs the full Pillow pipeline (`app/image_processor.py`) starting from that master image —
never from a previously generated output — so results never chain or degrade from repeated
edits. The client mirrors this by always sending the complete settings object on every request,
never a diff.

See `app/image_processor.py` for the fixed transform order (orient → resize → rotate/flip →
grayscale → exposure/brightness/contrast/saturation → blur/sharpen → background composite →
format/encode).

### Background removal / replacement

"Remove Background" runs local foreground segmentation (ISNet `isnet-general-use`, via
`onnxruntime`) on the **original** image — the cutout is computed once per session and cached
(`app/storage.py`, `get_working_bytes`), since it's expensive and deterministic; every other
setting still recomputes freely against that cached cutout without re-running the model.
"Replace with" then lets you leave the cutout transparent, fill it with a solid color, or
composite it onto a separately uploaded background image (`POST /api/background-image/<image_id>`,
cover-fit cropped to match). All processing happens locally — nothing is sent to a third-party
service.

`app/background.py` also refines the model's raw mask before using it, in two steps, to avoid
"ghosting" (other salient objects in the scene — other people, signage, etc. — coming through as
faint semi-transparent shapes instead of being cleanly removed):
1. **Largest-connected-component gating** (`_gate_to_largest_subject`) — a generic saliency model
   often flags more than one object as foreground; this keeps only the single largest connected
   blob (almost always the intended subject) and fully drops everything else, even if the model
   was fairly confident about it.
2. **Confidence thresholding** (`_refine_mask`) — within what's kept, low-confidence edge pixels
   are dropped to transparent and high-confidence pixels are pushed to fully opaque, then lightly
   feathered so edges stay smooth rather than jagged.

The model weights (~170MB, ISNet) download once on first use to
`~/.imageapp/models/isnet-general-use.onnx` and are reused after that. First use requires internet
access; subsequent uses work offline. This app deliberately does not depend on the `rembg` PyPI
package directly — `rembg`'s top-level import unconditionally pulls in `pymatting` → `numba` →
`llvmlite` for an alpha-matting refinement step this app doesn't use, and `llvmlite` ships a native
DLL that can be blocked by an Application Control / WDAC policy on locked-down Windows machines.
Instead, `app/background.py` reimplements ISNet's well-documented pre/post-processing directly
against `onnxruntime`, using the same model weights `rembg` itself would download.

## Manual test checklist

1. Upload a JPEG, PNG, and WebP image individually; confirm previews and stats render.
2. Upload once, then apply several settings changes in sequence (quality → sharpen → brightness →
   format) without re-uploading; confirm each apply reflects only the current full settings state
   (no compounding artifacts).
3. Click Reset; confirm settings and preview return to the initial auto-optimized state.
4. Download the result and confirm the file opens and matches the displayed stats.
5. Upload a PNG with transparency and convert to JPEG; confirm the transparency warning appears
   and the file downloads without error.
6. Switch to Batch mode, upload several images, apply shared settings, and download the ZIP.
7. Try an invalid file (e.g. a renamed `.txt`) and an oversized file; confirm friendly errors.
8. Resize the browser to ~375px width; confirm the settings panel stacks below the preview and
   the batch table remains scrollable.
9. Check "Remove Background (AI)"; confirm the subject is cut out (first run downloads the
   model — see note above). Try each "Replace with" option: Transparent, Solid Color (pick a
   color), and Custom Image (upload a background photo, then remove it and confirm it falls back
   to transparent).
10. Set Resize mode to Crop; drag the box on the Original preview (move + each corner/edge
    handle), type exact values into the X/Y/W/H fields, try an aspect-ratio preset (e.g. 1:1),
    and click "Reset Crop to Full Image". Confirm the Optimized preview always matches the box.

## Known limitations

- Single-process Flask dev server / synchronous batch processing — appropriate for local,
  single-user use; not designed for concurrent multi-user production load.
- No persistence of in-progress settings across a server restart (only the original image
  survives via disk; settings fall back to auto-optimized defaults in that edge case).
- No user accounts — sessions are anonymous, identified only by a server-generated ID.
- Rotation is available in 90° steps (left/right); arbitrary custom-angle rotation is not
  exposed in the UI.
- Background removal uses a general-purpose segmentation model (ISNet) plus largest-component
  gating and confidence thresholding (see above); quality is very good for clear photos with one
  primary subject, including busy scenes with other people/objects in frame, but isn't guaranteed
  for fine detail like loose hair strands, or for photos where the intended subject genuinely
  isn't the largest object in the frame (e.g. a small product on a large table — the gating logic
  would keep the table). No true alpha-matting refinement (blocked by this machine's WDAC policy,
  see above), so semi-transparent subjects (glass, smoke, veils) won't cut out with soft edges.
  The cutout is cached per session but not persisted across a server restart.
- Batch mode's shared-settings panel only exposes format/quality/metadata; background removal and
  custom background images are available in Single Image mode and via the API, but not wired into
  the batch UI, to keep the batch workflow simple.

## Settings

Limits and paths are environment variables with safe defaults: `MAX_SINGLE_FILE_SIZE`, `MAX_BATCH_FILES`, `MAX_TOTAL_SESSION_BYTES`, `MAX_IMAGE_DIMENSION_PIXELS`, `SESSION_TTL_SECONDS`, `CLEANUP_INTERVAL_SECONDS`, `IMAGEAPP_TEMP_DIR`, `LOG_DIR`, `LOG_LEVEL`, and the rate limits `RATELIMIT_ENABLED`, `RATELIMIT_DEFAULT`, `RATELIMIT_UPLOAD`, `RATELIMIT_HEAVY`, `RATELIMIT_STORAGE_URI`. Only the formats JPEG, PNG, WebP, BMP and GIF are accepted as uploads.

## Tests

```
pip install -r requirements-dev.txt
python -m pytest -q
```

CI runs the tests and a dependency vulnerability scan (`pip-audit`) on every pull request, and both must pass before anything merges to `main`.

## Contributing, security and licence

Work on a branch and open a pull request; add user-visible changes to [CHANGELOG.md](CHANGELOG.md). To report a security problem, read [SECURITY.md](SECURITY.md).

All rights reserved. See [LICENSE](LICENSE).
