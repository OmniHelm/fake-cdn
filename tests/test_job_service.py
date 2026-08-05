from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest

from fake_cdn.core.config_manager import ConfigConflictError, ConfigValidationError
from fake_cdn.core.job_service import JobService
from fake_cdn.core.tenant_config import TenantConfigStore


def _job_config(tmp_path: Path, *, real_push: bool = False) -> dict:
    source = Path(__file__).resolve().parent.parent / "config.json"
    config = json.loads(source.read_text(encoding="utf-8"))
    config["time"].update(
        {
            "start_datetime": "2026-08-01 00:00:00",
            "end_datetime": "2026-08-01 00:10:00",
            "interval_seconds": 300,
            "timezone": "Asia/Singapore",
        }
    )
    config["dimensions"]["domains"] = config["dimensions"]["domains"][:1]
    config["dimensions"]["regions"] = config["dimensions"]["regions"][:1]
    config["target"]["total_flux"]["value"] = 0.000001
    config["mode"]["output_dir"] = str(tmp_path / "artifacts")
    config["mode"]["dry_run"] = not real_push
    config["deployment"]["mode"] = "push" if real_push else "preview"
    return config


def test_legacy_job_schema_migrates_and_deduplicates_active_rows(tmp_path: Path):
    db_path = tmp_path / "config.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE generation_jobs (
                job_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                config_version_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                output_dir TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                finished_at TEXT,
                stats_json TEXT
            );
            INSERT INTO generation_jobs VALUES
                ('old-1', 'tenant-a', 1, 'simulation', 'running', '/tmp/1',
                 'legacy', '2026-08-01T00:00:00+00:00', NULL, NULL),
                ('old-2', 'tenant-a', 1, 'simulation', 'running', '/tmp/2',
                 'legacy', '2026-08-02T00:00:00+00:00', NULL, NULL);
            """)

    TenantConfigStore(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(generation_jobs)")}
        rows = conn.execute(
            "SELECT job_id, status, error_text FROM generation_jobs ORDER BY job_id"
        ).fetchall()
    assert {"parameters_json", "heartbeat_at", "worker_id", "log_path", "error_text"} <= columns
    assert rows == [
        ("old-1", "interrupted", "队列升级时合并了重复的活动任务"),
        ("old-2", "running", None),
    ]


def test_job_keeps_published_version_and_prevents_parallel_tenant_jobs(tmp_path: Path):
    store = TenantConfigStore(tmp_path / "config.db")
    original = store.create_tenant("tenant-a", "Tenant A", _job_config(tmp_path))
    service = JobService(store)

    job = service.enqueue("tenant-a", "simulation", {}, actor="admin")
    candidate = deepcopy(original["config"])
    candidate["target"]["total_flux"]["value"] = 0.000002
    draft = store.save_draft(
        "tenant-a", candidate, expected_revision=original["revision"], actor="admin"
    )
    published = store.publish(
        "tenant-a", draft["id"], expected_revision=draft["revision"], actor="admin"
    )

    assert published["id"] != job["config_version_id"]
    assert store.resolve_version("tenant-a", job["config_version_id"])["id"] == original["id"]
    with pytest.raises(ConfigConflictError, match="已有排队或运行中的任务"):
        service.enqueue("tenant-a", "simulation", {}, actor="admin")

    assert service.request_cancel(job["job_id"], actor="admin")["status"] == "cancelled"
    retried = service.retry(job["job_id"], actor="admin")
    assert retried["config_version_id"] == original["id"]
    retry_audit = store.read_audit("tenant-a", limit=1)[0]
    assert retry_audit["action"] == "job.retry"
    assert retry_audit["detail"]["source_job_id"] == job["job_id"]
    assert service.counts() == {"all": 2, "cancelled": 1, "queued": 1, "active": 1}


def test_catchup_window_alignment_and_bounds_are_validated(tmp_path: Path):
    store = TenantConfigStore(tmp_path / "config.db")
    store.create_tenant("tenant-a", "Tenant A", _job_config(tmp_path))
    service = JobService(store)

    job = service.enqueue(
        "tenant-a",
        "catchup",
        {
            "start_datetime": "2026-08-01T00:00:00",
            "end_datetime": "2026-08-01T00:05:00",
        },
        actor="admin",
    )
    assert job["parameters"]["start_datetime"] == "2026-08-01 00:00:00"
    service.request_cancel(job["job_id"], actor="admin")

    with pytest.raises(ConfigValidationError, match="粒度对齐"):
        service.enqueue(
            "tenant-a",
            "catchup",
            {
                "start_datetime": "2026-08-01T00:00:01",
                "end_datetime": "2026-08-01T00:05:00",
            },
            actor="admin",
        )
    with pytest.raises(ConfigValidationError, match="配置的时间窗口"):
        service.enqueue(
            "tenant-a",
            "catchup",
            {
                "start_datetime": "2026-07-31T23:55:00",
                "end_datetime": "2026-08-01T00:05:00",
            },
            actor="admin",
        )


def test_real_push_enqueue_and_retry_require_explicit_confirmation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CDN_API_ENDPOINT", "https://api.example.test/logs")
    monkeypatch.setenv("CDN_API_VIP", "test-vip")
    store = TenantConfigStore(tmp_path / "config.db")
    store.create_tenant("tenant-push", "Tenant Push", _job_config(tmp_path, real_push=True))
    service = JobService(store)

    with pytest.raises(ConfigValidationError, match="二次确认"):
        service.enqueue("tenant-push", "simulation", {}, actor="admin")

    job = service.enqueue("tenant-push", "simulation", {}, actor="admin", push_confirmed=True)
    assert job["parameters"]["effective_dry_run"] is False
    assert job["parameters"]["push_confirmed"] is True
    service.request_cancel(job["job_id"], actor="admin")

    with pytest.raises(ConfigValidationError, match="二次确认"):
        service.retry(job["job_id"], actor="admin")
    retried = service.retry(job["job_id"], actor="admin", push_confirmed=True)
    assert retried["parameters"]["push_confirmed"] is True


def test_real_push_rejects_missing_runtime_api_configuration(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CDN_API_ENDPOINT", raising=False)
    monkeypatch.delenv("CDN_API_VIP", raising=False)
    store = TenantConfigStore(tmp_path / "config.db")
    store.create_tenant("tenant-push", "Tenant Push", _job_config(tmp_path, real_push=True))

    with pytest.raises(ConfigValidationError, match="CDN_API_ENDPOINT"):
        JobService(store).enqueue(
            "tenant-push", "simulation", {}, actor="admin", push_confirmed=True
        )
