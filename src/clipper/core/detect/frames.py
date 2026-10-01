"""Still frames and contact sheets so the model can look at candidate moments."""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

from clipper.core import ffmpeg

MAX_FRAMES = 8
MAX_SHEET = 36


def frames(path: Path, timestamps: list[float], width: int = 640,
           crop: str | None = None) -> list[tuple[float, bytes]]:
    """JPEG bytes for each timestamp, scaled to ``width``. ``crop`` is an ffmpeg crop filter
    applied first (the crop is then enlarged to ``width``)."""
    if len(timestamps) > MAX_FRAMES:
        raise ValueError(f"At most {MAX_FRAMES} frames per call (got {len(timestamps)}); "
                         "use contact_sheet for an overview.")
    out = []
    with tempfile.TemporaryDirectory(prefix="clipper-frames-") as tmp:
        for i, t in enumerate(timestamps):
            dst = Path(tmp) / f"{i}.jpg"
            ffmpeg.extract_frame(path, t, dst, width, crop=crop)
            out.append((t, dst.read_bytes()))
    return out


def contact_sheet(path: Path, start: float, end: float, n: int = 12, width: int = 320
                  ) -> tuple[bytes, list[float]]:
    """One tiled JPEG of ``n`` evenly spaced frames in [start, end). Returns (jpeg, timestamps)."""
    if not 1 <= n <= MAX_SHEET:
        raise ValueError(f"n must be between 1 and {MAX_SHEET}")
    if end <= start:
        raise ValueError("end must be after start")
    step = (end - start) / n
    times = [round(start + step * (i + 0.5), 3) for i in range(n)]
    cols = min(n, math.ceil(math.sqrt(n * 1.5)))  # slightly wider than tall
    with tempfile.TemporaryDirectory(prefix="clipper-sheet-") as tmp:
        imgs = []
        for i, t in enumerate(times):
            dst = Path(tmp) / f"{i:03d}.jpg"
            ffmpeg.extract_frame(path, t, dst, width)
            imgs.append(dst)
        sheet = Path(tmp) / "sheet.jpg"
        ffmpeg.tile_images(imgs, sheet, cols)
        return sheet.read_bytes(), times
