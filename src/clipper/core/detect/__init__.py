"""Key-moment detectors. All produce ``Moment``s; ``find_key_moments`` combines them."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

Sensitivity = Literal["low", "medium", "high"]
METHODS = ("scenes", "audio", "transcript", "clock")


@dataclass
class Moment:
    time: float
    score: float
    source: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["time"] = round(self.time, 3)
        d["score"] = round(self.score, 3)
        return d


def find_key_moments(
    path: Path,
    methods: list[str],
    max_results: int = 50,
    sensitivity: Sensitivity = "medium",
    start: float | None = None,
    end: float | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    clock_region: str | None = None,
) -> dict[str, Any]:
    from clipper.core.detect import audio, clock, scenes, transcript
    from clipper.core.export import cached_probe

    unknown = [m for m in methods if m not in METHODS]
    if unknown:
        raise ValueError(f"Unknown method(s) {unknown}; choose from {list(METHODS)}")
    if sensitivity not in ("low", "medium", "high"):
        raise ValueError("sensitivity must be 'low', 'medium' or 'high'")
    info = cached_probe(path)
    if "clock" in methods and not clock_region:
        raise ValueError("The 'clock' method needs clock_region (x,y,w,h of the on-screen clock).")
    detectors: dict[str, Callable[..., list[Moment]]] = {
        "scenes": scenes.detect, "audio": audio.detect, "transcript": transcript.detect_pauses,
    }
    if clock_region and info.video:
        v = info.video
        region = clock.parse_region(clock_region, v.width or 0, v.height or 0)
        detectors["clock"] = lambda p, **kw: clock.detect(p, region, **kw)
    moments: list[Moment] = []
    skipped: dict[str, str] = {}
    for i, method in enumerate(methods):
        if progress:
            progress(i, len(methods), f"running {method}")
        if method in ("scenes", "clock") and info.video is None:
            skipped[method] = "no video stream"
            continue
        if method in ("audio", "transcript") and info.audio is None:
            skipped[method] = "no audio stream"
            continue
        moments += detectors[method](path, sensitivity=sensitivity, start=start, end=end)
    if progress:
        progress(len(methods), len(methods), "done")
    total = len(moments)
    top = sorted(moments, key=lambda m: m.score, reverse=True)[:max_results]
    out: dict[str, Any] = {
        "candidates": [m.to_dict() for m in sorted(top, key=lambda m: m.time)],
        "total_found": total,
        "returned": len(top),
        "duration": info.duration,
    }
    if skipped:
        out["skipped"] = skipped
    return out
