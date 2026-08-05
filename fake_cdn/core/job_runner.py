"""按任务记录中的固定配置版本执行生成任务。"""

from __future__ import annotations

import os
import signal
import threading
from copy import deepcopy
from types import SimpleNamespace
from typing import Dict

from fake_cdn.core.config_manager import ConfigConflictError
from fake_cdn.core.generator import normalize_config
from fake_cdn.core.tenant_config import TenantConfigStore


class JobCancelled(Exception):
    """任务收到管理员取消请求。"""

    def __init__(self, signum: int):
        self.signum = signum
        super().__init__(f"任务收到信号 {signum}")


def apply_api_environment(config: Dict) -> Dict:
    """在执行时注入 API 环境变量，避免凭据进入配置版本。"""
    resolved = deepcopy(config)
    if os.environ.get("CDN_API_ENDPOINT"):
        resolved.setdefault("api", {}).setdefault("headers", {})
        resolved["api"]["endpoint"] = os.environ["CDN_API_ENDPOINT"]
        print("[环境变量] API endpoint 已加载")
    if os.environ.get("CDN_API_VIP"):
        resolved.setdefault("api", {}).setdefault("headers", {})
        resolved["api"]["headers"]["vip"] = os.environ["CDN_API_VIP"]
        print("[环境变量] API vip 已加载")
    return normalize_config(resolved)


def prepare_job_config(store: TenantConfigStore, job: Dict) -> Dict:
    snapshot = store.resolve_version(job["tenant_id"], job["config_version_id"])
    config = apply_api_environment(snapshot["config"])
    parameters = job["parameters"]
    if parameters.get("effective_dry_run"):
        config = deepcopy(config)
        config["mode"]["dry_run"] = True
    config["mode"]["output_dir"] = job["output_dir"]
    central_log_db = os.environ.get("FAKE_CDN_DB_PATH") or str(store.db_path.parent / "cdn_logs.db")
    config["_runtime"] = {
        "tenant_id": job["tenant_id"],
        "config_version_id": snapshot["id"],
        "config_checksum": snapshot["checksum"],
        "generation_job_id": job["job_id"],
        "log_db_path": central_log_db,
    }
    return config


def _execution_args(job: Dict) -> SimpleNamespace:
    parameters = job["parameters"]
    return SimpleNamespace(
        once=bool(parameters.get("once", False)),
        start_datetime=parameters.get("start_datetime"),
        end_datetime=parameters.get("end_datetime"),
        start_date=None,
        end_date=None,
    )


def run_persisted_job(
    store: TenantConfigStore,
    job_id: str,
    *,
    worker_id: str,
    heartbeat_interval: float = 2.0,
) -> Dict:
    """执行已处于 running 状态的任务，并完整维护终态。"""
    job = store.get_job(job_id)
    if job["status"] == "cancel_requested":
        return store.finish_job(job_id, "cancelled", actor=worker_id)
    if job["status"] != "running":
        raise ConfigConflictError(f"任务不在运行状态: {job_id} / {job['status']}")

    store.set_job_process(job_id, worker_id, os.getpid())
    stop_heartbeat = threading.Event()

    def request_stop(signum, _frame=None):
        raise JobCancelled(signum)

    def heartbeat_loop() -> None:
        while not stop_heartbeat.wait(max(0.5, heartbeat_interval)):
            if not store.heartbeat_job(job_id, pid=os.getpid()):
                return
            if store.get_job(job_id)["status"] == "cancel_requested":
                os.kill(os.getpid(), signal.SIGTERM)
                return

    previous_term = None
    previous_int = None
    if threading.current_thread() is threading.main_thread():
        previous_term = signal.signal(signal.SIGTERM, request_stop)
        previous_int = signal.signal(signal.SIGINT, request_stop)
    heartbeat = threading.Thread(target=heartbeat_loop, name=f"heartbeat-{job_id}", daemon=True)
    heartbeat.start()

    try:
        config = prepare_job_config(store, job)
        from fake_cdn.cli import execute_runtime_mode

        stats = execute_runtime_mode(job["mode"], config, _execution_args(job))
        return store.finish_job(job_id, "succeeded", stats or {}, actor=worker_id)
    except (JobCancelled, KeyboardInterrupt) as exc:
        current = store.get_job(job_id)
        explicitly_cancelled = current["status"] == "cancel_requested" or isinstance(
            exc, KeyboardInterrupt
        )
        if isinstance(exc, JobCancelled) and exc.signum == signal.SIGINT:
            explicitly_cancelled = True
        return store.finish_job(
            job_id,
            "cancelled" if explicitly_cancelled else "interrupted",
            {"reason": str(exc)},
            error_text=None if explicitly_cancelled else "执行进程收到停止信号",
            actor=worker_id,
        )
    except Exception as exc:
        store.finish_job(job_id, "failed", error_text=str(exc), actor=worker_id)
        raise
    finally:
        stop_heartbeat.set()
        heartbeat.join(timeout=max(1.0, heartbeat_interval * 2))
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)
        if previous_int is not None:
            signal.signal(signal.SIGINT, previous_int)
