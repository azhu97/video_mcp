"""MCP resources (cached analysis) and prompts (common workflows)."""

from __future__ import annotations

import json

from mcp.server.mcpserver import MCPServer

from clipper.config import get_settings
from clipper.core import cache

# Short aliases → cache-file prefixes, so clients can ask for ".../transcript" without knowing
# which model or window produced it.
ALIASES = {"transcript": "transcript-", "scenes": "scenes-", "audio": "loudness-", "keyframes": "keyframes",
           "probe": "probe"}


def _resolve_name(video_id: str, name: str) -> str | None:
    d = cache.dir_for_id(video_id)
    if d is None:
        return None
    if (d / f"{name}.json").exists():
        return name
    prefix = ALIASES.get(name)
    if prefix is None:
        return None
    matches = sorted(d.glob(f"{prefix}*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    # Prefer whole-file analyses (no start/end window) over windowed ones.
    whole = [m for m in matches if m.stem.endswith("None-None")]
    best = (whole or matches or [None])[0]
    return best.stem if best else None


def register_resources(mcp: MCPServer) -> None:
    @mcp.resource("clipper://videos", mime_type="application/json")
    def videos() -> str:
        """Videos with cached analysis: video_id, path, and which analyses exist."""
        return json.dumps(cache.list_videos(), indent=2)

    @mcp.resource("clipper://analysis/{video_id}/{name}", mime_type="application/json")
    def analysis(video_id: str, name: str) -> str:
        """Cached analysis for a video: name is transcript, scenes, audio, keyframes, probe,
        or an exact cache entry listed by clipper://videos."""
        resolved = _resolve_name(video_id, name)
        data = cache.read(video_id, resolved) if resolved else None
        if data is None:
            raise ValueError(f"No cached {name!r} for video {video_id}; run the matching tool first.")
        return json.dumps(data)


_WORKFLOW = """\
Video: {path}

1. probe_video to get duration and format.
2. find_key_moments with preset="{preset}"{extra}.
3. Pick the strongest, non-redundant candidates (aim for about {count}). If unsure about any,
   call get_frames on them (up to 8 per call) and drop false positives.
4. export_clips with segments built around each kept moment and these settings:
   pad_before={pad_before}, pad_after={pad_after}, merge_gap={merge_gap}, min_length={min_length}.
   Use mode="smart" if clips must start exactly on the moment, otherwise the default "copy".
5. Report the exported files and any failures. Offer make_highlight_reel and
   export_segments_file (LosslessCut) as follow-ups.
"""


def _workflow(path: str, preset: str, count: int, extra: str = "") -> str:
    p = get_settings().presets.get(preset, {})
    return _WORKFLOW.format(path=path, preset=preset, count=count, extra=extra,
                            pad_before=p.get("pad_before", 1.5), pad_after=p.get("pad_after", 1.5),
                            merge_gap=p.get("merge_gap", 2.0), min_length=p.get("min_length", 2.0))


def register_prompts(mcp: MCPServer) -> None:
    @mcp.prompt()
    def sports_highlights(path: str, count: int = 10) -> str:
        """Find and export the biggest moments of a sports video (crowd noise + scene cuts)."""
        return ("Make sports highlights. Crowd/commentary loudness spikes are the best signal; "
                "scene cuts often mark replays.\n\n" + _workflow(path, "sports", count))

    @mcp.prompt()
    def lecture_key_points(path: str, count: int = 8) -> str:
        """Extract the key sections of a lecture or talk using the transcript."""
        return ("Extract the key points of this lecture. Use transcribe / search_transcript to "
                "understand the content; section boundaries tend to follow long pauses and slide "
                "changes. Give each clip a descriptive label.\n\n"
                + _workflow(path, "lecture", count, extra=" (transcription runs locally and may take a while)"))

    @mcp.prompt()
    def stream_best_moments(path: str, count: int = 15) -> str:
        """Find the best moments of a game stream / VOD (reactions, hype, loud moments)."""
        return ("Find the best moments of this stream. Loud reactions are the main signal; long "
                "streams can be analyzed in windows with start/end, or via start_job.\n\n"
                + _workflow(path, "stream", count))
