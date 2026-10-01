"""Subprocess wrappers around ffmpeg / ffprobe."""

from __future__ import annotations

import subprocess

from clipper.config import get_settings


class FFmpegNotFoundError(RuntimeError):
    """ffmpeg or ffprobe is missing or not runnable."""


def _check_binary(name: str, path: str | None, env_var: str) -> str:
    if not path:
        raise FFmpegNotFoundError(
            f"{name} not found on PATH. Install it (e.g. `brew install ffmpeg`) "
            f"or set {env_var} to the binary's location."
        )
    try:
        result = subprocess.run(
            [path, "-version"], capture_output=True, text=True, timeout=10
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
