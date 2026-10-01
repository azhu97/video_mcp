"""Batch clip export with per-clip error isolation and a manifest."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from clipper.config import get_settings
from clipper.core import cache, ffmpeg
from clipper.core.paths import ensure_not_source
from clipper.core.segments import Segment, clip_filename, format_time, process

log = logging.getLogger(__name__)

Mode = Literal["copy", "reencode", "smart"]
ProgressFn = Callable[[int, int, str], None]


@dataclass
class ExportSettings:
    mode: Mode = "copy"
    pad_before: float = 0.0
    pad_after: float = 0.0
    merge_gap: float | None = 0.0
    min_length: float = 0.1
    max_parallel: int | None = None


def cached_keyframes(src: Path) -> list[float]:
    return cache.get_or_compute(src, "keyframes", lambda: ffmpeg.keyframes(src))


def cached_probe(src: Path) -> ffmpeg.VideoInfo:
    return ffmpeg.VideoInfo.from_dict(cache.get_or_compute(src, "probe", lambda: ffmpeg.probe(src).to_dict()))


def _cut_one(src: Path, seg: Segment, dst: Path, mode: Mode, info: ffmpeg.VideoInfo,
             kfs: list[float] | None) -> dict[str, Any]:
    used = mode
    note = None
    if mode == "copy":
        a_start, a_end = ffmpeg.cut_copy(src, seg.start, seg.end, dst, info=info, kfs=kfs)
    elif mode == "reencode":
        a_start, a_end = ffmpeg.cut_reencode(src, seg.start, seg.end, dst, info=info)
    else:
        try:
            a_start, a_end = ffmpeg.cut_smart(src, seg.start, seg.end, dst, info=info, kfs=kfs)
        except ffmpeg.SmartCutUnsupported as e:
            note = f"smart cut fell back to reencode: {e}"
            log.info("%s: %s", dst.name, note)
            used = "reencode"
            a_start, a_end = ffmpeg.cut_reencode(src, seg.start, seg.end, dst, info=info)
    snap = round(seg.start - a_start, 3)
    out: dict[str, Any] = {
        "path": str(dst),
        "label": seg.label,
        "requested": {"start": round(seg.start, 3), "end": round(seg.end, 3)},
        "actual": {"start": round(a_start, 3), "end": round(a_end, 3)},
        "mode": used,
        "start_snap_seconds": snap,
    }
    if note:
        out["note"] = note
    if used == "copy" and snap > 0.05:
        out["note"] = (f"starts {snap:.2f}s early (snapped to keyframe); use mode='smart' or "
                       "'reencode' for a frame-accurate start")
    return out


def _invalid_reason(seg: Segment, duration: float) -> str | None:
    if seg.end <= seg.start:
        return f"end ({seg.end:.3f}s) is not after start ({seg.start:.3f}s)"
    if seg.start >= duration:
        return f"start ({seg.start:.3f}s) is past the end of the video ({duration:.3f}s)"
    return None


def export_clips(
    src: Path,
    segments: list[Segment],
    output_dir: Path,
    settings: ExportSettings | None = None,
    dry_run: bool = False,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Cut ``segments`` from ``src`` into ``output_dir``.

    Returns ``{"ok": [...], "failed": [{segment, error}], "manifest": path}``; in dry-run
    mode returns the processed segment list without cutting.
    """
    settings = settings or ExportSettings()
    info = cached_probe(src)
    if info.duration <= 0:
        raise ValueError(f"{src} has no measurable duration")
    rejected: list[dict[str, Any]] = []
    valid: list[Segment] = []
    for seg in segments:
        err = _invalid_reason(seg, info.duration)
        if err:
            rejected.append({"segment": {"start": seg.start, "end": seg.end, "label": seg.label}, "error": err})
        else:
            valid.append(seg)
    processed = process(valid, info.duration, settings.pad_before, settings.pad_after,
                        settings.merge_gap, settings.min_length)
    planned = [(seg, output_dir / clip_filename(src, i, seg)) for i, seg in enumerate(processed, 1)]
    dropped = len(valid) - len(processed)

    if dry_run:
        return {
            "dry_run": True,
            "source": str(src),
            "duration": info.duration,
            "segments": [
                {"start": round(s.start, 3), "end": round(s.end, 3), "label": s.label,
                 "start_hms": format_time(s.start), "duration": round(s.duration, 3), "output": str(d)}
                for s, d in planned
            ],
            "input_count": len(segments),
            "merged_or_dropped": dropped,
            "rejected": rejected,
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    kfs = cached_keyframes(src) if info.video and settings.mode in ("copy", "smart") else None
    workers = max(1, settings.max_parallel or get_settings().max_parallel)
    results: list[dict[str, Any] | None] = [None] * len(planned)
    failed: list[dict[str, Any]] = list(rejected)
    done = 0
    lock = threading.Lock()

    def job(i: int, seg: Segment, dst: Path) -> None:
        nonlocal done
        try:
            ensure_not_source(dst, src)
            results[i] = _cut_one(src, seg, dst, settings.mode, info, kfs)
        except Exception as e:  # isolate: one bad clip must not kill the batch
            log.warning("clip %d failed: %s", i + 1, e)
            dst.unlink(missing_ok=True)
            with lock:
                failed.append({"segment": {"start": seg.start, "end": seg.end, "label": seg.label},
                               "error": str(getattr(e, "reason", None) or e).splitlines()[0][:500]})
        finally:
            with lock:
                done += 1
                if progress:
                    progress(done, len(planned), dst.name)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for f in [pool.submit(job, i, s, d) for i, (s, d) in enumerate(planned)]:
            f.result()

    ok = [r for r in results if r is not None]
    manifest_path = output_dir / f"{src.stem}.manifest.json"
    manifest = {
        "source": str(src),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "settings": {"mode": settings.mode, "pad_before": settings.pad_before,
                     "pad_after": settings.pad_after, "merge_gap": settings.merge_gap,
                     "min_length": settings.min_length},
        "clips": ok,
        "failed": failed,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return {"ok": ok, "failed": failed, "manifest": str(manifest_path), "merged_or_dropped": dropped}
