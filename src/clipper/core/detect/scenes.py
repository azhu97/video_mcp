"""Scene-change detection with ffmpeg's ``scdet`` filter (no extra dependencies)."""

from __future__ import annotations

import re
from pathlib import Path

from clipper.core import cache, ffmpeg
from clipper.core.detect import Moment, Sensitivity

THRESHOLDS = {"low": 20.0, "medium": 10.0, "high": 5.0}
_LINE = re.compile(r"lavfi\.scd\.score:\s*([\d.]+),\s*lavfi\.scd\.time:\s*([\d.]+)")


def _window_args(start: float | None, end: float | None) -> list[str]:
    args: list[str] = []
    if start:
        args += ["-ss", f"{start:.3f}"]
    if end is not None:
        args += ["-t", f"{end - (start or 0):.3f}"]
    return args


def scan(path: Path, threshold: float, start: float | None = None, end: float | None = None) -> list[dict]:
    """Raw scene cuts: [{"time", "score"}] in source time. Analyzes a 320px-wide downscale."""
    res = ffmpeg.ffmpeg(
        *_window_args(start, end), "-i", str(path), "-map", "0:v:0", "-an", "-sn",
        "-vf", f"scale=320:-2,scdet=threshold={threshold}", "-f", "null", "-",
        loglevel="info", timeout=None,
    )
    offset = start or 0.0
    return [{"time": float(t) + offset, "score": float(s)} for s, t in _LINE.findall(res.stderr)]


def detect(path: Path, sensitivity: Sensitivity = "medium", start: float | None = None,
           end: float | None = None) -> list[Moment]:
    threshold = THRESHOLDS[sensitivity]
    key = f"scenes-t{threshold}-{start}-{end}"
    cuts = cache.get_or_compute(path, key, lambda: scan(path, threshold, start, end))
    return [
        Moment(time=c["time"], score=min(1.0, c["score"] / 50.0), source="scenes",
               note=f"scene change (score {c['score']:.1f})")
        for c in cuts
    ]
