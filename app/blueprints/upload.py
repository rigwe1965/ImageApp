from flask import Blueprint, current_app, jsonify, request

from .. import pipeline
from ..auto_optimize import suggest_settings
from ..errors import AppError, ValidationError
from ..extensions import limiter
from ..responses import build_process_result
from ..utils import new_id
from ..validators import validate_and_probe_image, validate_extension, validate_file_size

upload_bp = Blueprint("upload", __name__)


def _handle_single_upload(store, file_storage, max_single_size, allowed_extensions, allowed_formats, max_dim, batch_id=None):
    validate_extension(file_storage.filename, allowed_extensions)
    data = file_storage.read()
    validate_file_size(data, max_single_size)
    info = validate_and_probe_image(data, allowed_formats, max_dim)

    image_id = store.create_session(data, file_storage.filename, batch_id=batch_id, info=info)
    info = store.get_info(image_id)
    settings = suggest_settings(info)
    store.set_initial_settings(image_id, settings.to_dict())

    output_bytes, stats, warnings = pipeline.run_pipeline(store, image_id, settings)
    return build_process_result(store, image_id, settings, output_bytes, stats, warnings)


@upload_bp.route("/upload", methods=["POST"])
@limiter.limit(lambda: current_app.config["RATELIMIT_UPLOAD"])
def upload_single():
    store = current_app.session_store
    if "file" not in request.files:
        raise ValidationError("No file provided. Include a 'file' field in the multipart form.")
    file_storage = request.files["file"]
    if not file_storage or file_storage.filename == "":
        raise ValidationError("No file selected.")

    result = _handle_single_upload(
        store,
        file_storage,
        current_app.config["MAX_SINGLE_FILE_SIZE"],
        current_app.config["ALLOWED_INPUT_EXTENSIONS"],
        current_app.config["ALLOWED_INPUT_FORMATS"],
        current_app.config["MAX_IMAGE_DIMENSION_PIXELS"],
    )
    return jsonify(result), 201


@upload_bp.route("/upload/batch", methods=["POST"])
@limiter.limit(lambda: current_app.config["RATELIMIT_UPLOAD"])
def upload_batch():
    store = current_app.session_store
    files = request.files.getlist("files")
    if not files:
        raise ValidationError("No files provided. Include one or more 'files' fields in the multipart form.")
    max_batch = current_app.config["MAX_BATCH_FILES"]
    if len(files) > max_batch:
        raise ValidationError(f"Too many files in one batch. Maximum is {max_batch}.")

    batch_id = new_id()
    images = []
    for file_storage in files:
        entry = {"filename": file_storage.filename}
        try:
            result = _handle_single_upload(
                store,
                file_storage,
                current_app.config["MAX_SINGLE_FILE_SIZE"],
                current_app.config["ALLOWED_INPUT_EXTENSIONS"],
                current_app.config["ALLOWED_INPUT_FORMATS"],
                current_app.config["MAX_IMAGE_DIMENSION_PIXELS"],
                batch_id=batch_id,
            )
            entry.update({"status": "ok", "image_id": result["image_id"], "original": result["original"],
                          "output": result["output"], "preview_data_url": result["preview_data_url"],
                          "settings": result["settings"], "warnings": result["warnings"]})
        except AppError as exc:
            current_app.logger.warning("Batch upload item failed: %s", exc.message)
            entry.update({"status": "error", "error": {"code": exc.code, "message": exc.message}})
        images.append(entry)

    return jsonify({"batch_id": batch_id, "images": images}), 201
