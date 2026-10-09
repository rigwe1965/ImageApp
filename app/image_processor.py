import io
import math
from typing import Any, Dict, List, Optional, Tuple

import piexif
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, UnidentifiedImageError

from .errors import CorruptImageError, ValidationError
from .settings import ImageSettings

# rotate_degrees follows a clockwise-positive convention (matches "rotate right" UI button);
# Pillow's Image.rotate() is counter-clockwise-positive, hence the sign flip below.


# Upper bound on the pixel count of any image the pipeline produces (resize/rotate output).
# The upload limit (MAX_IMAGE_DIMENSION_PIXELS) only bounds the input; without this, a 500%
# resize or an extreme aspect ratio multiplies a legal input into gigapixels of RAM.
MAX_OUTPUT_PIXELS = 64_000_000


def _check_output_size(width: int, height: int) -> None:
    if width * height > MAX_OUTPUT_PIXELS:
        raise ValidationError("Requested output dimensions are too large.")


def open_and_validate(data: bytes, max_pixels: Optional[int] = None) -> Image.Image:
    """Authoritative content check: Pillow decodes the bytes, not the filename/Content-Type.
    img.verify() invalidates the file object, so the image must be reopened afterward.
    When max_pixels is given, the size declared in the header is checked BEFORE any pixel
    data is decoded, so an oversized image is rejected without allocating its full buffer."""
    try:
        probe = Image.open(io.BytesIO(data))
        if max_pixels is not None and probe.width * probe.height > max_pixels:
            raise ValidationError("Image dimensions are too large to process.")
        probe.verify()
    except ValidationError:
        raise
    except Exception as exc:
        raise CorruptImageError("The uploaded file is not a valid or supported image.") from exc
    try:
        img = Image.open(io.BytesIO(data))
        if max_pixels is not None and img.width * img.height > max_pixels:
            raise ValidationError("Image dimensions are too large to process.")
        img.load()
    except ValidationError:
        raise
    except (UnidentifiedImageError, OSError) as exc:
        raise CorruptImageError("The uploaded file is not a valid or supported image.") from exc
    return img


def _has_alpha(img: Image.Image) -> bool:
    if img.mode in ("RGBA", "LA"):
        return True
    if img.mode == "P" and "transparency" in img.info:
        return True
    return False


def _normalize_format(fmt: Optional[str]) -> str:
    fmt = (fmt or "").upper()
    if fmt == "JPG":
        return "JPEG"
    if fmt in ("JPEG", "PNG", "WEBP"):
        return fmt
    return "PNG"  # safest default for unusual source formats (BMP, GIF) — preserves any transparency


def get_original_info(data: bytes, max_pixels: Optional[int] = None) -> Dict[str, Any]:
    img = open_and_validate(data, max_pixels)
    # Report dimensions as they'll actually be treated everywhere else: browsers auto-orient
    # JPEGs with an EXIF orientation tag when displaying <img>, and apply_settings() does the
    # same by default (auto_orient=True) — so width/height here must match that oriented view,
    # not the raw on-disk pixel grid, or crop coordinates the user draws won't line up.
    oriented = ImageOps.exif_transpose(img)
    return {
        "width": oriented.width,
        "height": oriented.height,
        "format": _normalize_format(img.format),
        "mode": oriented.mode,
        "has_alpha": _has_alpha(oriented),
        "size_bytes": len(data),
    }


def _apply_crop(img: Image.Image, settings: ImageSettings) -> Image.Image:
    w, h = img.size
    x = max(0, min(settings.crop_x, w - 1))
    y = max(0, min(settings.crop_y, h - 1))
    cw = max(1, min(settings.crop_width, w - x))
    ch = max(1, min(settings.crop_height, h - y))
    return img.crop((x, y, x + cw, y + ch))


def _apply_resize(img: Image.Image, settings: ImageSettings) -> Image.Image:
    if settings.resize_mode == "none":
        return img
    if settings.resize_mode == "crop":
        return _apply_crop(img, settings)
    w, h = img.size
    if settings.resize_mode == "percentage":
        scale = settings.resize_percentage / 100.0
        new_w, new_h = max(1, round(w * scale)), max(1, round(h * scale))
        _check_output_size(new_w, new_h)
        return img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    # dimensions mode
    target_w, target_h = settings.resize_width, settings.resize_height
    if settings.maintain_aspect:
        if target_w and target_h:
            ratio = min(target_w / w, target_h / h)
        elif target_w:
            ratio = target_w / w
        elif target_h:
            ratio = target_h / h
        else:
            return img
        new_w, new_h = max(1, round(w * ratio)), max(1, round(h * ratio))
    else:
        new_w, new_h = target_w or w, target_h or h
    _check_output_size(new_w, new_h)
    return img.resize((new_w, new_h), Image.Resampling.LANCZOS)


def _apply_grayscale(img: Image.Image) -> Image.Image:
    has_a = img.mode in ("RGBA", "LA")
    alpha = img.split()[-1] if has_a else None
    base = img.convert("RGB") if img.mode != "L" else img
    gray = ImageOps.grayscale(base)
    if alpha is not None:
        return Image.merge("LA", (gray, alpha))
    return gray


def _hex_to_rgb(hex_color: str) -> tuple:
    h = hex_color.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def _cover_fit(bg_img: Image.Image, target_size: Tuple[int, int]) -> Image.Image:
    """Scales bg_img up to fully cover target_size (aspect preserved), then center-crops
    the excess — the same algorithm as CSS `background-size: cover`."""
    tw, th = target_size
    bw, bh = bg_img.size
    scale = max(tw / bw, th / bh)
    new_w, new_h = max(1, round(bw * scale)), max(1, round(bh * scale))
    resized = bg_img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = max(0, (new_w - tw) // 2)
    top = max(0, (new_h - th) // 2)
    return resized.crop((left, top, left + tw, top + th))


def _apply_exposure(img: Image.Image, exposure: int) -> Image.Image:
    factor = 2 ** (exposure / 100)
    lut = [min(255, round(i * factor)) for i in range(256)]
    if img.mode in ("RGB", "RGBA"):
        bands = list(img.split())
        for i in range(3):
            bands[i] = bands[i].point(lut)
        return Image.merge(img.mode, bands)
    if img.mode == "L":
        return img.point(lut)
    if img.mode == "LA":
        l_band, a_band = img.split()
        return Image.merge("LA", (l_band.point(lut), a_band))
    return img


def apply_settings(
    original_bytes: bytes,
    settings: ImageSettings,
    original_format_hint: Optional[str] = None,
    background_bytes: Optional[bytes] = None,
) -> Tuple[bytes, Dict[str, Any], List[str]]:
    """Pure function: recomputes the full pipeline from original_bytes every call.
    Never feed a previous apply_settings() output back in as original_bytes."""
    warnings: List[str] = []
    img = open_and_validate(original_bytes)
    source_format = _normalize_format(original_format_hint or img.format)
    exif_bytes = img.info.get("exif")

    if settings.auto_orient:
        img = ImageOps.exif_transpose(img)  # also strips the orientation tag from img.info

    if img.mode == "P":
        img = img.convert("RGBA" if _has_alpha(img) else "RGB")
    elif img.mode not in ("RGB", "RGBA", "L", "LA"):
        img = img.convert("RGBA" if _has_alpha(img) else "RGB")

    img = _apply_resize(img, settings)

    if settings.rotate_degrees:
        rad = math.radians(settings.rotate_degrees)
        w, h = img.size
        _check_output_size(
            math.ceil(abs(w * math.cos(rad)) + abs(h * math.sin(rad))),
            math.ceil(abs(w * math.sin(rad)) + abs(h * math.cos(rad))),
        )
        img = img.rotate(-settings.rotate_degrees, expand=True, resample=Image.Resampling.BICUBIC)

    if settings.flip_horizontal:
        img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if settings.flip_vertical:
        img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)

    if settings.grayscale:
        img = _apply_grayscale(img)

    if settings.exposure:
        img = _apply_exposure(img, settings.exposure)
    if settings.brightness != 1.0:
        img = ImageEnhance.Brightness(img).enhance(settings.brightness)
    if settings.contrast != 1.0:
        img = ImageEnhance.Contrast(img).enhance(settings.contrast)
    if settings.saturation != 1.0 and img.mode not in ("L", "LA"):
        img = ImageEnhance.Color(img).enhance(settings.saturation)

    if settings.blur_amount > 0:
        img = img.filter(ImageFilter.GaussianBlur(settings.blur_amount))
    if settings.sharpen_amount > 0:
        img = img.filter(
            ImageFilter.UnsharpMask(radius=settings.sharpen_radius, percent=int(settings.sharpen_amount), threshold=3)
        )

    # Background compositing: applies to ANY image that currently has alpha (whether from
    # background removal or an originally-transparent PNG/WebP upload), not only bg-removed
    # ones — runs after all geometry/color edits so the fill aligns with the final subject.
    if _has_alpha(img) and settings.background_mode != "transparent":
        if settings.background_mode == "color":
            bg = Image.new("RGB", img.size, _hex_to_rgb(settings.background_color))
            rgba = img.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        elif settings.background_mode == "image":
            if background_bytes:
                bg_src = open_and_validate(background_bytes).convert("RGB")
                bg_fitted = _cover_fit(bg_src, img.size)
                rgba = img.convert("RGBA")
                bg_fitted.paste(rgba, (0, 0), mask=rgba.split()[-1])
                img = bg_fitted
            else:
                warnings.append("background_image_missing")

    target_format = settings.format if settings.format != "ORIGINAL" else source_format
    if target_format not in ("JPEG", "PNG", "WEBP"):
        target_format = "JPEG"

    if target_format == "JPEG" and _has_alpha(img):
        background = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        background.paste(rgba, mask=rgba.split()[-1])
        img = background
        warnings.append("transparency_flattened")
    elif target_format == "JPEG" and img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    exif_for_save = None
    if not settings.strip_metadata and exif_bytes and target_format in ("JPEG", "WEBP", "PNG"):
        try:
            exif_dict = piexif.load(exif_bytes)
            exif_dict["0th"][piexif.ImageIFD.Orientation] = 1
            exif_for_save = piexif.dump(exif_dict)
        except Exception:
            exif_for_save = None

    save_kwargs: Dict[str, Any] = {}
    if target_format == "JPEG":
        save_kwargs.update(quality=settings.quality, optimize=True, progressive=settings.progressive)
        if exif_for_save:
            save_kwargs["exif"] = exif_for_save
    elif target_format == "PNG":
        save_kwargs.update(optimize=True)
        if exif_for_save:
            save_kwargs["exif"] = exif_for_save
    elif target_format == "WEBP":
        save_kwargs.update(quality=settings.quality, method=settings.webp_method, lossless=settings.lossless)
        if exif_for_save:
            save_kwargs["exif"] = exif_for_save

    buffer = io.BytesIO()
    img.save(buffer, format=target_format, **save_kwargs)
    output_bytes = buffer.getvalue()

    original_size = len(original_bytes)
    stats = {
        "width": img.width,
        "height": img.height,
        "format": target_format,
        "output_size_bytes": len(output_bytes),
        "original_size_bytes": original_size,
        "reduction_percent": round((1 - len(output_bytes) / original_size) * 100, 1) if original_size else 0.0,
    }
    return output_bytes, stats, warnings
