"""Detection, transcript and frame tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import anyio.to_thread
from mcp.server.mcpserver import Context, Image, MCPServer

from clipper.config import get_settings
from clipper.core import cache
from clipper.core.detect import clock, find_key_moments
from clipper.core.detect import frames as frames_mod
from clipper.core.detect import transcript as tr
from clipper.core.export import cached_probe
from clipper.core.paths import resolve_input
from clipper.core.segments import format_time, parse_time
from clipper.tools.clips import do_export
from clipper.tools.common import ProgressFn, SegmentIn, opt_time, run_blocking, user_errors

# --- sync implementations (shared with the job runner) -----------------------------------


def do_find(path: str, methods: list[str] | None = None, max_results: int = 50,
            sensitivity: Literal["low", "medium", "high"] | None = None, start: float | str | None = None,
            end: float | str | None = None, preset: str | None = None, clock_region: str | None = None,
            progress: ProgressFn | None = None) -> dict[str, Any]:
    src = resolve_input(path)
    applied: dict[str, Any] = {}
    if preset:
        presets = get_settings().presets
        if preset not in presets:
            raise ValueError(f"Unknown preset {preset!r}; available: {sorted(presets)}")
        applied = presets[preset]
    methods = methods or applied.get("methods") or ["scenes", "audio"]
    sensitivity = sensitivity or applied.get("sensitivity") or "medium"
    clock_region = clock_region or applied.get("clock_region")
    result = find_key_moments(src, methods, max_results, sensitivity, opt_time(start), opt_time(end), progress,
                              clock_region=clock_region)
    result["video_id"] = cache.video_id(src)
    if preset:
        result["preset"] = {"name": preset, **applied}
        result["hint"] = ("Suggested export_clips settings: "
                          + ", ".join(f"{k}={applied[k]}" for k in ("pad_before", "pad_after", "merge_gap",
                                                                      "min_length") if k in applied))
    for c in result["candidates"]:
        c["hms"] = format_time(c["time"])
    return result


def do_clock(path: str, region: str, sensitivity: Literal["low", "medium", "high"] = "medium",
             start: float | str | None = None, end: float | str | None = None, min_duration: float = 3.0,
             export: bool = False, output_dir: str | None = None,
             mode: Literal["copy", "reencode", "smart"] = "smart", pad_before: float = 0.0,
             pad_after: float = 0.0, progress: ProgressFn | None = None) -> dict[str, Any]:
    src = resolve_input(path)
    v = cached_probe(src).video
    if v is None:
        raise ValueError("Video has no video stream")
    reg = clock.parse_region(region, v.width or 0, v.height or 0)
    spans = clock.find_running(src, reg, sensitivity, opt_time(start), opt_time(end), min_duration, progress)
    total = sum(s["duration"] for s in spans)
    out: dict[str, Any] = {
        "region": {"x": reg.x, "y": reg.y, "w": reg.w, "h": reg.h},
        "count": len(spans),
        "total_running_seconds": round(total, 1),
        "total_running_hms": format_time(total),
        # Compact [start, end] pairs keep a full game's worth of spans small.
        "spans": [[s["start"], s["end"]] for s in spans],
        "video_id": cache.video_id(src),
    }
    if not spans:
        out["hint"] = ("No running clock found. Check the region with get_frames(region=...): it should tightly "
                       "contain the clock digits. Try sensitivity='high'.")
    if export and spans:
        segs = [SegmentIn(start=s["start"], end=s["end"], label=f"clock {i}") for i, s in enumerate(spans, 1)]
        exported = do_export(path, segs, output_dir, mode, pad_before, pad_after, merge_gap=None,
                             min_length=min(1.0, min_duration), progress=progress)
        out["export"] = {"clips": len(exported["ok"]), "failed": exported["failed"],
                         "manifest": exported["manifest"],
                         "output_dir": str(Path(exported["manifest"]).parent)}
    return out


def do_transcribe(path: str, start: float | str | None = None, end: float | str | None = None,
                  offset: int = 0, limit: int = 100, include_words: bool = False,
                  progress: ProgressFn | None = None) -> dict[str, Any]:
    src = resolve_input(path)
    if cached_probe(src).audio is None:
        raise ValueError("Video has no audio stream to transcribe")

    def asr_progress(t: float, total: float) -> None:
        if progress and total:
            progress(int(t), int(total), "transcribing")

    t = tr.transcript(src, opt_time(start), opt_time(end), words=include_words, progress=asr_progress)
    out = tr.page(t, offset, max(1, min(limit, 500)), include_words)
    out["video_id"] = cache.video_id(src)
    if out["next_offset"] is not None:
        out["hint"] = (f"{out['total_segments']} segments total; call again with offset={out['next_offset']} "
                       "or use search_transcript instead of reading everything.")
    return out


def do_search(path: str, query: str, mode: Literal["keyword", "regex", "fuzzy"] = "keyword",
              max_results: int = 50, progress: ProgressFn | None = None) -> dict[str, Any]:
    src = resolve_input(path)
    t = tr.transcript(src, words=True)
    hits = tr.search(t, query, mode, max_results)
    for h in hits:
        h["hms"] = format_time(h["start"])
    return {"query": query, "mode": mode, "matches": hits, "count": len(hits)}


# --- MCP registration ---------------------------------------------------------------------


def register(mcp: MCPServer) -> None:
    @mcp.tool(name="find_key_moments")
    @user_errors
    async def find_key_moments_tool(
        path: str,
        ctx: Context,
        methods: list[Literal["scenes", "audio", "transcript", "clock"]] | None = None,
        max_results: int = 50,
        sensitivity: Literal["low", "medium", "high"] | None = None,
        start: float | str | None = None,
        end: float | str | None = None,
        preset: str | None = None,
        clock_region: str | None = None,
    ) -> dict[str, Any]:
        """Propose candidate key moments, scored 0-1 and sorted by time. Detection runs locally.

        Methods: 'scenes' (visual cuts), 'audio' (loudness spikes: crowds, goals, laughter),
        'transcript' (speech resuming after long pauses; needs faster-whisper, slow first run),
        'clock' (moments where an on-screen game/shot clock starts running; needs clock_region).
        Results are cached per video. You decide which candidates to keep; then call export_clips
        (pad around each moment) or get_frames to check them visually first.

        Args:
            path: Absolute path to the video.
            methods: Detectors to run (default ['scenes','audio'] or the preset's).
            max_results: Keep the highest-scoring N candidates.
            sensitivity: 'low' = only strong events, 'high' = more, weaker candidates.
            start: Only analyze from here (seconds or HH:MM:SS).
            end: Only analyze up to here.
            preset: Footage preset from config (e.g. 'sports', 'lecture', 'stream').
            clock_region: For 'clock': 'x,y,w,h' of the clock digits in source pixels, or a named region.
        """
        return await run_blocking(ctx, lambda p: do_find(
            path, list(methods) if methods else None, max_results, sensitivity, start, end, preset,
            clock_region, p))

    @mcp.tool(name="find_clock_running")
    @user_errors
    async def find_clock_running_tool(
        path: str,
        region: str,
        ctx: Context,
        sensitivity: Literal["low", "medium", "high"] = "medium",
        start: float | str | None = None,
        end: float | str | None = None,
        min_duration: float = 3.0,
        export: bool = False,
        output_dir: str | None = None,
        mode: Literal["copy", "reencode", "smart"] = "smart",
        pad_before: float = 0.0,
        pad_after: float = 0.0,
    ) -> dict[str, Any]:
        """Spans where an on-screen clock (shot clock / game clock) is running, i.e. live play.

        Works without OCR: a running clock changes its digits once per second, a stopped one is
        frozen. Returns [start, end] pairs. Optionally exports every span as its own clip.

        Finding the region: call get_frames on a moment of live play, locate the scoreboard, then
        call get_frames with region='x,y,w,h' (source pixels) and refine until the crop tightly
        contains just the clock digits (the shot clock is best for "shot clock ticking"; the game
        clock for "game clock running"). Pregame/halftime countdowns on the same scoreboard also
        tick: check the first spans and pass start= (tip-off) to skip them.

        First scan of a full game takes a few minutes (~60x real time) and is then cached; for an
        uncached multi-hour video prefer start_job(tool='find_clock_running', ...).

        Args:
            path: Absolute path to the video.
            region: 'x,y,w,h' of the clock digits in source pixels (or fractions of the frame), or a
                named region from the config's [clock_regions] table.
            sensitivity: 'high' if ticks are missed (thin digits / low contrast), 'low' if noisy.
            start: Only analyze from here (seconds or HH:MM:SS), e.g. tip-off.
            end: Only analyze up to here.
            min_duration: Drop spans shorter than this (seconds).
            export: Also cut every span into its own clip.
            output_dir: For export; defaults to '<video dir>/clips'.
            mode: For export; 'smart' (default) starts each clip on the exact frame.
            pad_before: For export; seconds added before each span.
            pad_after: For export; seconds added after each span.
        """
        return await run_blocking(ctx, lambda p: do_clock(
            path, region, sensitivity, start, end, min_duration, export, output_dir, mode, pad_before,
            pad_after, p))

    @mcp.tool(name="transcribe")
    @user_errors
    async def transcribe_tool(
        path: str,
        ctx: Context,
        start: float | str | None = None,
        end: float | str | None = None,
        offset: int = 0,
        limit: int = 100,
        include_words: bool = False,
    ) -> dict[str, Any]:
        """Timestamped transcript (local faster-whisper), paginated. Cached after the first run.

        For long videos prefer search_transcript; read pages only when you need full context.

        Args:
            path: Absolute path to the video.
            start: Only transcribe from here (seconds or HH:MM:SS).
            end: Only transcribe up to here.
            offset: Index of the first segment to return.
            limit: Segments per page (max 500).
            include_words: Include per-word timestamps.
        """
        return await run_blocking(ctx, lambda p: do_transcribe(path, start, end, offset, limit, include_words, p))

    @mcp.tool(name="search_transcript")
    @user_errors
    async def search_transcript_tool(
        path: str,
        query: str,
        ctx: Context,
        mode: Literal["keyword", "regex", "fuzzy"] = "keyword",
        max_results: int = 50,
    ) -> dict[str, Any]:
        """Find where something is said. Transcribes the whole video first if not cached.

        Args:
            path: Absolute path to the video.
            query: Words to find (keyword: all words must appear), a regex, or a phrase (fuzzy).
            mode: 'keyword', 'regex' (case-insensitive) or 'fuzzy' (tolerates transcription errors).
            max_results: Maximum matches to return.
        """
        return await run_blocking(ctx, lambda p: do_search(path, query, mode, max_results, p))

    @mcp.tool(structured_output=False)
    @user_errors
    async def get_frames(path: str, timestamps: list[float | str], width: int = 640,
                         region: str | None = None) -> list[Any]:
        """Still frames (JPEG) at the given times so you can look at candidate moments.

        Args:
            path: Absolute path to the video.
            timestamps: Up to 8 times (seconds or HH:MM:SS).
            width: Frame width in pixels (height keeps aspect ratio), 160-1280.
            region: Optional 'x,y,w,h' crop in source pixels (e.g. to find a scoreboard clock);
                the crop is enlarged so small digits are readable.
        """
        src = resolve_input(path)
        times = [parse_time(t) for t in timestamps]
        width = max(160, min(1280, width))
        v = cached_probe(src).video
        if v is None:
            raise ValueError("Video has no video stream")
        crop = clock.parse_region(region, v.width or 0, v.height or 0) if region else None
        shots = await anyio.to_thread.run_sync(
            lambda: frames_mod.frames(src, times, width, crop=crop.crop() if crop else None))
        if crop:
            size = f"crop x={crop.x} y={crop.y} w={crop.w} h={crop.h} of the {v.width}x{v.height} source"
        else:
            size = f"scaled to {width}px wide from {v.width}x{v.height} source pixels"
        out: list[Any] = []
        for t, jpeg in shots:
            out += [f"Frame at {format_time(t)} ({t:.3f}s), {size}", Image(data=jpeg, format="jpeg")]
        return out

    @mcp.tool(structured_output=False)
    @user_errors
    async def contact_sheet(path: str, start: float | str, end: float | str, n: int = 12) -> list[Any]:
        """One image tiling N evenly spaced frames from [start, end): a cheap overview of a span.

        Args:
            path: Absolute path to the video.
            start: Span start (seconds or HH:MM:SS).
            end: Span end.
            n: Number of frames (1-36), laid out left-to-right, top-to-bottom.
        """
        src = resolve_input(path)
        jpeg, times = await anyio.to_thread.run_sync(
            lambda: frames_mod.contact_sheet(src, parse_time(start), parse_time(end), n))
        labels = ", ".join(f"{i + 1}: {format_time(t)}" for i, t in enumerate(times))
        return [f"Contact sheet, tiles in reading order — {labels}", Image(data=jpeg, format="jpeg")]
