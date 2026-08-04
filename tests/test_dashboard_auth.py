from __future__ import annotations

from pathlib import Path

from flask import Flask, session

from fake_cdn.dashboard.app import (
    create_app,
    create_sidebar,
    get_auth_config,
    is_public_auth_path,
    verify_password,
)
from fake_cdn.dashboard.auth import (
    authenticate_user,
    get_session_tenant_id,
    is_admin_session,
    resolve_tenant_scope,
)


def test_single_admin_authentication(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "strong-test-password")
    monkeypatch.delenv("DASHBOARD_USERS", raising=False)
    monkeypatch.delenv("DASHBOARD_TENANT_USERS", raising=False)

    config = get_auth_config()

    assert config == {
        "enabled": True,
        "users": {
            "admin": {
                "password": "strong-test-password",
                "account_type": "admin",
                "tenant_id": None,
            }
        },
    }
    assert verify_password("admin", "strong-test-password") is True
    assert verify_password("admin", "wrong-password") is False
    assert verify_password("viewer", "strong-test-password") is False


def test_tenant_user_authentication_and_scope(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "admin-password")
    monkeypatch.delenv("DASHBOARD_USERS", raising=False)
    monkeypatch.setenv(
        "DASHBOARD_TENANT_USERS",
        '{"alice":{"password":"alice-password","tenant_id":"hccl"}}',
    )
    config = get_auth_config()

    assert authenticate_user("alice", "alice-password", config) == {
        "username": "alice",
        "account_type": "tenant",
        "tenant_id": "hccl",
    }
    assert authenticate_user("alice", "wrong", config) is None

    app = Flask(__name__)
    app.secret_key = "test-secret"
    with app.test_request_context("/"):
        session["account_type"] = "tenant"
        session["tenant_id"] = "hccl"
        assert is_admin_session() is False
        assert get_session_tenant_id() == "hccl"
        assert resolve_tenant_scope("other-tenant") == "hccl"


def test_legacy_extra_users_remain_admins(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setenv("DASHBOARD_USERS", '{"ops":"ops-password"}')
    monkeypatch.delenv("DASHBOARD_TENANT_USERS", raising=False)

    assert authenticate_user("ops", "ops-password") == {
        "username": "ops",
        "account_type": "admin",
        "tenant_id": None,
    }


def test_tenant_user_login_has_neutral_initial_layout(tmp_path: Path, monkeypatch):
    source = Path(__file__).resolve().parent.parent / "config.json"
    config_path = tmp_path / "config.json"
    config_path.write_bytes(source.read_bytes())
    monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "admin-password")
    monkeypatch.setenv(
        "DASHBOARD_TENANT_USERS",
        '{"alice":{"password":"alice-password","tenant_id":"LITTLEHCCL"}}',
    )
    monkeypatch.setenv("FAKE_CDN_DB_PATH", str(tmp_path / "logs.db"))
    app = create_app(
        config_path=str(config_path), config_db_path=str(tmp_path / "config.db")
    )
    client = app.server.test_client()

    response = client.post(
        "/login",
        data={"username": "alice", "password": "alice-password"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/tenants/LITTLEHCCL/overview")

    layout = client.get("/_dash-layout")
    assert layout.status_code == 200
    assert "LITTLEHCCL" not in layout.get_data(as_text=True)

    user_sidebar = repr(
        create_sidebar("/tenants/LITTLEHCCL/overview", "LITTLEHCCL", is_admin=False)
    )
    assert "流量分析" in user_sidebar
    assert "配置管理" not in user_sidebar
    assert "审计日志" not in user_sidebar


def test_only_login_and_static_assets_are_public():
    assert is_public_auth_path("/login") is True
    assert is_public_auth_path("/logout") is True
    assert is_public_auth_path("/_favicon.ico") is True
    assert is_public_auth_path("/assets/config.css") is True
    assert is_public_auth_path("/_dash-component-suites/dash/renderer.js") is True

    assert is_public_auth_path("/config") is False
    assert is_public_auth_path("/config-audit") is False
    assert is_public_auth_path("/_dash-layout") is False
    assert is_public_auth_path("/_dash-dependencies") is False
    assert is_public_auth_path("/_dash-update-component") is False
