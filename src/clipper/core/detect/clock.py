"""Running-clock detection from an on-screen scoreboard, without OCR.

A running clock changes its digits once per second; a stopped clock is frozen. We crop the
clock's screen region, sample it a few times per second, and measure how much each sample
differs from the previous one (ffmpeg ``tblend`` difference + ``signalstats``). A spike is a
"tick". Ticks roughly one second apart form a "clock running" span. Seconds where the region
changes in most samples are treated as "busy" (overlay hidden, video showing through) and
ignored, so replays and transitions don't count as live play.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clipper.config import get_settings
from clipper.core import cache, ffmpeg
from clipper.core.detect import Moment, Sensitivity
from clipper.core.detect.scenes import _window_args

SAMPLE_FPS = 6
TICK_THRESHOLD = {"low": 14.0, "medium": 8.0, "high": 5.0}  # mean abs luma diff in the region
MAX_TICK_GAP = 2.2  # seconds; tolerates one missed tick
MIN_TICKS = 3
BUSY_RATIO = 0.5  # fraction of a second's samples that changed → overlay hidden

_PTS = re.compile(r"pts_time:([\d.]+)")
_YAVG = re.compile(r"lavfi\.signalstats\.YAVG=([\d.]+)")


@dataclass(frozen=True)
class Region:
    x: int
    y: int
    w: int
    h: int

    def crop(self) -> str:
        return f"crop={self.w}:{self.h}:{self.x}:{self.y}"

    def key(self) -> str:
        return f"{self.x}_{self.y}_{self.w}_{self.h}"


def parse_region(spec: str | list[float] | tuple[float, ...], width: int, height: int) -> Region:
    """``"x,y,w,h"`` (or a 4-list) in source pixels, or as fractions of the frame (all ≤ 1),
    or the name of a region from the ``[clock_regions]`` config table."""
    if isinstance(spec, str) and spec in get_settings().clock_regions:
        spec = get_settings().clock_regions[spec]
    if isinstance(spec, str):
        parts = [p for p in re.split(r"[,:\s]+", spec.strip()) if p]
    else:
        parts = list(spec)
    try:
        vals = [float(p) for p in parts]
    except (TypeError, ValueError):
        named = ", ".join(sorted(get_settings().clock_regions)) or "none configured"
        raise ValueError(f"region must be 'x,y,w,h' or a named region ({named}); got {spec!r}") from None
    if len(vals) != 4:
        raise ValueError(f"region must have 4 numbers x,y,w,h; got {spec!r}")
    if all(0 <= v <= 1 for v in vals):
        vals = [vals[0] * width, vals[1] * height, vals[2] * width, vals[3] * height]
    x, y, w, h = (int(round(v)) for v in vals)
    if w < 4 or h < 4 or x < 0 or y < 0 or x + w > width or y + h > height:
        raise ValueError(f"region {x},{y},{w},{h} is not inside the {width}x{height} frame")
    return Region(x, y, w, h)


CHUNK_SECONDS = 600.0


def _scan_window(path: Path, region: Region, start: float, end: float) -> list[list[float]]:
    res = ffmpeg.ffmpeg(
        *_window_args(start, end), "-i", str(path), "-map", "0:v:0", "-an", "-sn",
        "-vf", f"{region.crop()},fps={SAMPLE_FPS},format=gray,tblend=all_mode=difference,signalstats,"
               "metadata=print:key=lavfi.signalstats.YAVG:file=-",
        "-f", "null", "-", timeout=None,
    )
    out: list[list[float]] = []
    t: float | None = None
    for line in res.stdout.splitlines():
        if m := _PTS.search(line):
            t = float(m.group(1))
        elif (m := _YAVG.search(line)) and t is not None:
            out.append([round(t + start, 3), float(m.group(1))])
    return out


def scan(path: Path, region: Region, start: float, end: float, workers: int = 2,
         progress: Callable[[int, int, str], None] | None = None) -> list[list[float]]:
    """[[time, mean abs change vs previous sample], ...] at SAMPLE_FPS, in source time.

    Long spans are scanned in 10-minute chunks (in parallel) so progress can be reported.
    """
    bounds = []
    t = start
    while t < end:
        bounds.append((t, min(end, t + CHUNK_SECONDS)))
        t += CHUNK_SECONDS
    results: list[list[list[float]]] = [[] for _ in bounds]
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_scan_window, path, region, a, b): i for i, (a, b) in enumerate(bounds)}
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()
            done += 1
            if progress:
                progress(done, len(bounds), f"scanned {done * CHUNK_SECONDS / 60:.0f} of "
                                            f"{(end - start) / 60:.0f} min")
    return [sample for chunk in results for sample in chunk]


def running_spans(samples: list[list[float]], threshold: float, max_gap: float = MAX_TICK_GAP,
                  min_ticks: int = MIN_TICKS) -> list[dict[str, Any]]:
    """Group 1 Hz ticks into spans where the clock was running."""
    changed_per_sec: dict[int, int] = {}
    total_per_sec: dict[int, int] = {}
    for t, v in samples:
        s = int(t)
        total_per_sec[s] = total_per_sec.get(s, 0) + 1
        if v >= threshold:
            changed_per_sec[s] = changed_per_sec.get(s, 0) + 1
    busy = {s for s, n in changed_per_sec.items() if n / max(total_per_sec[s], 1) > BUSY_RATIO}

    ticks: list[float] = []
    for t, v in samples:
        if v >= threshold and int(t) not in busy:
            if ticks and t - ticks[-1] < 0.5:
                continue  # one digit change can straddle two samples
            ticks.append(t)

    spans: list[dict[str, Any]] = []
    run: list[float] = []

    def flush() -> None:
        if len(run) >= min_ticks:
            # The clock started up to a second before its first visible change, and stopped
            # up to a second after the last one.
            spans.append({"start": round(max(0.0, run[0] - 1.0), 2), "end": round(run[-1] + 1.0, 2),
                          "ticks": len(run)})

    for t in ticks:
        if run and t - run[-1] > max_gap:
            flush()
            run = []
        run.append(t)
    flush()
    for s in spans:
        s["duration"] = round(s["end"] - s["start"], 2)
    return spans


def find_running(path: Path, region: Region, sensitivity: Sensitivity = "medium",
                 start: float | None = None, end: float | None = None, min_duration: float = 3.0,
                 progress: Callable[[int, int, str], None] | None = None) -> list[dict[str, Any]]:
    from clipper.core.export import cached_probe

    lo, hi = start or 0.0, end if end is not None else cached_probe(path).duration
    full = cache.read(cache.video_id(path), f"clock-{region.key()}-{SAMPLE_FPS}-None-None")
    if full is not None:  # a whole-video scan covers any window
        samples = [s for s in full if lo <= s[0] < hi]
    else:
        samples = cache.get_or_compute(
            path, f"clock-{region.key()}-{SAMPLE_FPS}-{start}-{end}",
            lambda: scan(path, region, lo, hi, get_settings().max_parallel, progress),
        )
    spans = running_spans(samples, TICK_THRESHOLD[sensitivity])
    return [s for s in spans if s["duration"] >= min_duration]


def detect(path: Path, region: Region, sensitivity: Sensitivity = "medium", start: float | None = None,
           end: float | None = None) -> list[Moment]:
    """Moments where the clock starts running (play resumes)."""
    spans = find_running(path, region, sensitivity, start, end)
    longest = max((s["duration"] for s in spans), default=1.0)
    return [Moment(time=s["start"], score=min(1.0, s["duration"] / longest), source="clock",
                   note=f"clock runs for {s['duration']:.0f}s (until {s['end']:.1f}s)")
            for s in spans]
