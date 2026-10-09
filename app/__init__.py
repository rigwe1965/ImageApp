import logging
import os
from logging.handlers import RotatingFileHandler

from flask import Flask

from .config import Config
from .errors import register_error_handlers
from .extensions import limiter
from .storage import SessionStore


def create_app(config_class=Config):
    app = Flask(__name__, static_folder="../static", template_folder="../templates")
    app.config.from_object(config_class)

    _configure_logging(app)

    store = SessionStore(
        temp_dir=app.config["TEMP_DIR"],
        ttl_seconds=app.config["SESSION_TTL_SECONDS"],
        max_total_bytes=app.config["MAX_TOTAL_SESSION_BYTES"],
    )
    app.session_store = store
    store.start_cleanup_thread(app.config["CLEANUP_INTERVAL_SECONDS"])

    limiter.init_app(app)
    register_error_handlers(app)

    from .blueprints.upload import upload_bp
    from .blueprints.process import process_bp
    from .blueprints.download import download_bp
    from .blueprints.batch import batch_bp
    from .blueprints.pages import pages_bp

    app.register_blueprint(pages_bp)
    app.register_blueprint(upload_bp, url_prefix="/api")
    app.register_blueprint(process_bp, url_prefix="/api")
    app.register_blueprint(download_bp, url_prefix="/api")
    app.register_blueprint(batch_bp, url_prefix="/api")

    @app.route("/health")
    def health():
        return {"status": "ok"}

    @app.after_request
    def _set_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy", "default-src 'self'; img-src 'self' blob: data:"
        )
        return response

    return app


def _configure_logging(app: Flask) -> None:
    log_dir = app.config.get("LOG_DIR", "logs")
    os.makedirs(log_dir, exist_ok=True)
    level = getattr(logging, app.config.get("LOG_LEVEL", "INFO"), logging.INFO)

    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "app.log"), maxBytes=2 * 1024 * 1024, backupCount=3
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    file_handler.setLevel(level)

    app.logger.setLevel(level)
    app.logger.addHandler(file_handler)
