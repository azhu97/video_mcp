"""In-process background jobs: start_job → job_status → job_result.

For clients that time out on long tool calls. Jobs live in memory for the server's lifetime.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

ProgressFn = Callable[[int, int, str], None]


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"  # queued | running | done | failed
    progress: float = 0.0
    message: str = ""
    created: float = field(default_factory=time.time)
    finished: float | None = None
    result: Any = None
    error: str | None = None

    def view(self) -> dict[str, Any]:
        d = {"job_id": self.id, "kind": self.kind, "status": self.status,
             "progress": round(self.progress, 3), "message": self.message,
             "elapsed_seconds": round((self.finished or time.time()) - self.created, 1)}
        if self.error:
            d["error"] = self.error
        return d


class JobManager:
    def __init__(self, workers: int = 2, keep: int = 100):
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="clipper-job")
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._keep = keep

    def start(self, kind: str, fn: Callable[[ProgressFn], Any]) -> Job:
        job = Job(id=uuid.uuid4().hex[:10], kind=kind)

        def progress(done: int, total: int, message: str) -> None:
            job.progress = done / total if total else 0.0
            job.message = message

        def runner() -> None:
            job.status = "running"
            try:
                job.result = fn(progress)
                job.status = "done"
                job.progress = 1.0
            except Exception as e:
                job.status = "failed"
                job.error = f"{type(e).__name__}: {e}"
                job.message = traceback.format_exc(limit=3).splitlines()[-1]
            finally:
                job.finished = time.time()

        with self._lock:
            self._jobs[job.id] = job
            self._prune()
        self._pool.submit(runner)
        return job

    def get(self, job_id: str) -> Job:
        try:
            return self._jobs[job_id]
        except KeyError:
            raise KeyError(f"Unknown job_id {job_id!r}") from None

    def list(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)

    def _prune(self) -> None:
        done = [j for j in self._jobs.values() if j.finished]
        for j in sorted(done, key=lambda j: j.finished or 0)[: max(0, len(self._jobs) - self._keep)]:
            del self._jobs[j.id]
