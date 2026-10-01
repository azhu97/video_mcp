"""Per-video analysis cache: ``<cache_dir>/<sha1(path+size+mtime)>/<name>.json``.

Editing or replacing a video changes its size/mtime, which changes the key, so stale
analysis is never served.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from clipper.config import get_settings

T = TypeVar("T")

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def video_id(path: str | Path) -> str:
    p = Path(path).resolve()
    st = p.stat()
    return hashlib.sha1(f"{p}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:16]


def video_dir(path: str | Path) -> Path:
    vid = video_id(path)
    d = get_settings().cache_dir / vid
    d.mkdir(parents=True, exist_ok=True)
    meta = d / "meta.json"
    if not meta.exists():
        _atomic_write(meta, json.dumps({"video_id": vid, "path": str(Path(path).resolve())}))
    return d


def dir_for_id(vid: str) -> Path | None:
    if not vid.isalnum():
        return None
    d = get_settings().cache_dir / vid
    return d if d.is_dir() else None


def list_videos() -> list[dict[str, Any]]:
    root = get_settings().cache_dir
    out = []
    if root.is_dir():
        for meta in sorted(root.glob("*/meta.json")):
            try:
                info = json.loads(meta.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            info["analyses"] = sorted(p.stem for p in meta.parent.glob("*.json") if p.name != "meta.json")
            out.append(info)
    return out


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_.=" else "_" for c in name)


def get_or_compute(video: str | Path, name: str, compute: Callable[[], T]) -> T:
    """Return cached JSON-serializable result ``name`` for ``video``, computing it once."""
    path = video_dir(video) / f"{_safe_name(name)}.json"
    with _locks_guard:
        lock = _locks.setdefault(str(path), threading.Lock())
    with lock:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                path.unlink(missing_ok=True)
        value = compute()
        _atomic_write(path, json.dumps(value))
        return value


def read(video_id_: str, name: str) -> Any | None:
    d = dir_for_id(video_id_)
    if d is None:
        return None
    path = d / f"{_safe_name(name)}.json"
    return json.loads(path.read_text()) if path.exists() else None
