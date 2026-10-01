"""probe / export / reel / LosslessCut tools."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer

from clipper.core import segfile
from clipper.core.export import ExportSettings, cached_probe, export_clips
from clipper.core.paths import ensure_not_source, resolve_input, resolve_output_dir, resolve_output_file
from clipper.core.reel import make_highlight_reel
from clipper.core.segments import format_time
from clipper.tools.common import ProgressFn, SegmentIn, run_blocking, user_errors

# --- sync implementations (shared with the job runner) -----------------------------------


def do_probe(path: str) -> dict[str, Any]:
    src = resolve_input(path)
    info = cached_probe(src)
    out = info.summary()
    out["duration_hms"] = format_time(info.duration)
    if info.is_vfr:
        out["note"] = "variable frame rate: 'smart' mode will fall back to 'reencode'"
    return out


def do_export(path: str, segments: list[SegmentIn], output_dir: str | None = None,
              mode: Literal["copy", "reencode", "smart"] = "copy", pad_before: float = 0.0,
              pad_after: float = 0.0, merge_gap: float | None = 0.0, min_length: float = 0.1,
              dry_run: bool = False, progress: ProgressFn | None = None) -> dict[str, Any]:
    src = resolve_input(path)
    if not segments:
        raise ValueError("segments is empty")
    segs = [SegmentIn.model_validate(s).to_segment() for s in segments]
    out_dir = resolve_output_dir(src, output_dir)
    settings = ExportSettings(mode=mode, pad_before=pad_before, pad_after=pad_after,
                              merge_gap=merge_gap, min_length=min_length)
    return export_clips(src, segs, out_dir, settings, dry_run=dry_run, progress=progress)


def do_reel(clips: list[str], output: str, transition: Literal["none", "fade", "crossfade"] = "none",
            transition_duration: float = 0.5, progress: ProgressFn | None = None) -> dict[str, Any]:
    paths = [resolve_input(c) for c in clips]
    out = resolve_output_file(output)
    ensure_not_source(out, *paths)
    return make_highlight_reel(paths, out, transition, transition_duration, progress)


# --- MCP registration ---------------------------------------------------------------------


def register(mcp: MCPServer) -> None:
    @mcp.tool()
    @user_errors
    def probe_video(path: str) -> dict[str, Any]:
        """Duration, container, codecs, resolution, fps and stream counts of a local video.

        Args:
            path: Absolute path to a video file inside the allowed directories.
        """
        return do_probe(path)

    @mcp.tool(name="export_clips")
    @user_errors
    async def export_clips_tool(
        path: str,
        segments: list[SegmentIn],
        ctx: Context,
        output_dir: str | None = None,
        mode: Literal["copy", "reencode", "smart"] = "copy",
        pad_before: float = 0.0,
        pad_after: float = 0.0,
        merge_gap: float | None = 0.0,
        min_length: float = 0.1,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Cut segments out of a video into separate clip files.

        Segments are padded, clamped to the video, overlapping/nearby ones merged, and too-short
        ones dropped. One failing clip never aborts the batch: see `failed` in the result.
        A `<video>.manifest.json` with requested vs. actual times is written to the output dir.

        Args:
            path: Absolute path to the source video.
            segments: [{start, end, label?}] with times in seconds or 'HH:MM:SS.ms'.
            output_dir: Defaults to OUTPUT_DIR or '<video dir>/clips'. Relative = next to the video.
            mode: 'copy' = lossless, fast, start snaps back to a keyframe (reported per clip).
                  'reencode' = frame-accurate, slower, lossy (CRF 18).
                  'smart' = frame-accurate; re-encodes only up to the first keyframe, copies the rest.
            pad_before: Seconds added before each segment.
            pad_after: Seconds added after each segment.
            merge_gap: Merge segments closer than this many seconds; null disables merging.
            min_length: Drop segments shorter than this after processing.
            dry_run: Return the final processed segment list without cutting anything.
        """
        return await run_blocking(ctx, lambda p: do_export(
            path, segments, output_dir, mode, pad_before, pad_after, merge_gap, min_length, dry_run, p))

    @mcp.tool(name="make_highlight_reel")
    @user_errors
    async def make_highlight_reel_tool(
        clips: list[str],
        output: str,
        ctx: Context,
        transition: Literal["none", "fade", "crossfade"] = "none",
        transition_duration: float = 0.5,
    ) -> dict[str, Any]:
        """Join clips (in the given order) into one video.

        Stream-copies when all clips share a format and no transition is requested; otherwise
        re-encodes to the first clip's resolution and frame rate.

        Args:
            clips: Absolute paths of clips, in playback order.
            output: Absolute path of the reel to write.
            transition: 'none', 'fade' (fade through black at each cut) or 'crossfade'.
            transition_duration: Seconds per fade / crossfade.
        """
        return await run_blocking(ctx, lambda p: do_reel(clips, output, transition, transition_duration, p))

    @mcp.tool()
    @user_errors
    def export_segments_file(
        path: str,
        segments: list[SegmentIn],
        format: Literal["llc", "csv"] = "llc",
        output: str | None = None,
    ) -> dict[str, Any]:
        """Write proposed cuts as a LosslessCut project (.llc) or segment CSV for manual review.

        Opening the video in LosslessCut auto-loads '<name>-proj.llc' from the same folder.

        Args:
            path: Absolute path to the video the segments refer to.
            segments: [{start, end, label?}].
            format: 'llc' (LosslessCut project) or 'csv' (start,end,name rows).
            output: Where to write; defaults to next to the video.
        """
        src = resolve_input(path)
        segs = [s.to_segment() for s in segments]
        dst = resolve_output_file(output) if output else resolve_output_file(segfile.default_path(src, format))
        ensure_not_source(dst, src)
        segfile.write(dst, src, segs, format)
        return {"written": str(dst), "segments": len(segs), "format": format}

    @mcp.tool()
    @user_errors
    def import_segments_file(file: str) -> dict[str, Any]:
        """Read segments back from a LosslessCut .llc project or CSV (e.g. after manual edits).

        The returned `segments` can be passed straight to export_clips.

        Args:
            file: Absolute path to the .llc or .csv file.
        """
        p = resolve_input(file)
        segs, media = segfile.read(p)
        out: dict[str, Any] = {
            "segments": [{"start": round(s.start, 3), "end": round(s.end, 3), "label": s.label} for s in segs],
            "count": len(segs),
        }
        if media:
            out["media_file"] = str(p.with_name(media))
        return out

