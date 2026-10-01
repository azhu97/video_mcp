"""Loudness-peak detection from EBU R128 momentary loudness (10 Hz)."""

from __future__ import annotations

import re
import statistics
from pathlib import Path

from clipper.core import cache, ffmpeg
from clipper.core.detect import Moment, Sensitivity
from clipper.core.detect.scenes import _window_args

# Peak threshold = median + max(K * MAD, MIN_LU): robust spread, but never closer to the
# median than MIN_LU so steady sources (music beds, tones) don't produce noise peaks.
K = {"low": 6.0, "medium": 4.0, "high": 2.5}
MIN_LU = {"low": 9.0, "medium": 6.0, "high": 4.0}
MIN_SPACING = 5.0  # seconds between reported peaks
SILENCE = -70.0  # LUFS; ebur128 reports ~-120 for digital silence

_PTS = re.compile(r"pts_time:([\d.]+)")
_M = re.compile(r"lavfi\.r128\.M=(-?[\d.]+|-?inf|nan)")


def loudness_curve(path: Path, start: float | None = None, end: float | None = None) -> list[list[float]]:
    """[[time, momentary LUFS], ...] at 100 ms resolution, in source time."""
    res = ffmpeg.ffmpeg(
        *_window_args(start, end), "-i", str(path), "-map", "0:a:0", "-vn", "-sn",
        "-af", "ebur128=metadata=1,ametadata=mode=print:key=lavfi.r128.M:file=-",
        "-f", "null", "-", timeout=None,
    )
    offset = start or 0.0
    curve: list[list[float]] = []
    t: float | None = None
    for line in res.stdout.splitlines():
        if m := _PTS.search(line):
            t = float(m.group(1))
        elif (m := _M.search(line)) and t is not None:
            try:
                val = float(m.group(1))
            except ValueError:
                val = -120.0
            curve.append([round(t + offset, 2), max(val, -120.0)])
    return curve


def find_peaks(curve: list[list[float]], k: float, min_lu: float,
               min_spacing: float = MIN_SPACING) -> list[dict]:
    voiced = [v for _, v in curve if v > SILENCE]
    if len(voiced) < 10:
        return []
    median = statistics.median(voiced)
    mad = statistics.median(abs(v - median) for v in voiced) or 0.5
    threshold = median + max(k * mad, min_lu)
    # Local maxima above threshold (plateaus count once), strongest first, then spacing filter.
    candidates = []
    onsets: dict[float, float] = {}
    for i, (t, v) in enumerate(curve):
        if v < threshold:
            continue
        prev_v = curve[i - 1][1] if i else -1e9
        next_v = curve[i + 1][1] if i + 1 < len(curve) else -1e9
        if v > prev_v and v >= next_v:
            candidates.append((t, v))
            j = i
            while j > 0 and curve[j - 1][1] >= threshold:
                j -= 1
            onsets[t] = curve[j][0]
    peak_max = max((v for _, v in candidates), default=threshold)
    chosen: list[tuple[float, float]] = []
    for t, v in sorted(candidates, key=lambda c: c[1], reverse=True):
        if all(abs(t - ct) >= min_spacing for ct, _ in chosen):
            chosen.append((t, v))
    span = max(peak_max - median, 1e-6)
    return [
        {"time": t, "onset": onsets[t], "lufs": v, "above_median": round(v - median, 1),
         "score": round((v - median) / span, 3)}
        for t, v in sorted(chosen)
    ]


def detect(path: Path, sensitivity: Sensitivity = "medium", start: float | None = None,
           end: float | None = None) -> list[Moment]:
    curve = cache.get_or_compute(path, f"loudness-{start}-{end}", lambda: loudness_curve(path, start, end))
    # Report where loudness crossed the threshold (each sample covers the following 100 ms).
    return [
        Moment(time=max(0.0, p["onset"] - 0.1), score=p["score"], source="audio",
               note=f"loudness spike +{p['above_median']} LU (peak at {p['time']:.1f}s)")
        for p in find_peaks(curve, K[sensitivity], MIN_LU[sensitivity])
    ]
