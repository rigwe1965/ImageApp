import tempfile
import zipfile

from flask import Blueprint, current_app, jsonify, request, send_file

from .. import pipeline
from ..errors import AppError, ValidationError
from ..extensions import limiter
from ..settings import ImageSettings, SettingsValidationError
from ..utils import filename_with_extension

batch_bp = Blueprint("batch", __name__)


@batch_bp.route("/batch/process", methods=["POST"])
@limiter.limit(lambda: current_app.config["RATELIMIT_UPLOAD"])
def batch_process():
    store = current_app.session_store
    body = request.get_json(silent=True) or {}
    batch_id = body.get("batch_id")
    settings_payload = body.get("settings")
    if not batch_id:
        raise ValidationError("'batch_id' is required.")
    if settings_payload is None:
        raise ValidationError("'settings' is required.")
    try:
        settings = ImageSettings.from_dict(settings_payload)
    except SettingsValidationError as exc:
        raise ValidationError(str(exc))

    member_ids = store.get_batch_members(batch_id)
    results = []
    for image_id in member_ids:
        try:
            result = pipeline.run_and_persist(store, image_id, settings)
            result["status"] = "ok"
            results.append(result)
        except AppError as exc:
            current_app.logger.warning("Batch process item %s failed: %s", image_id, exc.message)
            results.append({"image_id": image_id, "status": "error", "error": {"code": exc.code, "message": exc.message}})

    return jsonify({"batch_id": batch_id, "results": results})


@batch_bp.route("/batch/<batch_id>/status", methods=["GET"])
def batch_status(batch_id):
    store = current_app.session_store
    member_ids = store.get_batch_members(batch_id)
    images = []
    for image_id in member_ids:
        try:
            info = store.get_info(image_id)
            settings_dict = store.get_settings(image_id)
            images.append({"image_id": image_id, "status": "ok", "original": info, "settings": settings_dict})
        except AppError as exc:
            images.append({"image_id": image_id, "status": "error", "error": {"code": exc.code, "message": exc.message}})
    return jsonify({"batch_id": batch_id, "images": images})


@batch_bp.route("/batch/download-zip", methods=["POST"])
@limiter.limit(lambda: current_app.config["RATELIMIT_UPLOAD"])
def batch_download_zip():
    store = current_app.session_store
    body = request.get_json(silent=True) or {}
    batch_id = body.get("batch_id")
    if not batch_id:
        raise ValidationError("'batch_id' is required.")

    member_ids = store.get_batch_members(batch_id)
    # Spools to disk past 10MB instead of holding the whole archive resident in memory --
    # up to MAX_BATCH_FILES members at MAX_SINGLE_FILE_SIZE output each could otherwise pin
    # hundreds of MB per request, uncounted by SessionStore's own byte budget.
    zip_buffer = tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024)
    errors = []
    used_names = set()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for image_id in member_ids:
            try:
                info = store.get_info(image_id)
                settings = ImageSettings.from_dict(store.get_settings(image_id))
                output_bytes, stats, _warnings = pipeline.run_pipeline(store, image_id, settings)
                name = filename_with_extension(info["filename"], stats["format"])
                unique_name = name
                counter = 1
                while unique_name in used_names:
                    stem, _, ext = name.rpartition(".")
                    unique_name = f"{stem}_{counter}.{ext}"
                    counter += 1
                used_names.add(unique_name)
                zf.writestr(unique_name, output_bytes)
            except AppError as exc:
                errors.append(f"{image_id}: {exc.message}")

        if errors:
            zf.writestr("errors.txt", "\n".join(errors))

    zip_buffer.seek(0)
    return send_file(zip_buffer, mimetype="application/zip", as_attachment=True, download_name="optimized_images.zip")


@batch_bp.route("/session/batch/<batch_id>", methods=["DELETE"])
def delete_batch(batch_id):
    store = current_app.session_store
    store.delete_batch(batch_id)
    return jsonify({"deleted_batch": batch_id})
