"""SQLite 任务队列 Worker。"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from fake_cdn.core.config_manager import ConfigConflictError
from fake_cdn.core.tenant_config import JOB_TERMINAL_STATUSES, TenantConfigStore

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows 兼容兜底
    fcntl = None


@dataclass
class RunningProcess:
    job_id: str
    mode: str
    process: subprocess.Popen
    terminate_sent_at: Optional[float] = None


class TaskWorker:
    """领取任务并用受限的 job_id 子进程执行。"""

    def __init__(
        self,
        config_db_path: str,
        *,
        poll_interval: float = 1.0,
        cancel_grace_seconds: float = 10.0,
        max_realtime: int = 1,
        max_batch: int = 1,
    ):
        self.store = TenantConfigStore(config_db_path)
        self.poll_interval = max(0.1, float(poll_interval))
        self.cancel_grace_seconds = max(1.0, float(cancel_grace_seconds))
        self.max_realtime = max(0, int(max_realtime))
        self.max_batch = max(0, int(max_batch))
        if self.max_realtime + self.max_batch < 1:
            raise ValueError("Worker 至少需要一个执行槽位")
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.children: Dict[str, RunningProcess] = {}
        self._stopping = False
        self._lock_file = None

    def _acquire_lock(self) -> None:
        lock_path = self.store.db_path.with_suffix(self.store.db_path.suffix + ".worker.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = lock_path.open("a+")
        if fcntl is None:
            return
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            self._lock_file = None
            raise ConfigConflictError("已有任务 Worker 正在运行") from exc

    def _release_lock(self) -> None:
        if self._lock_file is None:
            return
        if fcntl is not None:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
        self._lock_file.close()
        self._lock_file = None

    def _signal_stop(self, _signum=None, _frame=None) -> None:
        self._stopping = True

    def _available_modes(self):
        realtime_count = sum(child.mode == "realtime" for child in self.children.values())
        batch_count = sum(child.mode != "realtime" for child in self.children.values())
        modes = []
        if realtime_count < self.max_realtime:
            modes.append("realtime")
        if batch_count < self.max_batch:
            modes.extend(["simulation", "catchup"])
        return modes

    def _spawn(self, job: Dict) -> None:
        log_path = Path(job["log_path"] or Path(job["output_dir"]) / "job.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-u",
            "-m",
            "fake_cdn",
            "run-job",
            "--job-id",
            job["job_id"],
            "--config-db",
            str(self.store.db_path),
        ]
        environment = dict(os.environ)
        environment["FAKE_CDN_CONFIG_DB_PATH"] = str(self.store.db_path)
        environment["FAKE_CDN_WORKER_ID"] = self.worker_id
        process = None
        try:
            with log_path.open("ab", buffering=0) as log_file:
                process = subprocess.Popen(
                    command,
                    cwd=os.environ.get("FAKE_CDN_PROJECT_ROOT") or os.getcwd(),
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            self.store.set_job_process(job["job_id"], self.worker_id, process.pid)
            self.children[job["job_id"]] = RunningProcess(
                job_id=job["job_id"], mode=job["mode"], process=process
            )
            print(f"[Worker] 启动 {job['job_id']} / {job['mode']} / PID {process.pid}")
        except Exception as exc:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            self.store.finish_job(
                job["job_id"],
                "failed",
                error_text=f"启动任务子进程失败: {exc}",
                actor=self.worker_id,
            )
            raise

    def _start_available_jobs(self) -> int:
        started = 0
        while not self._stopping:
            modes = self._available_modes()
            if not modes:
                break
            job = self.store.claim_next_job(self.worker_id, modes=modes)
            if not job:
                break
            self._spawn(job)
            started += 1
        return started

    def _cancel_requested_jobs(self) -> None:
        now = time.monotonic()
        for child in list(self.children.values()):
            job = self.store.get_job(child.job_id)
            if job["status"] != "cancel_requested" or child.process.poll() is not None:
                continue
            if child.terminate_sent_at is None:
                child.process.terminate()
                child.terminate_sent_at = now
                print(f"[Worker] 请求停止 {child.job_id}")
            elif now - child.terminate_sent_at >= self.cancel_grace_seconds:
                child.process.kill()
                print(f"[Worker] 强制停止 {child.job_id}")

    def _reap(self, *, shutting_down: bool = False) -> int:
        finished = 0
        for job_id, child in list(self.children.items()):
            return_code = child.process.poll()
            if return_code is None:
                self.store.heartbeat_job(job_id, pid=child.process.pid)
                continue
            job = self.store.get_job(job_id)
            if job["status"] not in JOB_TERMINAL_STATUSES:
                if job["status"] == "cancel_requested":
                    self.store.finish_job(job_id, "cancelled", actor=self.worker_id)
                elif shutting_down:
                    self.store.finish_job(
                        job_id,
                        "interrupted",
                        error_text="Worker 服务停止",
                        actor=self.worker_id,
                    )
                elif return_code == 0:
                    self.store.finish_job(job_id, "succeeded", actor=self.worker_id)
                else:
                    self.store.finish_job(
                        job_id,
                        "failed",
                        error_text=f"任务子进程退出码: {return_code}",
                        actor=self.worker_id,
                    )
            print(f"[Worker] 结束 {job_id} / exit={return_code}")
            del self.children[job_id]
            finished += 1
        return finished

    def tick(self) -> Dict[str, int]:
        self.store.heartbeat_worker(self.worker_id)
        self._cancel_requested_jobs()
        finished = self._reap()
        started = self._start_available_jobs()
        return {"started": started, "finished": finished, "running": len(self.children)}

    def _stop_children(self) -> None:
        for child in self.children.values():
            if child.process.poll() is None:
                child.process.terminate()
        deadline = time.monotonic() + self.cancel_grace_seconds
        while self.children and time.monotonic() < deadline:
            self._reap(shutting_down=True)
            if self.children:
                time.sleep(0.1)
        for job_id, child in list(self.children.items()):
            if child.process.poll() is None:
                child.process.kill()
                child.process.wait(timeout=5)
            job = self.store.get_job(job_id)
            if job["status"] not in JOB_TERMINAL_STATUSES:
                self.store.finish_job(
                    job_id,
                    "interrupted",
                    error_text="Worker 服务停止",
                    actor=self.worker_id,
                )
            del self.children[job_id]

    def run(self, *, once: bool = False) -> None:
        self._acquire_lock()
        previous_term = signal.signal(signal.SIGTERM, self._signal_stop)
        previous_int = signal.signal(signal.SIGINT, self._signal_stop)
        try:
            recovered = self.store.recover_active_jobs(actor=self.worker_id)
            self.store.register_worker(self.worker_id, socket.gethostname(), os.getpid())
            print(f"[Worker] 已启动: {self.worker_id}; 恢复中断任务 {recovered} 个")
            while not self._stopping:
                state = self.tick()
                if once and state["running"] == 0 and state["started"] == 0:
                    break
                time.sleep(self.poll_interval)
        finally:
            self._stop_children()
            self.store.stop_worker(self.worker_id)
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGINT, previous_int)
            self._release_lock()
            print("[Worker] 已停止")


def run_worker(
    config_db_path: str,
    *,
    poll_interval: float = 1.0,
    once: bool = False,
    max_realtime: int = 1,
    max_batch: int = 1,
) -> None:
    TaskWorker(
        config_db_path,
        poll_interval=poll_interval,
        max_realtime=max_realtime,
        max_batch=max_batch,
    ).run(once=once)
