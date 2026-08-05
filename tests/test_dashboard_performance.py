from __future__ import annotations

import gzip
from pathlib import Path

import dash
from flask import session

import fake_cdn.dashboard.app as dashboard_module
from fake_cdn.core.storage import CDNLogStorage
from fake_cdn.core.tenant_config import TenantConfigStore
from fake_cdn.dashboard.app import create_app, create_overview_page


def _log(tenant_id: str, project: str, domain: str, start_time: int) -> dict:
    return {
        "start_time": start_time,
        "tenantId": tenant_id,
        "project": project,
        "domain": domain,
        "country": "sg",
        "region": "singapore",
        "interval": 300,
        "bw": 8_000,
        "flux": 1_000,
        "bs_bw": 800,
        "bs_flux": 100,
        "req_num": 100,
        "hit_num": 90,
        "bs_num": 10,
        "bs_fail_num": 1,
        "hit_flux": 900,
        "http_code_2xx": 98,
        "http_code_3xx": 1,
        "http_code_4xx": 1,
        "http_code_5xx": 0,
        "bs_http_code_2xx": 10,
        "bs_http_code_3xx": 0,
        "bs_http_code_4xx": 0,
        "bs_http_code_5xx": 0,
    }


def _build_app(tmp_path: Path, monkeypatch):
    source = Path(__file__).resolve().parent.parent / "config.json"
    config_path = tmp_path / "config.json"
    config_path.write_bytes(source.read_bytes())
    monkeypatch.setenv("FAKE_CDN_DB_PATH", str(tmp_path / "logs.db"))
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("DASHBOARD_USERS", raising=False)
    monkeypatch.delenv("DASHBOARD_TENANT_USERS", raising=False)
    return create_app(
        config_path=str(config_path),
        config_db_path=str(tmp_path / "config.db"),
    )


def test_dashboard_compresses_responses_and_caches_versioned_assets(tmp_path, monkeypatch):
    app = _build_app(tmp_path, monkeypatch)
    client = app.server.test_client()

    page = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert page.status_code == 200
    assert page.headers["Content-Encoding"] == "gzip"
    assert page.headers["Vary"] == "Accept-Encoding"
    assert b"CDN Panel" in gzip.decompress(page.data)

    asset = client.get("/assets/config.css?m=1", headers={"Accept-Encoding": "gzip"})
    assert asset.status_code == 200
    assert asset.headers["Content-Encoding"] == "gzip"
    assert asset.headers["Cache-Control"] == "public, max-age=31536000, immutable"

    unversioned = client.get("/_dash-component-suites/dash/dcc/async-graph.js")
    assert unversioned.status_code == 200
    assert unversioned.headers["Cache-Control"] == "public, max-age=86400"


def test_storage_time_range_stays_tenant_and_project_scoped(tmp_path):
    storage = CDNLogStorage(str(tmp_path / "logs.db"))
    storage.insert_logs(
        [
            _log("tenant-a", "project-a", "a.example.com", 1_000),
            _log("tenant-a", "project-b", "b.example.com", 3_000),
            _log("tenant-b", "project-a", "other.example.com", 4_000),
        ]
    )

    assert storage.get_time_range(tenant_id="tenant-a") == (1_000, 3_000)
    assert storage.get_time_range(tenant_id="tenant-b") == (4_000, 4_000)
    assert storage.get_time_range(project="project-a", tenant_id="tenant-a") == (1_000, 1_000)


def test_overview_reuses_aggregate_and_scopes_project_activity(tmp_path, monkeypatch):
    now_ms = int(dashboard_module.datetime.now(tz=dashboard_module.LOCAL_TZ).timestamp() * 1000)
    storage = CDNLogStorage(str(tmp_path / "logs.db"))
    storage.insert_logs(
        [
            _log("tenant-a", "project-a", "a.example.com", now_ms - 60_000),
            _log("tenant-b", "project-b", "b.example.com", now_ms - 60_000),
            _log("tenant-b", "project-c", "c.example.com", now_ms - 60_000),
        ]
    )

    original = dashboard_module._load_aggregate_meta
    calls = 0

    def counting_aggregate(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(dashboard_module, "_load_aggregate_meta", counting_aggregate)
    page = create_overview_page(storage, "tenant-a")

    assert calls == 1
    assert "活跃项目 1 个" in repr(page)
    assert "活跃项目 2 个" not in repr(page)


def test_static_route_does_not_scan_domain_filters(tmp_path, monkeypatch):
    app = _build_app(tmp_path, monkeypatch)
    config_store = TenantConfigStore(tmp_path / "config.db")
    tenant_id = config_store.list_tenants()[0]["tenant_id"]

    def unexpected_domain_scan(*_args, **_kwargs):
        raise AssertionError("静态页面不应扫描域名筛选项")

    monkeypatch.setattr(CDNLogStorage, "get_domains", unexpected_domain_scan)
    route_key = next(key for key in app.callback_map if "page-content.children" in key)
    render_page = app.callback_map[route_key]["callback"].__wrapped__

    with app.server.test_request_context(f"/tenants/{tenant_id}/cache"):
        session["account_type"] = "admin"
        page, _sidebar, _topbar, resolved_tenant = render_page(f"/tenants/{tenant_id}/cache")

    assert resolved_tenant == tenant_id
    assert "缓存" in repr(page)
    assert "refresh='callback-nav'" in repr(app.layout)


def test_analytics_poll_only_updates_when_data_revision_changes(tmp_path, monkeypatch):
    app = _build_app(tmp_path, monkeypatch)
    tenant_id = TenantConfigStore(tmp_path / "config.db").list_tenants()[0]["tenant_id"]
    poll_revision = app.callback_map["analytics-data-revision.data"]["callback"].__wrapped__

    with app.server.test_request_context("/"):
        session["account_type"] = "admin"
        assert poll_revision(1, None, tenant_id) is dash.no_update

    storage = CDNLogStorage(str(tmp_path / "logs.db"))
    storage.insert_logs([_log(tenant_id, "project-a", "a.example.com", 10_000)])

    with app.server.test_request_context("/"):
        session["account_type"] = "admin"
        assert poll_revision(2, None, tenant_id) == 10_000
        assert poll_revision(3, 10_000, tenant_id) is dash.no_update
