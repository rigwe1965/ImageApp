from flask import Blueprint, current_app, jsonify, request

from .. import pipeline
from ..presets import list_presets, resolve_preset
from ..auto_optimize import suggest_settings
from ..settings import ImageSettings, SettingsValidationError
from ..errors import ValidationError
from ..extensions import limiter
from ..validators import validate_and_probe_image, validate_extension, validate_file_size

process_bp = Blueprint("process", __name__)


@process_bp.route("/process/<image_id>", methods=["POST"])
@limiter.limit(lambda: current_app.config["RATELIMIT_HEAVY"])
def process_image(image_id):
    store = current_app.session_store
    body = request.get_json(silent=True)
    if body is None:
        raise ValidationError("Request body must be JSON.")
    try:
        settings = ImageSettings.from_dict(body)
    except SettingsValidationError as exc:
        raise ValidationError(str(exc))
    result = pipeline.run_and_persist(store, image_id, settings)
    return jsonify(result)


@process_bp.route("/reset/<image_id>", methods=["POST"])
@limiter.limit(lambda: current_app.config["RATELIMIT_HEAVY"])
def reset_image(image_id):
    store = current_app.session_store
    settings_dict = store.reset_settings(image_id)
    settings = ImageSettings.from_dict(settings_dict)
    result = pipeline.run_and_persist(store, image_id, settings)
    return jsonify(result)


@process_bp.route("/image-info/<image_id>", methods=["GET"])
def image_info(image_id):
    store = current_app.session_store
    info = store.get_info(image_id)
    settings_dict = store.get_settings(image_id)
    return jsonify({"original": info, "settings": settings_dict})


@process_bp.route("/presets", methods=["GET"])
def get_presets():
    return jsonify({"presets": list_presets()})


@process_bp.route("/presets/<preset_id>/apply/<image_id>", methods=["POST"])
@limiter.limit(lambda: current_app.config["RATELIMIT_HEAVY"])
def apply_preset(preset_id, image_id):
    store = current_app.session_store
    info = store.get_info(image_id)
    settings = resolve_preset(preset_id, info["width"], info["height"])
    result = pipeline.run_and_persist(store, image_id, settings)
    return jsonify(result)


@process_bp.route("/auto-optimize/<image_id>", methods=["POST"])
@limiter.limit(lambda: current_app.config["RATELIMIT_HEAVY"])
def auto_optimize_image(image_id):
    store = current_app.session_store
    info = store.get_info(image_id)
    settings = suggest_settings(info)
    result = pipeline.run_and_persist(store, image_id, settings)
    return jsonify(result)


@process_bp.route("/session/<image_id>", methods=["DELETE"])
def delete_session(image_id):
    store = current_app.session_store
    store.delete_session(image_id)
    return jsonify({"deleted": image_id})


@process_bp.route("/background-image/<image_id>", methods=["POST"])
@limiter.limit(lambda: current_app.config["RATELIMIT_UPLOAD"])
def upload_background_image(image_id):
    store = current_app.session_store
    store.get_info(image_id)  # 404s cleanly if the session doesn't exist
    if "file" not in request.files:
        raise ValidationError("No file provided. Include a 'file' field in the multipart form.")
    file_storage = request.files["file"]
    if not file_storage or file_storage.filename == "":
        raise ValidationError("No file selected.")

    validate_extension(file_storage.filename, current_app.config["ALLOWED_INPUT_EXTENSIONS"])
    data = file_storage.read()
    validate_file_size(data, current_app.config["MAX_SINGLE_FILE_SIZE"])
    validate_and_probe_image(data, current_app.config["ALLOWED_INPUT_FORMATS"], current_app.config["MAX_IMAGE_DIMENSION_PIXELS"])

    store.set_background_image(image_id, data, file_storage.filename)
    return jsonify({"image_id": image_id, "background_filename": file_storage.filename}), 201


@process_bp.route("/background-image/<image_id>", methods=["DELETE"])
def delete_background_image(image_id):
    store = current_app.session_store
    store.clear_background_image(image_id)
    return jsonify({"image_id": image_id, "background_cleared": True})
