"""Settings loaded from environment variables and an optional config.toml.

Precedence: environment variables > config.toml > built-in defaults.

Environment variables:
    ALLOWED_DIRS     Directories the server may read, separated by ``os.pathsep`` (":")
                     or commas. Required for the MCP server to touch any file.
    OUTPUT_DIR       Where clips go. Default: ``<video_dir>/clips/``.
    FFMPEG_PATH      ffmpeg binary. Default: resolved via ``shutil.which``.
    FFPROBE_PATH     ffprobe binary. Default: resolved via ``shutil.which``.
    CLIPPER_CACHE    Analysis cache root. Default: ``~/.cache/clipper``.
    CLIPPER_LOG      Log file. Default: ``<cache>/clipper.log``.
    CLIPPER_CONFIG   Path to a config.toml. Default: ``~/.config/clipper/config.toml``.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("~/.config/clipper/config.toml").expanduser()

# Per-footage-type defaults used by prompts and `find_key_moments(preset=...)`.
DEFAULT_PRESETS: dict[str, dict[str, Any]] = {
    "sports": {
        "methods": ["audio", "scenes"],
        "sensitivity": "medium",
        "pad_before": 4.0,
        "pad_after": 3.0,
        "merge_gap": 2.0,
        "min_length": 3.0,
    },
    "lecture": {
        "methods": ["transcript", "scenes"],
        "sensitivity": "low",
        "pad_before": 2.0,
        "pad_after": 2.0,
        "merge_gap": 5.0,
        "min_length": 5.0,
    },
    "stream": {
        "methods": ["audio"],
        "sensitivity": "high",
        "pad_before": 8.0,
        "pad_after": 4.0,
        "merge_gap": 3.0,
        "min_length": 4.0,
    },
    "game_clock": {
        "methods": ["clock"],
        "pad_before": 0.0,
        "pad_after": 0.0,
        "merge_gap": 0.0,
        "min_length": 1.0,
    },
}


def _split_dirs(value: str) -> list[str]:
    parts: list[str] = []
    for chunk in value.split(os.pathsep):
        parts.extend(p for p in chunk.split(",") if p.strip())
    return [p.strip() for p in parts]


@dataclass(frozen=True)
class Settings:
    allowed_dirs: tuple[Path, ...] = ()
    output_dir: Path | None = None
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    cache_dir: Path = field(default_factory=lambda: Path("~/.cache/clipper").expanduser())
    log_file: Path | None = None
    max_parallel: int = 3
    presets: dict[str, dict[str, Any]] = field(default_factory=lambda: dict(DEFAULT_PRESETS))
    whisper_model: str = "small"
    # Named scoreboard regions for clock detection, e.g. {"synergy_shot_clock": "1001,1010,26,20"}.
    clock_regions: dict[str, str] = field(default_factory=dict)

    @property
    def resolved_log_file(self) -> Path:
        return self.log_file or self.cache_dir / "clipper.log"


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    cfg_path = Path(env.get("CLIPPER_CONFIG", DEFAULT_CONFIG_PATH)).expanduser()
    toml = _load_toml(cfg_path)

    def pick(env_key: str, toml_key: str) -> Any:
        if env.get(env_key):
            return env[env_key]
        return toml.get(toml_key)

    raw_dirs = pick("ALLOWED_DIRS", "allowed_dirs") or []
    if isinstance(raw_dirs, str):
        raw_dirs = _split_dirs(raw_dirs)
    allowed = tuple(Path(d).expanduser().resolve() for d in raw_dirs)

    out = pick("OUTPUT_DIR", "output_dir")
    cache = pick("CLIPPER_CACHE", "cache_dir")
    log = pick("CLIPPER_LOG", "log_file")

    presets = dict(DEFAULT_PRESETS)
    for name, values in (toml.get("presets") or {}).items():
        presets[name] = {**presets.get(name, {}), **values}

    return Settings(
        allowed_dirs=allowed,
        output_dir=Path(out).expanduser().resolve() if out else None,
        ffmpeg_path=pick("FFMPEG_PATH", "ffmpeg_path") or shutil.which("ffmpeg"),
        ffprobe_path=pick("FFPROBE_PATH", "ffprobe_path") or shutil.which("ffprobe"),
        cache_dir=Path(cache).expanduser() if cache else Path("~/.cache/clipper").expanduser(),
        log_file=Path(log).expanduser() if log else None,
        max_parallel=int(pick("CLIPPER_MAX_PARALLEL", "max_parallel") or 3),
        presets=presets,
        whisper_model=str(pick("CLIPPER_WHISPER_MODEL", "whisper_model") or "small"),
        clock_regions={str(k): str(v) for k, v in (toml.get("clock_regions") or {}).items()},
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
