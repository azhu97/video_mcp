"""Subprocess wrappers around ffmpeg / ffprobe.

Every call uses list args (never ``shell=True``), ``-nostdin`` / ``stdin=DEVNULL`` so ffmpeg
can never swallow the MCP stdio channel, and raises a typed error carrying the stderr tail.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from bisect import bisect_right
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from clipper.config import get_settings

log = logging.getLogger(__name__)

# Above this the head of a smart cut is re-encoded; below it we treat start as "on" a keyframe.
KEYFRAME_EPS = 0.002


class FFmpegNotFoundError(RuntimeError):
    """ffmpeg or ffprobe is missing or not runnable."""


class FFmpegError(RuntimeError):
    """An ffmpeg/ffprobe invocation failed."""

    def __init__(self, cmd: list[str], returncode: int | None, stderr: str, reason: str = ""):
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        tail = "\n".join(stderr.strip().splitlines()[-15:])
        if not reason:
            if "No space left on device" in stderr:
                reason = "disk full (No space left on device)"
            elif "Invalid data found when processing input" in stderr or "moov atom not found" in stderr:
                reason = "input file is corrupted or not a media file"
            else:
                reason = f"exit code {returncode}"
        self.reason = reason
        super().__init__(f"{Path(cmd[0]).name} failed: {reason}\n{tail}")


def _check_binary(name: str, path: str | None, env_var: str) -> str:
    if not path:
        raise FFmpegNotFoundError(
            f"{name} not found on PATH. Install it (e.g. `brew install ffmpeg`) "
            f"or set {env_var} to the binary's location."
        )
    try:
        result = subprocess.run(
            [path, "-version"], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise FFmpegNotFoundError(f"{name} at {path!r} could not be run: {e}") from e
    if result.returncode != 0:
        raise FFmpegNotFoundError(
            f"{name} at {path!r} exited with {result.returncode}: {result.stderr.strip()}"
        )
    return result.stdout.splitlines()[0] if result.stdout else name


def check() -> tuple[str, str]:
    """Fail fast if ffmpeg/ffprobe aren't usable. Returns their version lines."""
    s = get_settings()
    return (
        _check_binary("ffmpeg", s.ffmpeg_path, "FFMPEG_PATH"),
        _check_binary("ffprobe", s.ffprobe_path, "FFPROBE_PATH"),
    )


def _bin(name: str) -> str:
    s = get_settings()
    path = s.ffmpeg_path if name == "ffmpeg" else s.ffprobe_path
    if not path:
        check()  # raises with a helpful message
    return path  # type: ignore[return-value]


def run(cmd: list[str], timeout: float | None = 600) -> subprocess.CompletedProcess[str]:
    """Run a command, raising FFmpegError on failure or timeout."""
    log.debug("run: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as e:
        stderr = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        raise FFmpegError(cmd, None, stderr, reason=f"timed out after {timeout:.0f}s") from e
    except OSError as e:
        raise FFmpegError(cmd, None, str(e), reason=str(e)) from e
    if result.returncode != 0:
        raise FFmpegError(cmd, result.returncode, result.stderr)
    return result


def ffmpeg(*args: str, timeout: float | None = 600, loglevel: str = "error") -> subprocess.CompletedProcess[str]:
    return run([_bin("ffmpeg"), "-hide_banner", "-nostdin", "-v", loglevel, *args], timeout=timeout)


def ffprobe(*args: str, timeout: float | None = 120) -> subprocess.CompletedProcess[str]:
    return run([_bin("ffprobe"), "-v", "error", *args], timeout=timeout)


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


@dataclass
class StreamInfo:
    index: int
    type: str
    codec: str
    profile: str | None = None
    language: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    pix_fmt: str | None = None
    level: int | None = None
    time_base: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    channel_layout: str | None = None


@dataclass
class VideoInfo:
    path: str
    duration: float
    container: str
    size_bytes: int
    bitrate: int | None
    streams: list[StreamInfo] = field(default_factory=list)

    @property
    def video(self) -> StreamInfo | None:
        return next((s for s in self.streams if s.type == "video"), None)

    @property
    def audio(self) -> StreamInfo | None:
        return next((s for s in self.streams if s.type == "audio"), None)

    @property
    def is_vfr(self) -> bool:
        return bool(self.video and self.video.fps is None)

    def summary(self) -> dict[str, Any]:
        v, a = self.video, self.audio
        return {
            "path": self.path,
            "duration": round(self.duration, 3),
            "container": self.container,
            "size_bytes": self.size_bytes,
            "bitrate": self.bitrate,
            "video": {
                "codec": v.codec,
                "profile": v.profile,
                "resolution": f"{v.width}x{v.height}",
                "fps": v.fps,
                "pix_fmt": v.pix_fmt,
            }
            if v
            else None,
            "audio": {"codec": a.codec, "sample_rate": a.sample_rate, "channels": a.channels} if a else None,
            "stream_counts": {
                t: sum(1 for s in self.streams if s.type == t)
                for t in ("video", "audio", "subtitle", "data")
                if any(s.type == t for s in self.streams)
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VideoInfo:
        streams = [StreamInfo(**s) for s in d.pop("streams", [])]
        return cls(**d, streams=streams)


def _rate(value: str | None) -> float | None:
    if not value or value in ("0/0", "0"):
        return None
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def probe(path: str | Path) -> VideoInfo:
    out = ffprobe("-print_format", "json", "-show_format", "-show_streams", str(path)).stdout
    data = json.loads(out)
    fmt = data.get("format", {})
    streams: list[StreamInfo] = []
    for s in data.get("streams", []):
        stype = s.get("codec_type", "unknown")
        if stype == "video" and s.get("disposition", {}).get("attached_pic"):
            stype = "attachment"  # cover art, not a real video stream
        fps = None
        if stype == "video":
            r, avg = _rate(s.get("r_frame_rate")), _rate(s.get("avg_frame_rate"))
            # r_frame_rate != avg_frame_rate is the usual VFR tell; report None for VFR.
            fps = r if r and avg and abs(r - avg) / r < 0.01 else (None if r and avg else (r or avg))
            if fps is not None:
                fps = round(fps, 3)
        streams.append(
            StreamInfo(
                index=s.get("index", len(streams)),
                type=stype,
                codec=s.get("codec_name", "unknown"),
                profile=s.get("profile"),
                language=(s.get("tags") or {}).get("language"),
                width=s.get("width"),
                height=s.get("height"),
                fps=fps,
                pix_fmt=s.get("pix_fmt"),
                level=_int(s.get("level")),
                time_base=s.get("time_base"),
                sample_rate=_int(s.get("sample_rate")),
                channels=s.get("channels"),
                channel_layout=s.get("channel_layout"),
            )
        )
    duration = float(fmt.get("duration") or 0.0)
    if not duration:
        durations = [float(s["duration"]) for s in data.get("streams", []) if s.get("duration")]
        duration = max(durations, default=0.0)
    return VideoInfo(
        path=str(path),
        duration=duration,
        container=fmt.get("format_name", "unknown"),
        size_bytes=_int(fmt.get("size")) or Path(path).stat().st_size,
        bitrate=_int(fmt.get("bit_rate")),
        streams=streams,
    )


# ---------------------------------------------------------------------------
# keyframes
# ---------------------------------------------------------------------------


def keyframes(path: str | Path, timeout: float | None = 1800) -> list[float]:
    """Keyframe timestamps of the first video stream (seconds, sorted).

    Reads packet flags instead of decoding (``-skip_frame nokey``), which is much faster on
    long files. Returns [] for audio-only files. Callers should go through the cache.
    """
    out = ffprobe(
        "-select_streams", "v:0", "-show_entries", "packet=pts_time,flags",
        "-of", "csv=p=0", str(path), timeout=timeout,
    ).stdout
    times: list[float] = []
    for line in out.splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 2 and "K" in parts[1] and parts[0] not in ("", "N/A"):
            times.append(float(parts[0]))
    times.sort()
    if times:
        # Normalize so the stream starts at 0 like the cut outputs do.
        base = min(0.0, times[0])
        times = [t - base for t in times]
    return times


def keyframe_at_or_before(kfs: list[float], t: float) -> float:
    i = bisect_right(kfs, t + KEYFRAME_EPS)
    return kfs[i - 1] if i else 0.0


def keyframe_at_or_after(kfs: list[float], t: float) -> float | None:
    i = bisect_right(kfs, t - KEYFRAME_EPS)
    return kfs[i] if i < len(kfs) else None


# ---------------------------------------------------------------------------
# cutting
# ---------------------------------------------------------------------------


def _stream_maps(info: VideoInfo, keep_subtitles: bool = True) -> list[str]:
    maps = ["-map", "0:v?", "-map", "0:a?"]
    if keep_subtitles and any(s.type == "subtitle" for s in info.streams):
        maps += ["-map", "0:s?"]
    # Drop cover art / data streams: they routinely break stream copy into mp4.
    return maps + ["-map", "-0:d?", "-map_metadata", "0"]


def _cut_timeout(duration: float, reencode: bool) -> float:
    return (120 + 20 * duration) if reencode else (60 + 2 * duration)


def cut_copy(
    src: str | Path,
    start: float,
    end: float,
    dst: str | Path,
    info: VideoInfo | None = None,
    kfs: list[float] | None = None,
) -> tuple[float, float]:
    """Lossless stream-copy cut. Returns the clip's (actual_start, actual_end) in source time.

    The start snaps back to the nearest keyframe at or before ``start``.
    """
    info = info or probe(src)
    dur = end - start

    def attempt(keep_subtitles: bool) -> None:
        ffmpeg(
            "-ss", f"{start:.6f}", "-i", str(src), "-t", f"{dur:.6f}",
            *_stream_maps(info, keep_subtitles), "-c", "copy", "-avoid_negative_ts", "make_zero",
            "-y", str(dst), timeout=_cut_timeout(dur, False),
        )

    try:
        attempt(keep_subtitles=True)
    except FFmpegError as e:
        # Some subtitle codecs can't be stream-copied into the target container; drop them.
        if any(s.type == "subtitle" for s in info.streams) and "subtitle" in e.stderr.lower():
            attempt(keep_subtitles=False)
        else:
            raise
    if info.video is None:
        return start, end  # audio packets are all "keyframes"
    actual_start = keyframe_at_or_before(kfs, start) if kfs is not None else start
    actual_dur = probe(dst).duration
    return actual_start, min(actual_start + actual_dur, info.duration)


# Encoder choice for re-encoded segments, keyed by output suffix.
_AUDIO_ENC = {".webm": ["-c:a", "libopus", "-b:a", "160k"], ".ogv": ["-c:a", "libvorbis"]}
_VIDEO_ENC = {
    ".webm": ["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", "-row-mt", "1"],
    ".ogv": ["-c:v", "libtheora", "-q:v", "8"],
}


def _encoder_args(dst: Path, info: VideoInfo, crf: int) -> list[str]:
    suffix = dst.suffix.lower()
    args: list[str] = []
    if info.video:
        if suffix in _VIDEO_ENC:
            args += _VIDEO_ENC[suffix]
        else:
            args += ["-c:v", "libx264", "-crf", str(crf), "-preset", "veryfast"]
            pix = info.video.pix_fmt or "yuv420p"
            args += ["-pix_fmt", pix if pix in ("yuv420p", "yuvj420p", "yuv422p", "yuv444p") else "yuv420p"]
    if info.audio:
        args += _AUDIO_ENC.get(suffix, ["-c:a", "aac", "-b:a", "192k"])
    if suffix in (".mp4", ".mov", ".m4v"):
        args += ["-movflags", "+faststart"]
    return args


def cut_reencode(
    src: str | Path, start: float, end: float, dst: str | Path, info: VideoInfo | None = None, crf: int = 18
) -> tuple[float, float]:
    """Frame-accurate cut by re-encoding. Input-side -ss is frame-accurate when transcoding."""
    info = info or probe(src)
    dst = Path(dst)
    dur = end - start
    ffmpeg(
        "-ss", f"{start:.6f}", "-i", str(src), "-t", f"{dur:.6f}",
        "-map", "0:v:0?", "-map", "0:a?", "-map_metadata", "0",
        *_encoder_args(dst, info, crf), "-y", str(dst),
        timeout=_cut_timeout(dur, True),
    )
    return start, start + dur


# --- smart cut --------------------------------------------------------------

_H264_PROFILES = {
    "constrained baseline": "baseline",
    "baseline": "baseline",
    "main": "main",
    "high": "high",
    "high 10": "high10",
    "high 4:2:2": "high422",
    "high 4:4:4 predictive": "high444",
}
_HEVC_PROFILES = {"main": "main", "main 10": "main10", "rext": None}


class SmartCutUnsupported(Exception):
    pass


def _matching_video_encoder(info: VideoInfo) -> list[str]:
    v = info.video
    if v is None:
        raise SmartCutUnsupported("no video stream")
    if v.fps is None:
        raise SmartCutUnsupported("variable frame rate source")
    common = ["-pix_fmt", v.pix_fmt or "yuv420p", "-r", str(v.fps)]
    if v.codec == "h264":
        profile = _H264_PROFILES.get((v.profile or "").lower())
        if not profile:
            raise SmartCutUnsupported(f"unsupported h264 profile {v.profile!r}")
        args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-profile:v", profile]
        if v.level and v.level > 0:
            args += ["-level:v", f"{v.level / 10:.1f}"]
        return args + common
    if v.codec == "hevc":
        profile = _HEVC_PROFILES.get((v.profile or "").lower())
        if not profile:
            raise SmartCutUnsupported(f"unsupported hevc profile {v.profile!r}")
        return ["-c:v", "libx265", "-preset", "veryfast", "-crf", "18", "-profile:v", profile,
                "-x265-params", "log-level=error", "-tag:v", "hvc1", *common]
    raise SmartCutUnsupported(f"smart cut not supported for codec {v.codec!r}")


def verify_decodes(path: str | Path, timeout: float = 300) -> None:
    """Full decode pass; raises FFmpegError if the decoder reports any error."""
    res = ffmpeg("-xerror", "-i", str(path), "-f", "null", "-", timeout=timeout)
    if res.stderr.strip():
        raise FFmpegError([_bin("ffmpeg")], 0, res.stderr, reason="decode errors in output")


def cut_smart(
    src: str | Path,
    start: float,
    end: float,
    dst: str | Path,
    info: VideoInfo | None = None,
    kfs: list[float] | None = None,
) -> tuple[float, float]:
    """Re-encode [start, next keyframe), stream-copy [keyframe, end), join, verify.

    Raises SmartCutUnsupported when the source can't be matched; callers fall back to
    re-encode.
    """
    info = info or probe(src)
    dst = Path(dst)
    if info.video is None:
        return cut_copy(src, start, end, dst, info=info)
    kfs = kfs if kfs is not None else keyframes(src)
    kf = keyframe_at_or_after(kfs, start)
    if kf is not None and abs(kf - start) <= KEYFRAME_EPS:
        return cut_copy(src, start, end, dst, info=info, kfs=kfs)
    venc = _matching_video_encoder(info)
    if kf is None or kf >= end:
        raise SmartCutUnsupported("no keyframe inside the clip; nothing to stream-copy")
    v = info.video
    ts = ["-video_track_timescale", Fraction(v.time_base).denominator.__str__()] if v.time_base else []
    half_frame = 0.5 / v.fps if v.fps else 0.001
    dur = end - start
    timeout = _cut_timeout(dur, True)

    bsf = "h264_mp4toannexb" if v.codec == "h264" else "hevc_mp4toannexb"
    with tempfile.TemporaryDirectory(prefix="clipper-smart-") as tmpdir:
        tmp = Path(tmpdir)
        head, tail, joined, audio = tmp / "head.ts", tmp / "tail.ts", tmp / f"video{dst.suffix}", tmp / "audio.mka"
        # Head: exact start up to (not including) the keyframe, re-encoded with matching params.
        ffmpeg(
            "-ss", f"{start:.6f}", "-i", str(src), "-t", f"{kf - start - half_frame / 2:.6f}",
            "-map", "0:v:0", "-an", *venc, "-bsf:v", bsf, "-f", "mpegts", "-y", str(head),
            timeout=timeout,
        )
        # Tail: stream copy from the keyframe. Seeking a hair past it still lands on it.
        ffmpeg(
            "-ss", f"{kf + half_frame / 4:.6f}", "-i", str(src), "-t", f"{end - kf:.6f}",
            "-map", "0:v:0", "-an", "-frames:v", str(round((end - kf) * v.fps)), "-c:v", "copy", "-bsf:v", bsf, "-f", "mpegts", "-y", str(tail),
            timeout=timeout,
        )
        listfile = tmp / "list.txt"
        listfile.write_text(f"file '{head.name}'\nfile '{tail.name}'\n")
        ffmpeg("-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy", *ts, "-y", str(joined),
               timeout=timeout)
        maps = ["-map", "0:v:0"]
        if info.audio:
            # Audio is cheap: re-encode the whole span for sample-accurate sync with the video.
            ffmpeg("-ss", f"{start:.6f}", "-i", str(src), "-t", f"{dur:.6f}", "-map", "0:a?", "-vn",
                   *_AUDIO_ENC.get(dst.suffix.lower(), ["-c:a", "aac", "-b:a", "192k"]),
                   "-y", str(audio), timeout=timeout)
            maps += ["-map", "1:a?"]
        inputs = ["-i", str(joined)] + (["-i", str(audio)] if info.audio else [])
        ffmpeg(*inputs, *maps, "-c", "copy", *ts,
               *(["-movflags", "+faststart"] if dst.suffix.lower() in (".mp4", ".mov", ".m4v") else []),
               "-y", str(dst), timeout=timeout)

    out = probe(dst)
    if out.video is None or abs(out.duration - dur) > max(0.1, 2 / (v.fps or 25)):
        raise SmartCutUnsupported(f"output duration {out.duration:.3f}s != requested {dur:.3f}s")
    try:
        verify_decodes(dst, timeout=timeout)
    except FFmpegError as e:
        raise SmartCutUnsupported(f"joined stream failed verification: {e.reason}") from e
    return start, end


# ---------------------------------------------------------------------------
# frames, audio extraction, concat
# ---------------------------------------------------------------------------


def extract_frame(src: str | Path, t: float, dst: str | Path, width: int = 640, crop: str | None = None) -> None:
    vf = f"{crop},scale={width}:-2:flags=neighbor" if crop else f"scale={width}:-2"
    ffmpeg("-ss", f"{t:.6f}", "-i", str(src), "-frames:v", "1", "-map", "0:v:0",
           "-vf", vf, "-q:v", "4", "-y", str(dst), timeout=60)
    if not Path(dst).exists():
        raise FFmpegError([_bin("ffmpeg")], 0, "", reason=f"no frame at {t:.3f}s (past end of video?)")


def tile_images(images: list[Path], dst: Path, cols: int) -> None:
    rows = -(-len(images) // cols)
    with tempfile.TemporaryDirectory(prefix="clipper-tile-") as tmpdir:
        for i, img in enumerate(images):
            shutil.copy(img, Path(tmpdir) / f"f{i:03d}.jpg")
        ffmpeg("-framerate", "1", "-i", str(Path(tmpdir) / "f%03d.jpg"),
               "-vf", f"tile={cols}x{rows}:padding=4:margin=4", "-frames:v", "1", "-q:v", "4",
               "-y", str(dst), timeout=60)


def extract_audio_pcm(src: str | Path, dst: str | Path, start: float | None = None,
                      end: float | None = None) -> None:
    args: list[str] = []
    if start:
        args += ["-ss", f"{start:.6f}"]
    args += ["-i", str(src)]
    if end is not None:
        args += ["-t", f"{end - (start or 0):.6f}"]
    ffmpeg(*args, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-y", str(dst),
           timeout=3600)
