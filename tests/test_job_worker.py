from __future__ import annotations

import json
import signal
import sqlite3
from pathlib import Path

import fake_cdn.cli
from fake_cdn.core.job_runner import JobCancelled, apply_api_environment, run_persisted_job
from fake_cdn.core.job_service import JobService
from fake_cdn.core.tenant_config import TenantConfigStore
from fake_cdn.worker import RunningProcess, TaskWorker


def _single_slot_config(tmp_path: Path) -> dict:
    source = Path(__file__).resolve().parent.parent / "config.json"
    config = json.loads(source.read_text(encoding="utf-8"))
    config["time"].update(
        {
            "start_datetime": "2026-08-01 00:00:00",
            "end_datetime": "2026-08-01 00:00:00",
            "interval_seconds": 300,
            "timezone": "Asia/Singapore",
        }
    )
    config["dimensions"]["domains"] = config["dimensions"]["domains"][:1]
    config["dimensions"]["regions"] = config["dimensions"]["regions"][:1]
    config["target"]["total_flux"]["value"] = 0.000001
    config["mode"].update(
        {"dry_run": True, "save_local": True, "output_dir": str(tmp_path / "artifacts")}
    )
    config["deployment"]["mode"] = "preview"
    return config


def test_api_environment_values_are_not_written_to_logs(tmp_path: Path, monkeypatch, capsys):
    endpoint = "https://secret.example.test/logs"
    vip = "sensitive-vip"
    monkeypatch.setenv("CDN_API_ENDPOINT", endpoint)
    monkeypatch.setenv("CDN_API_VIP", vip)

    resolved = apply_api_environment(_single_slot_config(tmp_path))
    output = capsys.readouterr().out

    assert resolved["api"]["endpoint"] == endpoint
    assert resolved["api"]["headers"]["vip"] == vip
    assert "API endpoint 已加载" in output
    assert endpoint not in output
    assert vip not in output


def test_worker_executes_queued_job_in_child_process(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "config.db"
    central_db = tmp_path / "cdn_logs.db"
    store = TenantConfigStore(db_path)
    store.create_tenant("tenant-a", "Tenant A", _single_slot_config(tmp_path))
    job = JobService(store).enqueue("tenant-a", "simulation", {}, actor="admin")
    monkeypatch.setenv("FAKE_CDN_DB_PATH", str(central_db))

    TaskWorker(str(db_path), poll_interval=0.05, max_realtime=0, max_batch=1).run(once=True)

    completed = store.get_job(job["job_id"])
    assert completed["status"] == "succeeded"
    assert completed["stats"]
    assert Path(completed["log_path"]).exists()
    assert central_db.exists()
    with sqlite3.connect(central_db) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.list_workers()[0]["status"] == "stopped"


class _FinishedProcess:
    pid = 43210

    @staticmethod
    def poll():
        return -15


def test_worker_shutdown_marks_unfinished_child_interrupted(tmp_path: Path):
    db_path = tmp_path / "config.db"
    store = TenantConfigStore(db_path)
    store.create_tenant("tenant-a", "Tenant A", _single_slot_config(tmp_path))
    job = JobService(store).create_direct("tenant-a", "simulation", {}, actor="cli")
    worker = TaskWorker(str(db_path), poll_interval=0.05)
    worker.children[job["job_id"]] = RunningProcess(
        job_id=job["job_id"], mode="simulation", process=_FinishedProcess()
    )

    assert worker._reap(shutting_down=True) == 1
    interrupted = store.get_job(job["job_id"])
    assert interrupted["status"] == "interrupted"
    assert interrupted["error_text"] == "Worker 服务停止"


def test_worker_startup_recovers_orphaned_running_job(tmp_path: Path):
    db_path = tmp_path / "config.db"
    store = TenantConfigStore(db_path)
    snapshot = store.create_tenant("tenant-a", "Tenant A", _single_slot_config(tmp_path))
    job = store.create_job(
        "tenant-a",
        snapshot["id"],
        "simulation",
        snapshot["config"]["mode"]["output_dir"],
        actor="legacy",
    )

    assert store.recover_active_jobs(actor="worker-test") == 1
    recovered = store.get_job(job["job_id"])
    assert recovered["status"] == "interrupted"
    assert "Worker 重启" in recovered["error_text"]


def test_worker_startup_does_not_interrupt_direct_cli_job(tmp_path: Path):
    db_path = tmp_path / "config.db"
    store = TenantConfigStore(db_path)
    store.create_tenant("tenant-a", "Tenant A", _single_slot_config(tmp_path))
    job = JobService(store).create_direct(
        "tenant-a", "simulation", {}, actor="cli", worker_id="cli-123"
    )

    assert store.recover_active_jobs(actor="worker-test") == 0
    assert store.get_job(job["job_id"])["status"] == "running"


def test_worker_recovers_dead_direct_cli_process(tmp_path: Path):
    db_path = tmp_path / "config.db"
    store = TenantConfigStore(db_path)
    snapshot = store.create_tenant("tenant-a", "Tenant A", _single_slot_config(tmp_path))
    job = store.create_job(
        "tenant-a",
        snapshot["id"],
        "simulation",
        snapshot["config"]["mode"]["output_dir"],
        actor="cli",
        worker_id="cli-dead",
    )
    store.set_job_process(job["job_id"], "cli-dead", 99999999)

    assert store.recover_active_jobs(actor="worker-test") == 1
    assert store.get_job(job["job_id"])["status"] == "interrupted"


def test_runner_distinguishes_worker_stop_from_admin_cancel(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "config.db"
    store = TenantConfigStore(db_path)
    store.create_tenant("tenant-a", "Tenant A", _single_slot_config(tmp_path))
    job = JobService(store).create_direct(
        "tenant-a", "simulation", {}, actor="cli", worker_id="worker-test"
    )

    def stop_execution(_mode, _config, _args):
        raise JobCancelled(signal.SIGTERM)

    monkeypatch.setattr(fake_cdn.cli, "execute_runtime_mode", stop_execution)
    result = run_persisted_job(store, job["job_id"], worker_id="worker-test")

    assert result["status"] == "interrupted"
    assert result["error_text"] == "执行进程收到停止信号"
