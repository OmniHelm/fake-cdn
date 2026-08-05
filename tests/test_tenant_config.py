from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from fake_cdn.core.config_manager import ConfigConflictError, ConfigManagerError
from fake_cdn.core.generator import CDNLogGenerator
from fake_cdn.core.pusher import LocalSaver
from fake_cdn.core.storage import CDNLogStorage
from fake_cdn.core.tenant_config import TenantConfigStore
from fake_cdn.dashboard.app import _build_where, create_app, load_analytics_data


def load_base_config() -> dict:
    path = Path(__file__).resolve().parent.parent / "config.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_tenant_draft_publish_and_rollback_are_versioned(tmp_path: Path):
    store = TenantConfigStore(tmp_path / "config.db")
    created = store.create_tenant("tenant-a", "Tenant A", load_base_config(), actor="tester")
    assert created["status"] == "published"
    assert created["config"]["dimensions"]["tenant_id"] == "tenant-a"

    candidate = deepcopy(created["config"])
    candidate["target"]["total_flux"]["value"] = 1.25
    draft = store.save_draft(
        "tenant-a", candidate, expected_revision=created["revision"], actor="tester"
    )

    assert draft["status"] == "draft"
    assert store.resolve_active("tenant-a")["config"]["target"]["total_flux"]["value"] != 1.25

    published = store.publish(
        "tenant-a", draft["id"], expected_revision=draft["revision"], actor="tester"
    )
    assert published["version_no"] == 2
    assert published["config"]["target"]["total_flux"]["value"] == 1.25

    rolled_back = store.rollback(
        "tenant-a",
        created["id"],
        expected_revision=published["revision"],
        actor="tester",
    )
    assert rolled_back["version_no"] == 3
    assert (
        rolled_back["config"]["target"]["total_flux"]["value"]
        == created["config"]["target"]["total_flux"]["value"]
    )
    assert [item["action"] for item in store.read_audit("tenant-a")] == [
        "config.rollback",
        "config.publish",
        "config.draft.save",
        "config.publish",
        "config.draft.save",
        "tenant.create",
    ]


def test_tenant_revision_conflict_and_exact_id_migration(tmp_path: Path):
    store = TenantConfigStore(tmp_path / "config.db")
    config_a = load_base_config()
    config_a["dimensions"]["tenant_id"] = "hccl"
    config_b = deepcopy(config_a)
    config_b["dimensions"]["tenant_id"] = "LITTLEHCCL"
    path_a = tmp_path / "config-a.json"
    path_b = tmp_path / "config-b.json"
    path_a.write_text(json.dumps(config_a), encoding="utf-8")
    path_b.write_text(json.dumps(config_b), encoding="utf-8")

    store.bootstrap([path_a, path_b])
    assert {item["tenant_id"] for item in store.list_tenants()} == {"hccl", "LITTLEHCCL"}

    snapshot = store.load("hccl")
    store.save_draft("hccl", snapshot["config"], expected_revision=snapshot["revision"])
    with pytest.raises(ConfigConflictError):
        store.save_draft("hccl", snapshot["config"], expected_revision=snapshot["revision"])


def test_repository_configs_support_existing_log_tenants(tmp_path: Path):
    root = Path(__file__).resolve().parent.parent
    paths = sorted(root.glob("config*.json"))
    store = TenantConfigStore(tmp_path / "config.db")

    store.bootstrap([root / "config.json"])
    store.bootstrap(
        [path for path in paths if path.name != "config.json"],
        allowed_tenant_ids={"LITTLEHCCL", "770CDN26633384HCCL"},
    )

    assert {item["tenant_id"] for item in store.list_tenants()} == {
        "LITTLEHCCL",
        "770CDN26633384HCCL",
    }
    backfill = store.resolve_active("770CDN26633384HCCL")["config"]
    assert backfill["target"]["total_flux"] == {
        "value": 13.392,
        "unit": "PB",
        "base": 1000,
    }


def test_bootstrap_validation_failure_is_atomic(tmp_path: Path):
    valid = load_base_config()
    valid["dimensions"]["tenant_id"] = "tenant-valid"
    invalid = deepcopy(valid)
    invalid["dimensions"]["tenant_id"] = "tenant-invalid"
    del invalid["target"]["total_flux"]

    valid_path = tmp_path / "config-valid.json"
    invalid_path = tmp_path / "config-invalid.json"
    valid_path.write_text(json.dumps(valid), encoding="utf-8")
    invalid_path.write_text(json.dumps(invalid), encoding="utf-8")
    store = TenantConfigStore(tmp_path / "config.db")

    with pytest.raises(ConfigManagerError, match="config-invalid.json"):
        store.bootstrap(
            [valid_path, invalid_path],
            allowed_tenant_ids={"tenant-valid", "tenant-invalid"},
        )

    assert store.list_tenants() == []
    store.bootstrap([invalid_path], allowed_tenant_ids={"another-tenant"})
    assert store.list_tenants() == []


def _log(tenant_id: str, flux: int, domain: str) -> dict:
    return {
        "start_time": 1_800_000_000_000,
        "tenantId": tenant_id,
        "project": "same-project",
        "domain": domain,
        "country": "sg",
        "region": "singapore",
        "interval": 300,
        "bw": flux * 8,
        "flux": flux,
        "bs_bw": 0,
        "bs_flux": 0,
        "req_num": 100,
        "hit_num": 90,
        "bs_num": 10,
        "bs_fail_num": 0,
        "hit_flux": int(flux * 0.9),
        "http_code_2xx": 100,
        "http_code_3xx": 0,
        "http_code_4xx": 0,
        "http_code_5xx": 0,
        "bs_http_code_2xx": 10,
        "bs_http_code_3xx": 0,
        "bs_http_code_4xx": 0,
        "bs_http_code_5xx": 0,
    }


def test_storage_and_dashboard_queries_are_tenant_isolated(tmp_path: Path):
    storage = CDNLogStorage(str(tmp_path / "logs.db"))
    storage.insert_logs(
        [
            _log("tenant-a", 1000, "a.example.com"),
            _log("tenant-b", 9000, "b.example.com"),
        ]
    )

    assert storage.get_record_count("tenant-a") == 1
    assert storage.get_domains(tenant_id="tenant-a") == ["a.example.com"]
    assert storage.get_domains(tenant_id="tenant-b") == ["b.example.com"]
    assert storage.query_logs(tenant_id="tenant-a")[0]["flux"] == 1000

    analytics = load_analytics_data(storage, "tenant-a")
    assert analytics["meta"]["total_records"] == 1
    assert analytics["meta"]["total_flux"] == 1000
    where, params = _build_where("tenant-a", project="same-project")
    assert where.startswith("tenant_id = ?")
    assert params == ["tenant-a", "same-project"]
    with pytest.raises(ValueError, match="tenant_id"):
        _build_where(None)


def test_dashboard_bootstrap_uses_log_tenant_scope(tmp_path: Path, monkeypatch):
    root = Path(__file__).resolve().parent.parent
    config_root = tmp_path / "configs"
    config_root.mkdir()
    for source in root.glob("config*.json"):
        (config_root / source.name).write_bytes(source.read_bytes())

    logs_db = tmp_path / "logs.db"
    storage = CDNLogStorage(str(logs_db))
    storage.insert_logs(
        [
            _log("LITTLEHCCL", 1000, "little.example.com"),
            _log("770CDN26633384HCCL", 9000, "backfill.example.com"),
        ]
    )
    config_db = tmp_path / "config.db"
    monkeypatch.setenv("FAKE_CDN_DB_PATH", str(logs_db))
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("DASHBOARD_USERS", raising=False)
    monkeypatch.delenv("DASHBOARD_TENANT_USERS", raising=False)

    create_app(
        config_path=str(config_root / "config.json"),
        config_db_path=str(config_db),
    )

    assert {item["tenant_id"] for item in TenantConfigStore(config_db).list_tenants()} == {
        "LITTLEHCCL",
        "770CDN26633384HCCL",
    }


def test_generation_job_metadata_reaches_logs_and_tenant_output(tmp_path: Path):
    store = TenantConfigStore(tmp_path / "config.db")
    snapshot = store.create_tenant("tenant-a", "Tenant A", load_base_config())
    job = store.create_job("tenant-a", snapshot["id"], "simulation", str(tmp_path / "output"))
    config = deepcopy(snapshot["config"])
    config["time"]["end_datetime"] = config["time"]["start_datetime"]
    config["dimensions"]["domains"] = config["dimensions"]["domains"][:1]
    config["dimensions"]["regions"] = config["dimensions"]["regions"][:1]
    config["_runtime"] = {
        "config_version_id": snapshot["id"],
        "generation_job_id": job["job_id"],
    }
    log = CDNLogGenerator(config).generate_window_logs()[0][0]

    assert log["tenantId"] == "tenant-a"
    assert log["configVersionId"] == snapshot["id"]
    assert log["generationJobId"] == job["job_id"]
    assert f"tenants/tenant-a/jobs/{job['job_id']}" in job["output_dir"]

    central_db = tmp_path / "central-logs.db"
    LocalSaver.save_logs([log], job["output_dir"], db_path=str(central_db))
    assert central_db.exists()
    assert not (Path(job["output_dir"]) / "cdn_logs.db").exists()
    assert (
        CDNLogStorage(str(central_db)).query_logs(tenant_id="tenant-a")[0]["generation_job_id"]
        == job["job_id"]
    )


def test_cli_tenant_run_uses_published_config_and_central_log_db(tmp_path: Path):
    config = load_base_config()
    config["dimensions"]["tenant_id"] = "tenant-cli"
    config["dimensions"]["domains"] = config["dimensions"]["domains"][:1]
    config["dimensions"]["regions"] = config["dimensions"]["regions"][:1]
    config["time"]["end_datetime"] = config["time"]["start_datetime"]
    config["target"]["total_flux"]["value"] = 0.000001
    config["mode"]["output_dir"] = str(tmp_path / "artifacts")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    config_db = tmp_path / "config.db"
    TenantConfigStore(config_db).import_config_file(config_path)
    central_db = tmp_path / "central.db"
    env = dict(os.environ)
    env["FAKE_CDN_DB_PATH"] = str(central_db)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "fake_cdn",
            "simulation",
            "--tenant-id",
            "tenant-cli",
            "--config",
            str(config_path),
            "--config-db",
            str(config_db),
            "--dry-run",
        ],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    stats = list((tmp_path / "artifacts" / "tenants" / "tenant-cli" / "jobs").glob("*/stats.json"))
    assert len(stats) == 1
    assert not list(stats[0].parent.glob("cdn_logs.db"))
    with sqlite3.connect(central_db) as conn:
        row = conn.execute(
            "SELECT tenant_id, config_version_id, generation_job_id FROM cdn_logs"
        ).fetchone()
    assert row[0] == "tenant-cli"
    assert row[1] is not None
    assert row[2].startswith("job-")
