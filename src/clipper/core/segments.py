"""Segment arithmetic: parsing, padding, clamping, merging, naming. Pure functions."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    label: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start


_TIME_RE = re.compile(r"^\s*(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)\s*$")


def parse_time(value: float | int | str) -> float:
    """Seconds as a number, or ``SS(.ms)``, ``MM:SS(.ms)``, ``HH:MM:SS(.ms)`` strings."""
    if isinstance(value, bool):
        raise ValueError(f"invalid timestamp: {value!r}")
    if isinstance(value, (int, float)):
        t = float(value)
    else:
        s = value.strip()
        m = _TIME_RE.match(s)
        if m:
            h, mi, sec = m.groups()
            if float(sec) >= 60 or (h is not None and int(mi) >= 60):
                raise ValueError(f"invalid timestamp: {value!r}")
            t = int(h or 0) * 3600 + int(mi) * 60 + float(sec)
        else:
            try:
                t = float(s)
            except ValueError:
                raise ValueError(
                    f"invalid timestamp: {value!r} (use seconds or HH:MM:SS.ms)"
                ) from None
    if t < 0 or t != t:
        raise ValueError(f"invalid timestamp: {value!r}")
    return t


def format_time(t: float) -> str:
    ms = round(t * 1000)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def pad(seg: Segment, before: float, after: float) -> Segment:
    return replace(seg, start=seg.start - before, end=seg.end + after)


def clamp(seg: Segment, duration: float) -> Segment:
    return replace(seg, start=max(0.0, min(seg.start, duration)), end=max(0.0, min(seg.end, duration)))


def merge_overlaps(segs: list[Segment], gap_tolerance: float = 0.0) -> list[Segment]:
    """Merge segments that overlap or are within ``gap_tolerance`` seconds of each other."""
    out: list[Segment] = []
    for seg in sorted(segs, key=lambda s: (s.start, s.end)):
        if out and seg.start <= out[-1].end + gap_tolerance:
            prev = out[-1]
            labels = [lbl for lbl in (prev.label, seg.label) if lbl]
            label = " + ".join(dict.fromkeys(" + ".join(labels).split(" + "))) if labels else ""
            out[-1] = Segment(prev.start, max(prev.end, seg.end), label)
        else:
            out.append(seg)
    return out


def drop_short(segs: list[Segment], min_length: float) -> list[Segment]:
    return [s for s in segs if s.duration >= min_length]


def process(
    segs: list[Segment],
    duration: float,
    pad_before: float = 0.0,
    pad_after: float = 0.0,
    merge_gap: float | None = 0.0,
    min_length: float = 0.1,
) -> list[Segment]:
    """pad → clamp → merge → drop short. ``merge_gap=None`` disables merging."""
    out = [clamp(pad(s, pad_before, pad_after), duration) for s in segs]
    if merge_gap is not None:
        out = merge_overlaps(out, merge_gap)
    else:
        out = sorted(out, key=lambda s: (s.start, s.end))
    return drop_short(out, min_length)


def slugify(text: str, max_len: int = 40) -> str:
    # Strip accents (ü → u), then turn every other non-alphanumeric run into one dash.
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-")


def clip_filename(src: Path, index: int, seg: Segment, ext: str | None = None) -> str:
    """``{stem}_{index:02d}_{label-slug}_{HHMMSS}.{ext}`` (slug omitted when there is no label)."""
    t = int(seg.start)
    hhmmss = f"{t // 3600:02d}{t % 3600 // 60:02d}{t % 60:02d}"
    parts = [src.stem, f"{index:02d}"]
    if slug := slugify(seg.label):
        parts.append(slug)
    parts.append(hhmmss)
    return "_".join(parts) + (ext or src.suffix)
