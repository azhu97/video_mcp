"""Local speech-to-text with faster-whisper (optional extra: ``clipper-mcp[transcribe]``)."""

from __future__ import annotations

import difflib
import logging
import re
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from clipper.config import get_settings
from clipper.core import cache, ffmpeg
from clipper.core.detect import Moment, Sensitivity

log = logging.getLogger(__name__)

_models: dict[str, Any] = {}
_model_lock = threading.Lock()

PAUSE_SECONDS = {"low": 3.0, "medium": 2.0, "high": 1.2}


class TranscriptionUnavailable(RuntimeError):
    pass


def _model(name: str) -> Any:
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise TranscriptionUnavailable(
            "Transcription needs faster-whisper: install the 'transcribe' extra "
            "(`uv sync --extra transcribe`, or run via `uvx --from 'clipper-mcp[transcribe]' clipper-mcp`)."
        ) from e
    with _model_lock:
        if name not in _models:
            log.info("loading whisper model %s", name)
            _models[name] = WhisperModel(name, device="auto", compute_type="int8")
        return _models[name]


def _run_whisper(path: Path, model: str, start: float | None, end: float | None, words: bool,
                 progress: Callable[[float, float], None] | None) -> dict[str, Any]:
    whisper = _model(model)  # raises TranscriptionUnavailable with an install hint
    import numpy as np  # ships with faster-whisper; keep the base install free of it

    offset = start or 0.0
    with tempfile.TemporaryDirectory(prefix="clipper-asr-") as tmp:
        pcm = Path(tmp) / "audio.pcm"
        ffmpeg.extract_audio_pcm(path, pcm, start, end)
        # Hand whisper decoded samples, not a path: its own PyAV-based decoder is version-fragile.
        samples = np.frombuffer(pcm.read_bytes(), dtype=np.int16).astype(np.float32) / 32768.0
        segments, info = whisper.transcribe(samples, word_timestamps=words, vad_filter=True)
        out = []
        for seg in segments:  # generator: decoding happens here
            item: dict[str, Any] = {
                "start": round(float(seg.start) + offset, 2),
                "end": round(float(seg.end) + offset, 2),
                "text": seg.text.strip(),
            }
            if words and seg.words:
                item["words"] = [
                    [round(float(w.start) + offset, 2), round(float(w.end) + offset, 2), w.word.strip()]
                    for w in seg.words
                ]
            out.append(item)
            if progress:
                progress(seg.end, info.duration)
    for i, seg in enumerate(out):
        seg["id"] = i
    return {"language": info.language, "model": model, "start": offset,
            "end": end, "segments": out}


def transcript(path: Path, start: float | None = None, end: float | None = None, words: bool = False,
               model: str | None = None, progress: Callable[[float, float], None] | None = None
               ) -> dict[str, Any]:
    """Cached transcript. Reuses a cached full-file transcript for any sub-range."""
    model = model or get_settings().whisper_model
    full_key = f"transcript-{model}-w{int(words)}-None-None"
    full = cache.read(cache.video_id(path), full_key)
    if full is None and words is False:
        full = cache.read(cache.video_id(path), f"transcript-{model}-w1-None-None")
    if full is not None:
        return _slice(full, start, end)
    key = f"transcript-{model}-w{int(words)}-{start}-{end}"
    return cache.get_or_compute(path, key, lambda: _run_whisper(path, model, start, end, words, progress))


def _slice(t: dict[str, Any], start: float | None, end: float | None) -> dict[str, Any]:
    if start is None and end is None:
        return t
    lo, hi = start or 0.0, end if end is not None else float("inf")
    return {**t, "start": lo, "end": end, "segments": [s for s in t["segments"] if s["end"] > lo and s["start"] < hi]}


def page(t: dict[str, Any], offset: int = 0, limit: int = 100, include_words: bool = False) -> dict[str, Any]:
    segs = t["segments"]
    items = segs[offset: offset + limit]
    if not include_words:
        items = [{k: v for k, v in s.items() if k != "words"} for s in items]
    nxt = offset + limit if offset + limit < len(segs) else None
    return {"language": t.get("language"), "total_segments": len(segs), "offset": offset,
            "segments": items, "next_offset": nxt}


SearchMode = Literal["keyword", "regex", "fuzzy"]


def search(t: dict[str, Any], query: str, mode: SearchMode = "keyword", max_results: int = 50,
           fuzzy_threshold: float = 0.75) -> list[dict[str, Any]]:
    """Find segments matching ``query``.

    keyword: every word of the query appears (case-insensitive, whole-word prefix).
    regex:   Python regex, case-insensitive.
    fuzzy:   best sliding-window similarity ≥ ``fuzzy_threshold`` (tolerates ASR misspellings).
    """
    hits: list[dict[str, Any]] = []
    if mode == "regex":
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"invalid regex: {e}") from None
    terms = [re.compile(rf"\b{re.escape(w)}", re.IGNORECASE) for w in query.split()]
    q_norm = _norm(query)
    for seg in t["segments"]:
        text = seg["text"]
        score = None
        if mode == "keyword":
            if terms and all(p.search(text) for p in terms):
                score = 1.0
        elif mode == "regex":
            if pattern.search(text):
                score = 1.0
        elif mode == "fuzzy":
            score = _fuzzy(q_norm, _norm(text))
            if score < fuzzy_threshold:
                score = None
        else:
            raise ValueError("mode must be 'keyword', 'regex' or 'fuzzy'")
        if score is not None:
            hit = {"id": seg["id"], "start": seg["start"], "end": seg["end"], "text": text,
                   "score": round(score, 3)}
            if mode == "keyword" and seg.get("words"):
                first = next((w for w in seg["words"] if terms[0].search(w[2])), None)
                if first:
                    hit["word_time"] = first[0]
            hits.append(hit)
    if mode == "fuzzy":
        hits.sort(key=lambda h: h["score"], reverse=True)
        hits = sorted(hits[:max_results], key=lambda h: h["start"])
    return hits[:max_results]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", s.lower())


def _fuzzy(q: str, text: str) -> float:
    qw, tw = q.split(), text.split()
    if not qw or not tw:
        return 0.0
    best = 0.0
    # Windows one word shorter/longer than the query catch split or merged words ("time out").
    for n in {max(1, len(qw) - 1), len(qw), len(qw) + 1}:
        for i in range(max(1, len(tw) - n + 1)):
            window = " ".join(tw[i: i + n])
            best = max(best, difflib.SequenceMatcher(None, q, window).ratio())
    return best


def detect_pauses(path: Path, sensitivity: Sensitivity = "medium", start: float | None = None,
                  end: float | None = None) -> list[Moment]:
    """Speech that resumes after a long pause: a cheap proxy for topic/section boundaries."""
    t = transcript(path, start, end)
    gap_needed = PAUSE_SECONDS[sensitivity]
    out: list[Moment] = []
    prev_end = t.get("start") or 0.0
    for seg in t["segments"]:
        gap = seg["start"] - prev_end
        if gap >= gap_needed:
            preview = seg["text"][:80]
            out.append(Moment(time=seg["start"], score=min(1.0, gap / 6.0), source="transcript",
                              note=f"speech resumes after {gap:.1f}s pause: \"{preview}\""))
        prev_end = seg["end"]
    return out
