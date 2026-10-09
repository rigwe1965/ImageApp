import os
import secrets
import tempfile


class Config:
    # No fallback to a fixed string here on purpose: a hardcoded default becomes a publicly
    # known secret the moment this source is. Nothing in this app currently relies on
    # SECRET_KEY staying stable across restarts (no flask.session/CSRF use it), so a fresh
    # random value per process is safe; set SECRET_KEY in the environment if that ever changes.
    SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", 100 * 1024 * 1024))
    MAX_SINGLE_FILE_SIZE = int(os.environ.get("MAX_SINGLE_FILE_SIZE", 25 * 1024 * 1024))
    MAX_IMAGE_DIMENSION_PIXELS = int(os.environ.get("MAX_IMAGE_DIMENSION_PIXELS", 8000 * 8000))
    MAX_BATCH_FILES = int(os.environ.get("MAX_BATCH_FILES", 30))
    # Total bytes held in-memory across all live sessions at once (independent of the TTL
    # cleanup sweep) — bounds worst-case memory growth from a sustained upload rate within one
    # SESSION_TTL_SECONDS window, which MAX_SINGLE_FILE_SIZE/MAX_BATCH_FILES alone don't cap.
    MAX_TOTAL_SESSION_BYTES = int(os.environ.get("MAX_TOTAL_SESSION_BYTES", 500 * 1024 * 1024))

    TEMP_DIR = os.environ.get("IMAGEAPP_TEMP_DIR", os.path.join(tempfile.gettempdir(), "imageapp"))

    SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", 45 * 60))
    CLEANUP_INTERVAL_SECONDS = int(os.environ.get("CLEANUP_INTERVAL_SECONDS", 5 * 60))

    # Rate limits (Flask-Limiter syntax). Heavy = anything that decodes/re-encodes images.
    RATELIMIT_ENABLED = os.environ.get("RATELIMIT_ENABLED", "1").lower() not in ("0", "false", "no")
    RATELIMIT_DEFAULT = os.environ.get("RATELIMIT_DEFAULT", "300 per minute")
    RATELIMIT_UPLOAD = os.environ.get("RATELIMIT_UPLOAD", "20 per minute")
    RATELIMIT_HEAVY = os.environ.get("RATELIMIT_HEAVY", "60 per minute")
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")

    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
    LOG_DIR = os.environ.get("LOG_DIR", "logs")

    ALLOWED_INPUT_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "bmp", "gif"}
    ALLOWED_INPUT_FORMATS = {"JPEG", "PNG", "WEBP", "BMP", "GIF"}
    ALLOWED_OUTPUT_FORMATS = {"JPEG", "PNG", "WEBP"}
