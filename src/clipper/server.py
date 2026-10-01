"""MCP server entry point (stdio). Never print to stdout: it is the protocol channel."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from mcp.server.mcpserver import MCPServer

from clipper import __version__
from clipper.config import get_settings
from clipper.core import ffmpeg
from clipper.tools import analysis, clips, jobs, resources

INSTRUCTIONS = """\
Local video clipper. Typical flow: probe_video → find_key_moments (or transcribe /
search_transcript) → optionally get_frames / contact_sheet to verify → export_clips →
optionally make_highlight_reel. All paths must be absolute and inside the configured allowed
directories. Times may be seconds or HH:MM:SS(.ms). For very long videos, use start_job."""


def build_server() -> MCPServer:
    mcp = MCPServer("clipper", version=__version__, instructions=INSTRUCTIONS)
    clips.register(mcp)
    analysis.register(mcp)
    jobs.register(mcp)
    resources.register_resources(mcp)
    resources.register_prompts(mcp)
    return mcp


def _setup_logging() -> None:
    settings = get_settings()
    log_file = settings.resolved_log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=2)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setLevel(logging.WARNING)
    root.addHandler(stderr)


def main() -> None:
    _setup_logging()
    log = logging.getLogger("clipper")
    try:
        versions = ffmpeg.check()
    except ffmpeg.FFmpegNotFoundError as e:
        log.error("%s", e)
        sys.exit(1)
    settings = get_settings()
    log.info("clipper %s starting; %s; allowed_dirs=%s", __version__, versions[0],
             [str(d) for d in settings.allowed_dirs])
    if not settings.allowed_dirs:
        log.warning("ALLOWED_DIRS is not set: every file access will be refused.")
    build_server().run("stdio")


if __name__ == "__main__":
    main()
