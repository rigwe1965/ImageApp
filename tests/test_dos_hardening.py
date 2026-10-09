import hashlib
import io

import pytest
from PIL import Image

from app import background, image_processor
from app.errors import ValidationError
from app.settings import ImageSettings
from app.storage import SessionStore
from tests.conftest import make_image_bytes


def _settings(**kw):
    return ImageSettings.from_dict(kw)


def test_percentage_resize_over_output_cap_rejected(monkeypatch):
    monkeypatch.setattr(image_processor, "MAX_OUTPUT_PIXELS", 10_000)
    data = make_image_bytes(100, 100)  # 10,000 px -> 200% would be 40,000
    with pytest.raises(ValidationError):
        image_processor.apply_settings(data, _settings(resize_mode="percentage", resize_percentage=200))


def test_extreme_aspect_ratio_dimensions_resize_rejected(monkeypatch):
    monkeypatch.setattr(image_processor, "MAX_OUTPUT_PIXELS", 100_000)
    data = make_image_bytes(10, 1000)  # width 8000 with aspect kept => ratio 800
    with pytest.raises(ValidationError):
        image_processor.apply_settings(data, _settings(resize_mode="dimensions", resize_width=8000))


def test_rotation_expansion_over_cap_rejected(monkeypatch):
    monkeypatch.setattr(image_processor, "MAX_OUTPUT_PIXELS", 10_000)
    data = make_image_bytes(100, 100)  # 45deg expand => ~141x141 = 20k
    with pytest.raises(ValidationError):
        image_processor.apply_settings(data, _settings(rotate_degrees=45))


def test_resize_within_cap_still_works():
    out, stats, _ = image_processor.apply_settings(
        make_image_bytes(100, 100), _settings(resize_mode="percentage", resize_percentage=50)
    )
    assert (stats["width"], stats["height"]) == (50, 50)


def test_oversized_image_rejected_before_decode(monkeypatch):
    data = make_image_bytes(200, 200)

    def boom(self, *a, **k):
        raise AssertionError("pixels were decoded before the size check")

    monkeypatch.setattr(Image.Image, "load", boom)
    with pytest.raises(ValidationError):
        image_processor.open_and_validate(data, max_pixels=1000)


def test_cached_buffers_count_against_budget(temp_store_dir):
    data = make_image_bytes(50, 50)
    store = SessionStore(temp_store_dir, 600, max_total_bytes=len(data) + 100)
    image_id = store.create_session(data, "a.png")
    with pytest.raises(ValidationError):
        store.get_or_compute_working_bytes(image_id, lambda _b: b"x" * 500)
    with pytest.raises(ValidationError):
        store.set_background_image(image_id, b"x" * 500, "bg.png")
    store.delete_session(image_id)
    assert store._total_bytes == 0


def test_budget_released_with_caches(temp_store_dir):
    data = make_image_bytes(50, 50)
    store = SessionStore(temp_store_dir, 600, max_total_bytes=10_000_000)
    image_id = store.create_session(data, "a.png")
    store.get_or_compute_working_bytes(image_id, lambda _b: b"x" * 300)
    store.set_background_image(image_id, b"y" * 200, "bg.png")
    assert store._total_bytes == len(data) + 500
    store.clear_background_image(image_id)
    assert store._total_bytes == len(data) + 300
    store.delete_session(image_id)
    assert store._total_bytes == 0


def test_tampered_cached_model_is_discarded(tmp_path, monkeypatch):
    model = tmp_path / "m.onnx"
    model.write_bytes(b"evil")
    monkeypatch.setattr(background, "_MODEL_PATH", str(model))
    monkeypatch.setattr(background, "_MODEL_DIR", str(tmp_path))
    good = b"good-weights"
    monkeypatch.setattr(background, "_MODEL_SHA256", hashlib.sha256(good).hexdigest())

    def fake_retrieve(url, known_hash, fname, path, progressbar):
        assert known_hash.startswith("sha256:")
        (tmp_path / fname).write_bytes(good)

    import pooch
    monkeypatch.setattr(pooch, "retrieve", fake_retrieve)
    assert background._ensure_model() == str(model)
    assert model.read_bytes() == good


def test_verified_cached_model_not_redownloaded(tmp_path, monkeypatch):
    model = tmp_path / "m.onnx"
    model.write_bytes(b"good-weights")
    monkeypatch.setattr(background, "_MODEL_PATH", str(model))
    monkeypatch.setattr(background, "_MODEL_SHA256", hashlib.sha256(b"good-weights").hexdigest())
    import pooch
    monkeypatch.setattr(pooch, "retrieve", lambda *a, **k: pytest.fail("should not download"))
    assert background._ensure_model() == str(model)


def test_upload_rate_limited_returns_json_429(app):
    app.config["RATELIMIT_UPLOAD"] = "2 per minute"
    from app.extensions import limiter
    limiter.reset()
    client = app.test_client()
    codes = []
    for _ in range(3):
        resp = client.post("/api/upload")  # no file -> 400, but still counts toward the limit
        codes.append(resp.status_code)
    assert codes == [400, 400, 429]
    assert resp.get_json()["error"]["code"] == "rate_limited"
