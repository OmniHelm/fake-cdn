"""Dashboard 的单管理员与租户绑定普通账号认证。"""

from __future__ import annotations

import json
import os
import secrets
from typing import Dict, Optional

from flask import session


def get_auth_config() -> Dict:
    """读取管理员、兼容管理员和租户用户配置。"""
    users: Dict[str, Dict] = {}
    admin_password = os.environ.get("DASHBOARD_PASSWORD", "")
    admin_username = os.environ.get("DASHBOARD_USERNAME", "admin")
    if admin_password:
        users[admin_username] = {
            "password": admin_password,
            "account_type": "admin",
            "tenant_id": None,
        }

    # 兼容旧配置：DASHBOARD_USERS 中的字符串密码仍视为额外管理员。
    legacy_users = os.environ.get("DASHBOARD_USERS", "")
    if legacy_users:
        try:
            parsed = json.loads(legacy_users)
            if isinstance(parsed, dict):
                for username, password in parsed.items():
                    if isinstance(password, str) and password:
                        normalized_username = str(username)
                        if normalized_username in users:
                            print(f"[警告] 账号 {normalized_username} 重复，保留已有管理员配置")
                            continue
                        users[normalized_username] = {
                            "password": password,
                            "account_type": "admin",
                            "tenant_id": None,
                        }
        except json.JSONDecodeError:
            print("[警告] DASHBOARD_USERS 格式错误，应为 JSON 对象")

    tenant_users = os.environ.get("DASHBOARD_TENANT_USERS", "")
    if tenant_users:
        try:
            parsed = json.loads(tenant_users)
            if isinstance(parsed, dict):
                for username, value in parsed.items():
                    normalized_username = str(username)
                    if normalized_username in users:
                        print(f"[警告] 账号 {normalized_username} 重复，保留已有账号配置")
                        continue
                    if not isinstance(value, dict):
                        print(f"[警告] 普通账号 {username} 配置必须是对象")
                        continue
                    password = value.get("password")
                    tenant_id = value.get("tenant_id")
                    if not isinstance(password, str) or not password or not tenant_id:
                        print(f"[警告] 普通账号 {username} 缺少 password 或 tenant_id")
                        continue
                    users[normalized_username] = {
                        "password": password,
                        "account_type": "tenant",
                        "tenant_id": str(tenant_id),
                    }
        except json.JSONDecodeError:
            print("[警告] DASHBOARD_TENANT_USERS 格式错误，应为 JSON 对象")

    return {"enabled": bool(users), "users": users}


def authenticate_user(
    username: str, password: str, config: Optional[Dict] = None
) -> Optional[Dict]:
    """使用常量时间比较凭据，成功时返回不含密码的会话身份。"""
    auth_config = config or get_auth_config()
    if not auth_config["enabled"]:
        return {
            "username": username or "admin",
            "account_type": "admin",
            "tenant_id": None,
        }

    matched: Optional[Dict] = None
    for valid_username, user in auth_config["users"].items():
        correct_username = secrets.compare_digest(str(username), str(valid_username))
        correct_password = secrets.compare_digest(str(password), str(user["password"]))
        if correct_username and correct_password:
            matched = {
                "username": valid_username,
                "account_type": user["account_type"],
                "tenant_id": user.get("tenant_id"),
            }
    return matched


def verify_password(username: str, password: str) -> bool:
    """兼容原有布尔验证接口。"""
    return authenticate_user(username, password) is not None


def is_public_auth_path(path: str) -> bool:
    """认证启用时仍允许匿名访问的登录页和静态资源。"""
    public_paths = {"/login", "/logout", "/favicon.ico", "/_favicon.ico"}
    return path in public_paths or path.startswith(("/assets/", "/_dash-component-suites/"))


def is_admin_session() -> bool:
    """未启用认证时默认保持管理员开发体验。"""
    return session.get("account_type", "admin") == "admin"


def get_session_tenant_id() -> Optional[str]:
    value = session.get("tenant_id")
    return str(value) if value else None


def resolve_tenant_scope(requested_tenant_id: Optional[str]) -> Optional[str]:
    """普通账号永远返回会话绑定租户，忽略客户端提交的 tenant_id。"""
    if is_admin_session():
        return requested_tenant_id
    return get_session_tenant_id()
