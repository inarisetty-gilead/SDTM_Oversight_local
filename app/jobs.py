"""A one-at-a-time background job with progress, so a long build does not block the UI.

This is a single-user desktop application: one job runs at a time, its state lives in
memory, and nothing is persisted beyond the output folder the job writes."""
from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Job:
    kind: str = ""
    status: str = "idle"          # idle | running | done | error
    message: str = ""
    step: int = 0
    total: int = 0
    started: str = ""
    finished: str = ""
    error: str = ""
    detail: str = ""

    def as_dict(self) -> dict:
        pct = 0 if not self.total else min(100, round(100 * self.step / self.total))
        return {"kind": self.kind, "status": self.status, "message": self.message,
                "step": self.step, "total": self.total, "percent": pct,
                "started": self.started, "finished": self.finished,
                "error": self.error, "detail": self.detail}


class JobRunner:
    def __init__(self):
        self.job = Job()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        return self.job.status == "running"

    def progress(self, message: str, step: int | None = None, total: int | None = None) -> None:
        with self._lock:
            self.job.message = message
            if step is not None:
                self.job.step = step
            if total is not None:
                self.job.total = total

    def start(self, kind: str, fn, total: int = 0) -> bool:
        """Run fn(progress) on a worker thread. Returns False if a job is already running."""
        with self._lock:
            if self.job.status == "running":
                return False
            self.job = Job(kind=kind, status="running", total=total,
                           started=datetime.now().isoformat(timespec="seconds"),
                           message="starting…")

        def run():
            try:
                fn(self.progress)
                with self._lock:
                    self.job.status = "done"
                    self.job.message = "complete"
                    self.job.step = self.job.total
            except Exception as exc:                              # noqa: BLE001
                with self._lock:
                    self.job.status = "error"
                    self.job.error = f"{type(exc).__name__}: {exc}"
                    self.job.detail = traceback.format_exc()
                    self.job.message = "failed"
            finally:
                with self._lock:
                    self.job.finished = datetime.now().isoformat(timespec="seconds")

        self._thread = threading.Thread(target=run, daemon=True, name=f"job-{kind}")
        self._thread.start()
        return True

    def state(self) -> dict:
        with self._lock:
            return self.job.as_dict()
