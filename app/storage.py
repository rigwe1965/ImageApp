import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .errors import SessionNotFoundError, ValidationError
from .image_processor import get_original_info
from .settings import ImageSettings
from .utils import new_id
from .validators import safe_join_within

logger = logging.getLogger(__name__)

_EXT_BY_FORMAT = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "BMP": "bmp", "GIF": "gif"}

# image_id is always a uuid4().hex from new_id() — but every public method here accepts one
# straight from a URL path segment, and several build a filesystem path (including one fed to
# shutil.rmtree) by joining it onto temp_dir. A value like ".." or "../.." would resolve outside
# temp_dir entirely (verified: os.path.join(temp_dir, "..") lands on temp_dir's parent), so any
# id that doesn't match this shape is rejected before it ever touches a path. safe_join_within()
# (below, at every session_dir construction site) is a second, independent layer on top of this
# regex check — belt-and-suspenders, not a replacement for it.
_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _validate_id(image_id: str) -> None:
    if not _ID_RE.match(image_id or ""):
        raise SessionNotFoundError(f"No session found for id '{image_id}'. Please upload the image again.")


class BatchRegistry:
    """Tracks which image_ids belong to which batch_id. Kept separate from SessionStore's own
    session-lifecycle bookkeeping (disk I/O, the total-byte budget, TTL expiry) because batch
    membership is a distinct concern — grouping already-independent sessions together — that
    doesn't need a SessionEntry or the session lock at all, only its own small lock over its own
    dict."""

    def __init__(self):
        self._batches: Dict[str, List[str]] = {}
        self._lock = threading.Lock()

    def add_member(self, batch_id: str, image_id: str) -> None:
        with self._lock:
            self._batches.setdefault(batch_id, []).append(image_id)

    def remove_member(self, image_id: str, batch_id: Optional[str]) -> None:
        if not batch_id:
            return
        with self._lock:
            members = self._batches.get(batch_id)
            if members:
                try:
                    members.remove(image_id)
                except ValueError:
                    pass

    def get_members(self, batch_id: str) -> List[str]:
        with self._lock:
            return list(self._batches.get(batch_id, []))

    def pop_members(self, batch_id: str) -> List[str]:
        with self._lock:
            return self._batches.pop(batch_id, [])


@dataclass
class SessionEntry:
    image_id: str
    original_filename: str
    original_format: str
    width: int
    height: int
    has_alpha: bool
    original_size_bytes: int
    disk_path: str
    original_bytes: Optional[bytes]
    current_settings: dict
    initial_settings: dict
    last_access: float = field(default_factory=time.time)
    batch_id: Optional[str] = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    bg_removed_cache: Optional[bytes] = None
    background_image_bytes: Optional[bytes] = None
    background_image_filename: Optional[str] = None


class SessionStore:
    def __init__(self, temp_dir: str, ttl_seconds: int, max_total_bytes: Optional[int] = None):
        self.temp_dir = temp_dir
        self.ttl_seconds = ttl_seconds
        # Caps total bytes held in `original_bytes` across all in-memory sessions, independent
        # of the TTL sweep — without this, a sustained upload rate within one TTL window (default
        # 45 min) can grow memory without bound, since MAX_SINGLE_FILE_SIZE/MAX_BATCH_FILES only
        # cap a single request, not cumulative retention. None disables the cap (unbounded).
        self.max_total_bytes = max_total_bytes
        self._sessions: Dict[str, SessionEntry] = {}
        self._batch_registry = BatchRegistry()
        self._global_lock = threading.Lock()
        self._total_bytes = 0
        os.makedirs(self.temp_dir, exist_ok=True)

    # -- memory budget -----------------------------------------------------

    def _charge(self, num_bytes: int) -> None:
        """Reserves num_bytes of the global in-memory budget or raises. Used for every cached
        buffer (original, background-removal cutout, background image), not just uploads."""
        with self._global_lock:
            if self.max_total_bytes is not None and self._total_bytes + num_bytes > self.max_total_bytes:
                raise ValidationError(
                    "Server is at capacity (too many images held in memory). Try again shortly."
                )
            self._total_bytes += num_bytes

    def _release(self, num_bytes: int) -> None:
        with self._global_lock:
            self._total_bytes = max(0, self._total_bytes - num_bytes)

    # -- session lifecycle -------------------------------------------------

    def create_session(self, original_bytes: bytes, original_filename: str,
                        batch_id: Optional[str] = None, info: Optional[dict] = None) -> str:
        # Callers that already probed the image (validate_and_probe_image) should pass its
        # returned info dict here instead of letting this decode+verify+reopen+load run twice.
        info = info or get_original_info(original_bytes)

        self._charge(len(original_bytes))

        try:
            image_id = new_id()
            ext = _EXT_BY_FORMAT.get(info["format"], "bin")
            session_dir = safe_join_within(self.temp_dir, image_id)
            os.makedirs(session_dir, exist_ok=True)
            disk_path = os.path.join(session_dir, f"original.{ext}")
            with open(disk_path, "wb") as f:
                f.write(original_bytes)
        except Exception:
            # Roll back the budget charge -- nothing was actually retained if disk I/O failed,
            # so counting these bytes forever would permanently shrink the available budget.
            self._release(len(original_bytes))
            raise

        defaults = ImageSettings().to_dict()
        entry = SessionEntry(
            image_id=image_id,
            original_filename=original_filename or f"image.{ext}",
            original_format=info["format"],
            width=info["width"],
            height=info["height"],
            has_alpha=info["has_alpha"],
            original_size_bytes=info["size_bytes"],
            disk_path=disk_path,
            original_bytes=original_bytes,
            current_settings=defaults,
            initial_settings=defaults,
            batch_id=batch_id,
        )
        with self._global_lock:
            self._sessions[image_id] = entry
        if batch_id:
            self._batch_registry.add_member(batch_id, image_id)
        return image_id

    def _get_entry(self, image_id: str) -> SessionEntry:
        _validate_id(image_id)
        with self._global_lock:
            entry = self._sessions.get(image_id)
        if entry is not None:
            entry.last_access = time.time()
            return entry

        # not in memory — try lazy rehydration from disk (covers cache eviction, not process restart)
        session_dir = safe_join_within(self.temp_dir, image_id)
        if not os.path.isdir(session_dir):
            raise SessionNotFoundError(f"No session found for id '{image_id}'. Please upload the image again.")
        candidates = [f for f in os.listdir(session_dir) if f.startswith("original.")]
        if not candidates:
            raise SessionNotFoundError(f"No session found for id '{image_id}'. Please upload the image again.")
        disk_path = os.path.join(session_dir, candidates[0])
        with open(disk_path, "rb") as f:
            original_bytes = f.read()
        info = get_original_info(original_bytes)
        defaults = ImageSettings().to_dict()
        entry = SessionEntry(
            image_id=image_id,
            original_filename=f"image.{candidates[0].rsplit('.', 1)[-1]}",
            original_format=info["format"],
            width=info["width"],
            height=info["height"],
            has_alpha=info["has_alpha"],
            original_size_bytes=info["size_bytes"],
            disk_path=disk_path,
            original_bytes=original_bytes,
            current_settings=defaults,
            initial_settings=defaults,
        )
        with self._global_lock:
            self._sessions[image_id] = entry
            # Rehydrated data already legitimately existed on disk (this isn't a new upload),
            # so it's tracked for future cap checks but not itself rejected by max_total_bytes.
            self._total_bytes += len(original_bytes)
        os.utime(disk_path, None)
        return entry

    def get_original_bytes(self, image_id: str) -> bytes:
        entry = self._get_entry(image_id)
        if entry.original_bytes is not None:
            return entry.original_bytes
        with open(entry.disk_path, "rb") as f:
            entry.original_bytes = f.read()
        return entry.original_bytes

    def get_or_compute_working_bytes(self, image_id: str, compute_fn) -> bytes:
        """Generic memoized derived-bytes cache keyed on the session's original image: the
        caller (app/pipeline.py) decides *when* a derived artifact (e.g. a background-removed
        cutout) is needed and supplies `compute_fn(original_bytes) -> bytes`; this store only
        knows how to cache the result, not why it exists — that keeps SessionStore a pure
        persistence/cache layer with no knowledge of specific domain operations like background
        removal. Deterministic given the same original, so it's safe to reuse across every
        subsequent apply/reset/preset call for this session."""
        entry = self._get_entry(image_id)
        with entry.lock:
            if entry.bg_removed_cache is None:
                original = self.get_original_bytes(image_id)
                computed = compute_fn(original)
                self._charge(len(computed))
                entry.bg_removed_cache = computed
            return entry.bg_removed_cache

    def set_background_image(self, image_id: str, data: bytes, filename: str) -> None:
        entry = self._get_entry(image_id)
        with entry.lock:
            previous = len(entry.background_image_bytes or b"")
            if len(data) > previous:
                self._charge(len(data) - previous)
            else:
                self._release(previous - len(data))
            entry.background_image_bytes = data
            entry.background_image_filename = filename

    def get_background_image_bytes(self, image_id: str) -> Optional[bytes]:
        entry = self._get_entry(image_id)
        return entry.background_image_bytes

    def clear_background_image(self, image_id: str) -> None:
        entry = self._get_entry(image_id)
        with entry.lock:
            self._release(len(entry.background_image_bytes or b""))
            entry.background_image_bytes = None
            entry.background_image_filename = None

    def get_info(self, image_id: str) -> dict:
        entry = self._get_entry(image_id)
        return {
            "image_id": entry.image_id,
            "filename": entry.original_filename,
            "format": entry.original_format,
            "width": entry.width,
            "height": entry.height,
            "has_alpha": entry.has_alpha,
            "size_bytes": entry.original_size_bytes,
        }

    def get_settings(self, image_id: str) -> dict:
        entry = self._get_entry(image_id)
        with entry.lock:
            return dict(entry.current_settings)

    def update_settings(self, image_id: str, settings_dict: dict) -> dict:
        entry = self._get_entry(image_id)
        with entry.lock:
            entry.current_settings = dict(settings_dict)
            return dict(entry.current_settings)

    def set_initial_settings(self, image_id: str, settings_dict: dict) -> None:
        entry = self._get_entry(image_id)
        with entry.lock:
            entry.initial_settings = dict(settings_dict)
            entry.current_settings = dict(settings_dict)

    def reset_settings(self, image_id: str) -> dict:
        entry = self._get_entry(image_id)
        with entry.lock:
            entry.current_settings = dict(entry.initial_settings)
            return dict(entry.current_settings)

    def delete_session(self, image_id: str) -> None:
        _validate_id(image_id)
        with self._global_lock:
            entry = self._sessions.pop(image_id, None)
            if entry:
                held = (
                    len(entry.original_bytes or b"")
                    + len(entry.bg_removed_cache or b"")
                    + len(entry.background_image_bytes or b"")
                )
                self._total_bytes = max(0, self._total_bytes - held)
        if entry:
            self._batch_registry.remove_member(image_id, entry.batch_id)
        session_dir = safe_join_within(self.temp_dir, image_id)
        shutil.rmtree(session_dir, ignore_errors=True)

    # -- batch ---------------------------------------------------------
    # Membership tracking itself lives in BatchRegistry (see above) — these two methods are a
    # thin pass-through that adds the one behavior specific to SessionStore's own domain: turning
    # "no members" into the same SessionNotFoundError callers already expect for a missing image.

    def get_batch_members(self, batch_id: str) -> List[str]:
        members = self._batch_registry.get_members(batch_id)
        if not members:
            raise SessionNotFoundError(f"No batch found for id '{batch_id}'.")
        return members

    def delete_batch(self, batch_id: str) -> None:
        members = self._batch_registry.pop_members(batch_id)
        for image_id in members:
            self.delete_session(image_id)

    # -- cleanup ---------------------------------------------------------

    def cleanup_expired(self) -> None:
        now = time.time()
        with self._global_lock:
            expired = [iid for iid, e in self._sessions.items() if now - e.last_access > self.ttl_seconds]
        for image_id in expired:
            logger.info("Purging expired session %s", image_id)
            self.delete_session(image_id)

        # also sweep orphaned disk directories (covers process-restart case where memory is empty)
        if not os.path.isdir(self.temp_dir):
            return
        for name in os.listdir(self.temp_dir):
            session_dir = os.path.join(self.temp_dir, name)
            if not os.path.isdir(session_dir):
                continue
            with self._global_lock:
                if name in self._sessions:
                    continue
            try:
                mtime = os.path.getmtime(session_dir)
            except OSError:
                continue
            if now - mtime > self.ttl_seconds:
                logger.info("Purging orphaned session directory %s", name)
                shutil.rmtree(session_dir, ignore_errors=True)

    def start_cleanup_thread(self, interval_seconds: int) -> threading.Thread:
        def _loop():
            while True:
                time.sleep(interval_seconds)
                try:
                    self.cleanup_expired()
                except Exception:
                    logger.exception("Error during session cleanup")

        thread = threading.Thread(target=_loop, name="session-cleanup", daemon=True)
        thread.start()
        return thread
