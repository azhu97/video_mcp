"""Path safety: every file the server reads or writes must live inside ALLOWED_DIRS."""

from __future__ import annotations

from pathlib import Path

from clipper.config import Settings, get_settings


class PathNotAllowedError(PermissionError):
    pass


def _check_allowed(p: Path, settings: Settings) -> None:
    if not settings.allowed_dirs:
        raise PathNotAllowedError(
            "No ALLOWED_DIRS configured; set ALLOWED_DIRS to the folders the server may access."
        )
    if not any(p == d or p.is_relative_to(d) for d in settings.allowed_dirs):
        allowed = ", ".join(str(d) for d in settings.allowed_dirs)
        raise PathNotAllowedError(f"{p} is outside the allowed directories ({allowed}).")


def resolve_input(path: str | Path, settings: Settings | None = None) -> Path:
    """Resolve (killing ``..`` and symlinks) and require an existing file inside ALLOWED_DIRS."""
    settings = settings or get_settings()
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise PathNotAllowedError(f"Path must be absolute: {path}")
    p = p.resolve()
    _check_allowed(p, settings)
    if not p.exists():
        raise FileNotFoundError(f"No such file: {p}")
    if not p.is_file():
        raise IsADirectoryError(f"Not a file: {p}")
    return p


def resolve_output_dir(src: Path, output_dir: str | Path | None = None,
                       settings: Settings | None = None) -> Path:
    """Explicit dir > OUTPUT_DIR setting > ``<video_dir>/clips``. Must be inside ALLOWED_DIRS."""
    settings = settings or get_settings()
    if output_dir:
        out = Path(output_dir).expanduser()
        if not out.is_absolute():
            out = src.parent / out
    else:
        out = settings.output_dir or src.parent / "clips"
    out = out.resolve()
    _check_allowed(out, settings)
    out.mkdir(parents=True, exist_ok=True)
    return out


def resolve_output_file(path: str | Path, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise PathNotAllowedError(f"Path must be absolute: {path}")
    p = p.parent.resolve() / p.name
    _check_allowed(p, settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def ensure_not_source(dst: Path, *sources: Path) -> None:
    for src in sources:
        if dst.resolve() == src.resolve():
            raise PathNotAllowedError(f"Refusing to overwrite the source file {src}")
