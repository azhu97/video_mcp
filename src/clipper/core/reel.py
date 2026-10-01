"""Join clips into a highlight reel."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from clipper.core import ffmpeg

Transition = Literal["none", "fade", "crossfade"]


def _signature(info: ffmpeg.VideoInfo) -> tuple:
    v, a = info.video, info.audio
    return (
        (v.codec, v.width, v.height, v.pix_fmt, v.fps, v.profile) if v else None,
        (a.codec, a.sample_rate, a.channels) if a else None,
    )


def make_highlight_reel(
    clips: list[Path],
    output: Path,
    transition: Transition = "none",
    transition_duration: float = 0.5,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """Concatenate ``clips``. Stream copy when all clips match and no transition is asked for;
    otherwise re-encode to the first clip's resolution/fps with optional fades or crossfades."""
    if len(clips) < 1:
        raise ValueError("Need at least one clip")
    infos = [ffmpeg.probe(c) for c in clips]
    for c, i in zip(clips, infos, strict=False):
        if i.video is None:
            raise ValueError(f"{c.name} has no video stream")
    same = len({_signature(i) for i in infos}) == 1
    if progress:
        progress(0, 1, "joining")
    if transition == "none" and same:
        _concat_copy(clips, output, sum(i.duration for i in infos))
        method = "copy"
    else:
        _concat_reencode(clips, infos, output, transition, transition_duration)
        method = "reencode"
    if progress:
        progress(1, 1, "done")
    out = ffmpeg.probe(output)
    return {"output": str(output), "duration": round(out.duration, 3), "clips": len(clips), "method": method,
            **({} if same else {"note": "clips differed in format; re-encoded to match the first clip"})}


def _concat_copy(clips: list[Path], output: Path, total: float) -> None:
    with tempfile.TemporaryDirectory(prefix="clipper-reel-") as tmp:
        listfile = Path(tmp) / "list.txt"
        listfile.write_text("".join(f"file '{_escape(c)}'\n" for c in clips))
        ffmpeg.ffmpeg("-f", "concat", "-safe", "0", "-i", str(listfile), "-map", "0:v?", "-map", "0:a?",
                      "-c", "copy", *_faststart(output), "-y", str(output), timeout=120 + 2 * total)


def _escape(p: Path) -> str:
    return str(p.resolve()).replace("'", "'\\''")


def _faststart(output: Path) -> list[str]:
    return ["-movflags", "+faststart"] if output.suffix.lower() in (".mp4", ".mov", ".m4v") else []


def _concat_reencode(clips: list[Path], infos: list[ffmpeg.VideoInfo], output: Path,
                     transition: Transition, td: float) -> None:
    first = infos[0].video
    assert first is not None
    w, h = first.width or 1280, first.height or 720
    fps = first.fps or 30
    durs = [i.duration for i in infos]
    if transition != "none" and any(d <= 2 * td for d in durs):
        raise ValueError(f"Every clip must be longer than {2 * td:.1f}s for a {td}s {transition}")
    inputs: list[str] = []
    chains: list[str] = []
    for k, (c, info) in enumerate(zip(clips, infos, strict=False)):
        inputs += ["-i", str(c)]
        v = (f"[{k}:v:0]scale={w}:{h}:force_original_aspect_ratio=decrease,"
             f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p,settb=AVTB")
        a = (f"[{k}:a:0]aformat=sample_rates=48000:channel_layouts=stereo,asetpts=PTS-STARTPTS"
             if info.audio else
             f"anullsrc=r=48000:cl=stereo,atrim=duration={durs[k]:.3f}")
        if transition == "fade":
            v += f",fade=t=in:d={td},fade=t=out:st={durs[k] - td:.3f}:d={td}"
            a += f",afade=t=in:d={td},afade=t=out:st={durs[k] - td:.3f}:d={td}"
        chains += [f"{v}[v{k}]", f"{a}[a{k}]"]
    n = len(clips)
    if transition == "crossfade" and n > 1:
        offset = 0.0
        vprev, aprev = "v0", "a0"
        for k in range(1, n):
            offset += durs[k - 1] - td
            chains.append(f"[{vprev}][v{k}]xfade=transition=fade:duration={td}:offset={offset:.3f}[vx{k}]")
            chains.append(f"[{aprev}][a{k}]acrossfade=d={td}[ax{k}]")
            vprev, aprev = f"vx{k}", f"ax{k}"
        vout, aout = vprev, aprev
    else:
        chains.append("".join(f"[v{k}][a{k}]" for k in range(n)) + f"concat=n={n}:v=1:a=1[vout][aout]")
        vout, aout = "vout", "aout"
    ffmpeg.ffmpeg(
        *inputs, "-filter_complex", ";".join(chains), "-map", f"[{vout}]", "-map", f"[{aout}]",
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-c:a", "aac", "-b:a", "192k",
        *_faststart(output), "-y", str(output), timeout=300 + 20 * sum(durs),
    )
