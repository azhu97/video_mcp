"""Shared helpers for MCP tool adapters."""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable
from typing import Any, TypeVar

import anyio
import anyio.from_thread
import anyio.to_thread
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from clipper.core.ffmpeg import FFmpegError, FFmpegNotFoundError
from clipper.core.segments import Segment, parse_time

log = logging.getLogger("clipper.tools")
T = TypeVar("T")

ProgressFn = Callable[[int, int, str], None]


class SegmentIn(BaseModel):
    start: float | str = Field(description="Seconds (e.g. 12.5) or 'HH:MM:SS.ms' / 'MM:SS'")
    end: float | str = Field(description="Seconds or 'HH:MM:SS.ms' / 'MM:SS'")
    label: str = Field("", description="Optional short label; used in the output filename")

    def to_segment(self) -> Segment:
        return Segment(parse_time(self.start), parse_time(self.end), self.label)


def opt_time(value: float | str | None) -> float | None:
    return None if value is None else parse_time(value)


async def run_blocking(ctx: Context | None, fn: Callable[[ProgressFn], T]) -> T:
    """Run blocking core work in a worker thread, forwarding progress as MCP notifications."""

    def progress(done: int, total: int, message: str) -> None:
        if ctx is None:
            return
        try:
            anyio.from_thread.run(ctx.report_progress, done, total, message)
        except Exception:  # progress is best-effort; never fail the work over it
            log.debug("progress notification failed", exc_info=True)

    return await anyio.to_thread.run_sync(lambda: fn(progress))


# Errors whose message is meant for the caller. MCPServer replaces the text of any other
# exception with a generic "Error executing tool", so these are re-raised as ToolError.
USER_ERRORS = (ValueError, KeyError, OSError, RuntimeError, FFmpegError, FFmpegNotFoundError)


def _message(e: Exception) -> str:
    msg = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
    return f"{type(e).__name__}: {msg}"


def user_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Surface expected failures (bad path, bad timestamp, ffmpeg error...) to the model."""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except ToolError:
                raise
            except USER_ERRORS as e:
                log.info("%s failed: %s", fn.__name__, e)
                raise ToolError(_message(e)) from e

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except USER_ERRORS as e:
            log.info("%s failed: %s", fn.__name__, e)
            raise ToolError(_message(e)) from e

    return wrapper
