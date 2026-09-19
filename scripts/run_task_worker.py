"""Managed mock-only task worker; --once is suitable for service probes/tests."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading

# Support `python scripts/run_task_worker.py` from a managed launcher.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None) -> int:
    from enterprise.rpa_client import RPAClient, RPAConfig
    from enterprise.task_workflow import TaskRepository
    from enterprise.task_worker import TaskWorker
    from paths import MANAGED_DIR

    parser = argparse.ArgumentParser(description="派发已签发的官方mock任务并同步模拟回执")
    parser.add_argument("--root", type=Path, default=MANAGED_DIR, help="统一MANAGED_DIR")
    parser.add_argument("--once", action="store_true", help="仅执行一个有界轮次")
    parser.add_argument("--interval", type=float, default=2.0, help="轮询间隔，范围0.1–5秒")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--receipt-interval", type=float, default=30.0)
    parser.add_argument("--mock-url", default="http://127.0.0.1:8090", help="仅允许官方8090 loopback origin")
    args = parser.parse_args(argv)
    if not 0.1 <= args.interval <= 5:
        parser.error("--interval必须介于0.1与5秒")
    config = RPAConfig(base_url=args.mock_url)
    worker = TaskWorker(TaskRepository(args.root), client_factory=lambda: RPAClient(config),
                        batch_size=args.batch_size, receipt_interval_seconds=args.receipt_interval)
    stopped = threading.Event()
    try:
        while not stopped.is_set():
            try:
                result = worker.run_once()
                summary = {"started_utc": result["started_utc"], "dispatched": len(result["dispatched"]),
                           "synced": len(result["synced"]), "errors": len(result["errors"]),
                           "dispatch_paused": result["dispatch_paused"]}
                print(json.dumps(summary, ensure_ascii=False), flush=True)
                if args.once:
                    return 1 if result["errors"] else 0
            except Exception as exc:
                # Stable error types only; credentials and task content stay out.
                print(json.dumps({"worker_error": type(exc).__name__, "action": "paused"}), flush=True)
                if args.once:
                    return 1
            stopped.wait(args.interval)
    except KeyboardInterrupt:
        stopped.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
