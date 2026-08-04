from __future__ import annotations

from fake_cdn.dashboard.app import get_auth_config, is_public_auth_path, verify_password


def test_single_admin_authentication(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "strong-test-password")
    monkeypatch.delenv("DASHBOARD_USERS", raising=False)

    config = get_auth_config()

    assert config == {
        "enabled": True,
        "users": {"admin": "strong-test-password"},
    }
    assert verify_password("admin", "strong-test-password") is True
    assert verify_password("admin", "wrong-password") is False
    assert verify_password("viewer", "strong-test-password") is False


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
