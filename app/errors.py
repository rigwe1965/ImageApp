class AppError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, code: str = None, status_code: int = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code

    def to_dict(self):
        return {"error": {"code": self.code, "message": self.message}}


class ValidationError(AppError):
    status_code = 400
    code = "validation_error"


class CorruptImageError(AppError):
    status_code = 400
    code = "corrupt_image"


class SessionNotFoundError(AppError):
    status_code = 404
    code = "session_not_found"


class UnsupportedFormatError(AppError):
    status_code = 415
    code = "unsupported_format"


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"


def register_error_handlers(app):
    from flask import jsonify

    @app.errorhandler(AppError)
    def handle_app_error(err: AppError):
        app.logger.warning("AppError %s: %s", err.code, err.message)
        return jsonify(err.to_dict()), err.status_code

    @app.errorhandler(413)
    def handle_413(_err):
        return jsonify(PayloadTooLargeError("Upload exceeds the maximum allowed size.").to_dict()), 413

    @app.errorhandler(429)
    def handle_429(_err):
        return jsonify({"error": {"code": "rate_limited", "message": "Too many requests. Please slow down."}}), 429

    @app.errorhandler(404)
    def handle_404(_err):
        return jsonify({"error": {"code": "not_found", "message": "Resource not found."}}), 404

    @app.errorhandler(Exception)
    def handle_unexpected(err: Exception):
        if isinstance(err, AppError):
            raise err
        app.logger.exception("Unhandled exception")
        return jsonify({"error": {"code": "internal_error", "message": "An unexpected error occurred."}}), 500
