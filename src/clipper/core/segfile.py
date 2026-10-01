"""LosslessCut interop: ``.llc`` project files (JSON5) and segment CSVs.

LosslessCut CSV is ``start,end,name`` per line in seconds, no header. ``.llc`` is JSON5 with
``cutSegments: [{start, end, name}]``; we write strict JSON (valid JSON5) and read either.
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Literal

from clipper.core.segments import Segment, parse_time

Format = Literal["llc", "csv"]


def write(path: Path, media: Path, segments: list[Segment], fmt: Format) -> None:
    if fmt == "llc":
        data = {
            "version": 1,
            "mediaFileName": media.name,
            "cutSegments": [{"start": s.start, "end": s.end, "name": s.label} for s in segments],
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    elif fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        for s in segments:
            w.writerow([f"{s.start:.3f}", f"{s.end:.3f}", s.label])
        path.write_text(buf.getvalue())
    else:
        raise ValueError("format must be 'llc' or 'csv'")


def default_path(media: Path, fmt: Format) -> Path:
    # LosslessCut auto-loads `<name>-proj.llc` next to the media file.
    return media.with_name(f"{media.stem}-proj.llc") if fmt == "llc" else media.with_suffix(".segments.csv")


def read(path: Path) -> tuple[list[Segment], str | None]:
    """Returns (segments, mediaFileName or None). Format inferred from the extension/content."""
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".llc" or text.lstrip().startswith("{"):
        data = _json5_loads(text)
        segs = []
        for s in data.get("cutSegments", []):
            if s.get("start") is None or s.get("end") is None:
                continue  # LosslessCut allows open-ended markers; skip them
            segs.append(Segment(float(s["start"]), float(s["end"]), s.get("name") or ""))
        return segs, data.get("mediaFileName")
    segs = []
    for row in csv.reader(io.StringIO(text)):
        if not row or not row[0].strip():
            continue
        try:
            start = parse_time(row[0])
        except ValueError:
            continue  # header line
        end_s = row[1].strip() if len(row) > 1 else ""
        if not end_s:
            continue
        segs.append(Segment(start, parse_time(end_s), row[2].strip() if len(row) > 2 else ""))
    return segs, None


def _json5_loads(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Minimal JSON5 → JSON: comments, single-quoted strings, unquoted keys, trailing commas.
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            j = i + 1
            buf = []
            while j < n and text[j] != c:
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j: j + 2])
                    j += 2
                    continue
                buf.append('\\"' if text[j] == '"' else text[j])
                j += 1
            out.append('"' + "".join(buf).replace("\\'", "'") + '"')
            i = j + 1
        elif text.startswith("//", i):
            i = text.find("\n", i) if "\n" in text[i:] else n
        elif text.startswith("/*", i):
            i = text.find("*/", i) + 2
        else:
            out.append(c)
            i += 1
    s = "".join(out)
    s = re.sub(r"([{,]\s*)([A-Za-z_$][\w$]*)\s*:", r'\1"\2":', s)
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    return json.loads(s)
