"""Controlled task worker. No background thread, timer, or network at import time.

Launchers may call run_once periodically or run scripts/run_task_worker.py as their
managed child. Each round refreshes the server identity; repository dispatch also
rechecks the current issuer and dispatcher immediately before each possible POST.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from enterprise.operations import MaintenanceError
from enterprise.rpa_client import RPAClient
from enterprise.security import Principal, require, worker_principal
from enterprise.task_workflow import TaskRepository


class TaskWorker:
    def __init__(self, repository: TaskRepository, *,
                 actor_provider: Callable[[], Principal] = worker_principal,
                 client_factory: Callable[[], RPAClient] = RPAClient,
                 batch_size: int = 10, receipt_interval_seconds: float = 30.0):
        if not 1 <= batch_size <= 100 or not 0 <= receipt_interval_seconds <= 3600:
            raise ValueError("worker批次或回执间隔无效")
        self.repository = repository
        self.actor_provider = actor_provider
        self.client_factory = client_factory
        self.batch_size = batch_size
        self.receipt_interval_seconds = receipt_interval_seconds

    def run_once(self, *, actor_provider: Callable[[], Principal] | None = None) -> dict:
        """One bounded round; never sleeps or requeues terminal/unknown records.

        Auth failures propagate before opening a client. Maintenance/restore-review
        blocks dispatch; a restore review still permits receipt-only reconciliation.
        Automatic receipt polling is only for accepted non-completed tasks. Unknown
        POST results exhaust the repository's finite query budget and await review.
        """
        provider = actor_provider or self.actor_provider
        actor = provider()
        require(actor, "task.send")
        result = {"started_utc": datetime.now(timezone.utc).isoformat(), "dispatched": [],
                  "synced": [], "reminders_scheduled": [], "reminders_dispatched": [],
                  "errors": [], "dispatch_paused": False}
        with self.client_factory() as client:
            try:
                result["dispatched"] = self.repository.dispatch(client, actor=actor, limit=self.batch_size)
            except MaintenanceError:
                result["dispatch_paused"] = True
            except Exception as exc:
                # Unexpected transport/DB failures leave a fenced lease. Do not
                # print provider bodies, SQL or business data into worker logs.
                result["errors"].append({"operation": "dispatch", "error_type": type(exc).__name__})
            actor = provider()
            require(actor, "task.send")
            try:
                # Pagination is applied after authorization by the repository.
                candidates, offset = [], 0
                while True:
                    page = self.repository.list(actor=actor, status="accepted", limit=1000, offset=offset)
                    candidates.extend(row for row in page if row["receipt_status"] != "completed")
                    if len(page) < 1000:
                        break
                    offset += 1000
                candidates.sort(key=lambda row: (row["updated_utc"], row["task_id"]))
            except MaintenanceError:
                candidates = []
                result["dispatch_paused"] = True
            now = self.repository.clock()
            due = [row for row in candidates if
                   now - datetime.fromisoformat(row["updated_utc"]).timestamp() >= self.receipt_interval_seconds]
            for row in due[:self.batch_size]:
                try:
                    current = provider()
                    require(current, "task.send")
                    result["synced"].append(self.repository.sync(row["task_id"], client, actor=current))
                except MaintenanceError:
                    result["dispatch_paused"] = True
                    break
                except PermissionError:
                    # Permission changes must stop work, not reuse an old principal.
                    result["errors"].append({"operation": "sync", "task_id": row["task_id"], "error_type": "PermissionError"})
                    break
                except Exception as exc:
                    result["errors"].append({"operation": "sync", "task_id": row["task_id"], "error_type": type(exc).__name__})
            try:
                current = provider()
                require(current, "task.remind")
                result["reminders_scheduled"] = self.repository.schedule_reminders(actor=current, limit=self.batch_size)
                result["reminders_dispatched"] = self.repository.dispatch_reminders(client, actor=current, limit=self.batch_size)
            except MaintenanceError:
                result["dispatch_paused"] = True
            except Exception as exc:
                result["errors"].append({"operation": "reminders", "error_type": type(exc).__name__})
        result["finished_utc"] = datetime.now(timezone.utc).isoformat()
        return result
