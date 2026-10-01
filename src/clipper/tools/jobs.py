"""Background-job tools for long work: start_job → job_status → job_result."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from clipper.core.jobs import JobManager
from clipper.tools import analysis, clips
from clipper.tools.common import user_errors

JobKind = Literal["export_clips", "find_key_moments", "find_clock_running", "transcribe", "search_transcript",
                  "make_highlight_reel"]

RUNNERS: dict[str, Callable[..., Any]] = {
    "export_clips": clips.do_export,
    "find_key_moments": analysis.do_find,
    "find_clock_running": analysis.do_clock,
    "transcribe": analysis.do_transcribe,
    "search_transcript": analysis.do_search,
    "make_highlight_reel": clips.do_reel,
}


def register(mcp: MCPServer, manager: JobManager | None = None) -> JobManager:
    jobs = manager or JobManager()

    @mcp.tool()
    @user_errors
    def start_job(tool: JobKind, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run a long tool in the background and return immediately with a job_id.

        Use for long videos (transcribing hours of audio, big exports) when a direct call
        might time out. Poll job_status, then fetch job_result.

        Args:
            tool: Which tool to run.
            arguments: The same arguments the direct tool takes (path, segments, ...).
        """
        fn = RUNNERS[tool]
        # Validate argument names up front so typos fail now, not in the background.
        params = inspect.signature(fn).parameters
        unknown = sorted(set(arguments) - set(params) - {"progress"})
        if unknown:
            raise ValueError(f"Unknown argument(s) for {tool}: {unknown}")
        job = jobs.start(tool, lambda progress: fn(**arguments, progress=progress))
        return job.view()

    @mcp.tool()
    @user_errors
    def job_status(job_id: str | None = None) -> dict[str, Any]:
        """Status/progress of a background job, or of all recent jobs when job_id is omitted.

        Args:
            job_id: The id returned by start_job.
        """
        if job_id:
            return jobs.get(job_id).view()
        return {"jobs": [j.view() for j in jobs.list()]}

    @mcp.tool()
    @user_errors
    def job_result(job_id: str) -> dict[str, Any]:
        """Result of a finished background job (same shape as the direct tool's result).

        Args:
            job_id: The id returned by start_job.
        """
        job = jobs.get(job_id)
        if job.status == "failed":
            raise RuntimeError(f"Job {job_id} failed: {job.error}")
        if job.status != "done":
            return {**job.view(), "hint": "Not finished yet; poll job_status."}
        return {"job_id": job_id, "status": "done", "result": job.result}

    return jobs
