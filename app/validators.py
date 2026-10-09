import os

from .errors import UnsupportedFormatError, ValidationError
from .image_processor import get_original_info


def validate_extension(filename: str, allowed_extensions: set) -> None:
    if not filename or "." not in filename:
        raise ValidationError("File has no extension.")
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext not in allowed_extensions:
        raise UnsupportedFormatError(
            f"File extension '.{ext}' is not supported. Allowed: {', '.join(sorted(allowed_extensions))}."
        )


def validate_file_size(data: bytes, max_bytes: int) -> None:
    if len(data) == 0:
        raise ValidationError("Uploaded file is empty.")
    if len(data) > max_bytes:
        raise ValidationError(
            f"File is too large ({len(data) / (1024*1024):.1f} MB). Maximum allowed is {max_bytes / (1024*1024):.0f} MB."
        )


def validate_and_probe_image(data: bytes, allowed_formats: set, max_dimension_pixels: int) -> dict:
    """Runs the authoritative Pillow-based decode/format check and dimension guard.
    Raises CorruptImageError (via get_original_info) or UnsupportedFormatError/ValidationError."""
    info = get_original_info(data, max_dimension_pixels)
    if info["format"] not in allowed_formats:
        raise UnsupportedFormatError(f"Image format '{info['format']}' is not supported.")
    if info["width"] * info["height"] > max_dimension_pixels:
        raise ValidationError("Image dimensions are too large to process.")
    return info


def safe_join_within(base_dir: str, *parts: str) -> str:
    """Joins path parts under base_dir and rejects any traversal outside it."""
    candidate = os.path.normpath(os.path.join(base_dir, *parts))
    base_norm = os.path.normpath(base_dir)
    # A bare startswith() would let a sibling like "<base_norm>_evil" pass, since it shares
    # the same string prefix without being inside base_dir. commonpath forces a real
    # ancestor relationship instead.
    if os.path.commonpath([candidate, base_norm]) != base_norm:
        raise ValidationError("Invalid path.")
    return candidate
