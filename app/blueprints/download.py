import io

from flask import Blueprint, current_app, request, send_file

from ..extensions import limiter

from .. import pipeline
from ..settings import ImageSettings
from ..utils import filename_with_extension

download_bp = Blueprint("download", __name__)

_MIME_BY_FORMAT = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


@download_bp.route("/download/<image_id>", methods=["GET"])
@limiter.limit(lambda: current_app.config["RATELIMIT_HEAVY"])
def download_image(image_id):
    store = current_app.session_store
    settings_dict = store.get_settings(image_id)
    settings = ImageSettings.from_dict(settings_dict)
    info = store.get_info(image_id)

    output_bytes, stats, _warnings = pipeline.run_pipeline(store, image_id, settings)

    requested_name = request.args.get("filename")
    base_name = requested_name or info["filename"]
    download_name = filename_with_extension(base_name, stats["format"])

    return send_file(
        io.BytesIO(output_bytes),
        mimetype=_MIME_BY_FORMAT.get(stats["format"], "application/octet-stream"),
        as_attachment=True,
        download_name=download_name,
    )
