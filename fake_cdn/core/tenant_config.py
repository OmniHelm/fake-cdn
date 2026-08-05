"""租户配置数据库、不可变版本、审计与生成任务管理。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Union

from fake_cdn.core.config_manager import (
    ConfigConflictError,
    ConfigManager,
    ConfigManagerError,
    ConfigValidationError,
)

TENANT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class TenantConfigStore:
    """以 SQLite 为配置事实来源，按租户管理草稿、发布与回滚。"""

    def __init__(self, db_path: Union[Path, str]):
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _get_conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tenants (
                    tenant_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'disabled')),
                    revision INTEGER NOT NULL DEFAULT 0,
                    active_version_id INTEGER,
                    latest_draft_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS config_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
                    version_no INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('draft', 'published', 'archived')),
                    config_json TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    remark TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    published_by TEXT,
                    published_at TEXT,
                    source_version_id INTEGER,
                    UNIQUE(tenant_id, version_no)
                );

                CREATE TABLE IF NOT EXISTS config_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    version_id INTEGER,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS generation_jobs (
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

                CREATE INDEX IF NOT EXISTS idx_config_versions_tenant
                    ON config_versions(tenant_id, version_no DESC);
                CREATE INDEX IF NOT EXISTS idx_config_audit_tenant
                    ON config_audit(tenant_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_generation_jobs_tenant
                    ON generation_jobs(tenant_id, created_at DESC);
                """)

    @staticmethod
    def validate(candidate: Dict) -> Dict:
        return ConfigManager.validate(candidate)

    @staticmethod
    def summarize(config: Dict) -> Dict:
        return ConfigManager.summarize(config)

    @staticmethod
    def validate_tenant_id(tenant_id: str) -> str:
        value = str(tenant_id or "").strip()
        if not TENANT_ID_PATTERN.fullmatch(value):
            raise ConfigValidationError(
                "tenant_id 只能包含字母、数字、点、下划线、冒号或短横线，长度 1-64"
            )
        return value

    @staticmethod
    def _row_to_version(row: sqlite3.Row) -> Dict:
        value = dict(row)
        value["config"] = json.loads(value.pop("config_json"))
        return value

    @staticmethod
    def _checksum(config: Dict) -> str:
        return hashlib.sha256(_canonical_json(config).encode("utf-8")).hexdigest()

    def _audit(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        actor: str,
        action: str,
        version_id: Optional[int] = None,
        detail: Optional[Dict] = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO config_audit
                (tenant_id, actor, action, version_id, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                tenant_id,
                actor or "system",
                action,
                version_id,
                _canonical_json(detail or {}),
                _utc_now(),
            ),
        )

    def tenant_exists(self, tenant_id: str) -> bool:
        with self._get_conn() as conn:
            return (
                conn.execute("SELECT 1 FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
                is not None
            )

    def create_tenant(
        self,
        tenant_id: str,
        display_name: str,
        base_config: Dict,
        *,
        actor: str = "admin",
        publish: bool = True,
        remark: str = "创建租户",
    ) -> Dict:
        tenant_id = self.validate_tenant_id(tenant_id)
        candidate = json.loads(json.dumps(base_config))
        candidate.setdefault("dimensions", {})["tenant_id"] = tenant_id
        normalized = self.validate(candidate)
        now = _utc_now()
        status = "published" if publish else "draft"
        try:
            with self._get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO tenants
                        (tenant_id, display_name, status, revision, created_at, updated_at)
                    VALUES (?, ?, 'active', 1, ?, ?)
                    """,
                    (tenant_id, (display_name or tenant_id).strip(), now, now),
                )
                cursor = conn.execute(
                    """
                    INSERT INTO config_versions
                        (tenant_id, version_no, status, config_json, checksum, remark,
                         created_by, created_at, published_by, published_at)
                    VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tenant_id,
                        status,
                        _canonical_json(normalized),
                        self._checksum(normalized),
                        remark,
                        actor,
                        now,
                        actor if publish else None,
                        now if publish else None,
                    ),
                )
                version_id = int(cursor.lastrowid)
                pointer = "active_version_id" if publish else "latest_draft_id"
                conn.execute(
                    f"UPDATE tenants SET {pointer} = ? WHERE tenant_id = ?",
                    (version_id, tenant_id),
                )
                self._audit(conn, tenant_id, actor, "tenant.create", version_id)
        except sqlite3.IntegrityError as exc:
            raise ConfigConflictError(f"租户 {tenant_id} 已存在") from exc
        return self.load(tenant_id, prefer_draft=not publish)

    def import_config_file(
        self,
        config_path: Union[Path, str],
        *,
        actor: str = "migration",
        display_name: Optional[str] = None,
    ) -> Dict:
        path = Path(config_path).expanduser().resolve()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigManagerError(f"无法导入配置 {path}: {exc}") from exc
        normalized = self.validate(payload)
        tenant_id = self.validate_tenant_id(normalized["dimensions"]["tenant_id"])
        if self.tenant_exists(tenant_id):
            return self.load(tenant_id)
        return self.create_tenant(
            tenant_id,
            display_name or tenant_id,
            normalized,
            actor=actor,
            publish=True,
            remark=f"从 {path.name} 导入",
        )

    def bootstrap(
        self,
        config_paths: Iterable[Union[Path, str]],
        *,
        allowed_tenant_ids: Optional[Iterable[str]] = None,
    ) -> List[Dict]:
        """校验并导入选中租户；任一选中配置无效时不写入任何租户。"""
        allowed = (
            None
            if allowed_tenant_ids is None
            else {self.validate_tenant_id(value) for value in allowed_tenant_ids}
        )
        validated = []
        errors = []

        for path in config_paths:
            resolved = Path(path).expanduser().resolve()
            if not resolved.exists():
                continue
            try:
                payload = json.loads(resolved.read_text(encoding="utf-8"))
                if not isinstance(payload, dict) or not isinstance(payload.get("dimensions"), dict):
                    raise ConfigValidationError("配置根节点必须包含 dimensions 对象")
                tenant_id = self.validate_tenant_id(payload["dimensions"].get("tenant_id"))
                if allowed is not None and tenant_id not in allowed:
                    continue
                self.validate(payload)
            except (OSError, json.JSONDecodeError, ConfigManagerError) as exc:
                errors.append(f"{resolved.name}: {exc}")
                continue
            validated.append((resolved, tenant_id))

        if errors:
            raise ConfigManagerError("租户配置导入失败: " + "; ".join(errors))

        imported: List[Dict] = []
        for resolved, tenant_id in validated:
            if not self.tenant_exists(tenant_id):
                imported.append(self.import_config_file(resolved))
        return imported

    def list_tenants(self) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT t.*,
                       av.version_no AS active_version_no,
                       av.checksum AS active_checksum,
                       dv.version_no AS draft_version_no,
                       (SELECT COUNT(*) FROM config_versions v
                        WHERE v.tenant_id = t.tenant_id) AS version_count
                FROM tenants t
                LEFT JOIN config_versions av ON av.id = t.active_version_id
                LEFT JOIN config_versions dv ON dv.id = t.latest_draft_id
                ORDER BY t.updated_at DESC, t.tenant_id
                """).fetchall()
        return [dict(row) for row in rows]

    def get_tenant(self, tenant_id: str) -> Dict:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
        if not row:
            raise ConfigManagerError(f"租户不存在: {tenant_id}")
        return dict(row)

    def load(self, tenant_id: str, *, prefer_draft: bool = True) -> Dict:
        tenant_id = self.validate_tenant_id(tenant_id)
        with self._get_conn() as conn:
            tenant = conn.execute(
                "SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
            if not tenant:
                raise ConfigManagerError(f"租户不存在: {tenant_id}")
            version_id = (
                tenant["latest_draft_id"]
                if prefer_draft and tenant["latest_draft_id"]
                else tenant["active_version_id"]
            )
            if not version_id:
                raise ConfigManagerError(f"租户 {tenant_id} 尚无可用配置")
            version = conn.execute(
                "SELECT * FROM config_versions WHERE id = ?", (version_id,)
            ).fetchone()
        result = self._row_to_version(version)
        result.update(
            {
                "tenant_id": tenant_id,
                "display_name": tenant["display_name"],
                "revision": str(tenant["revision"]),
                "summary": self.summarize(result["config"]),
                "path": str(self.db_path),
                "active_version_id": tenant["active_version_id"],
                "latest_draft_id": tenant["latest_draft_id"],
            }
        )
        return result

    def resolve_active(self, tenant_id: str) -> Dict:
        return self.load(tenant_id, prefer_draft=False)

    def save_draft(
        self,
        tenant_id: str,
        candidate: Dict,
        *,
        expected_revision: Optional[str],
        actor: str = "admin",
        remark: str = "保存草稿",
    ) -> Dict:
        tenant_id = self.validate_tenant_id(tenant_id)
        payload = json.loads(json.dumps(candidate))
        payload.setdefault("dimensions", {})["tenant_id"] = tenant_id
        normalized = self.validate(payload)
        now = _utc_now()
        with self._get_conn() as conn:
            tenant = conn.execute(
                "SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
            if not tenant:
                raise ConfigManagerError(f"租户不存在: {tenant_id}")
            if expected_revision and str(tenant["revision"]) != str(expected_revision):
                raise ConfigConflictError("配置已被其他会话修改，请刷新页面后重试")
            version_no = int(
                conn.execute(
                    "SELECT COALESCE(MAX(version_no), 0) + 1 FROM config_versions WHERE tenant_id = ?",
                    (tenant_id,),
                ).fetchone()[0]
            )
            cursor = conn.execute(
                """
                INSERT INTO config_versions
                    (tenant_id, version_no, status, config_json, checksum, remark,
                     created_by, created_at, source_version_id)
                VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?)
                """,
                (
                    tenant_id,
                    version_no,
                    _canonical_json(normalized),
                    self._checksum(normalized),
                    remark,
                    actor,
                    now,
                    tenant["active_version_id"],
                ),
            )
            version_id = int(cursor.lastrowid)
            conn.execute(
                """
                UPDATE tenants
                SET latest_draft_id = ?, revision = revision + 1, updated_at = ?
                WHERE tenant_id = ?
                """,
                (version_id, now, tenant_id),
            )
            self._audit(conn, tenant_id, actor, "config.draft.save", version_id)
        return self.load(tenant_id)

    def publish(
        self,
        tenant_id: str,
        version_id: Optional[int] = None,
        *,
        expected_revision: Optional[str] = None,
        actor: str = "admin",
    ) -> Dict:
        tenant_id = self.validate_tenant_id(tenant_id)
        now = _utc_now()
        with self._get_conn() as conn:
            tenant = conn.execute(
                "SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
            if not tenant:
                raise ConfigManagerError(f"租户不存在: {tenant_id}")
            if expected_revision and str(tenant["revision"]) != str(expected_revision):
                raise ConfigConflictError("配置已被其他会话修改，请刷新页面后重试")
            selected_id = version_id or tenant["latest_draft_id"]
            version = conn.execute(
                "SELECT * FROM config_versions WHERE id = ? AND tenant_id = ?",
                (selected_id, tenant_id),
            ).fetchone()
            if not version:
                raise ConfigManagerError("没有可发布的配置版本")
            if version["status"] == "archived":
                raise ConfigValidationError("历史版本请使用回滚操作，不能直接发布")
            conn.execute(
                "UPDATE config_versions SET status = 'archived' WHERE id = ?",
                (tenant["active_version_id"],),
            )
            conn.execute(
                """
                UPDATE config_versions
                SET status = 'published', published_by = ?, published_at = ?
                WHERE id = ?
                """,
                (actor, now, selected_id),
            )
            conn.execute(
                """
                UPDATE tenants
                SET active_version_id = ?,
                    latest_draft_id = CASE WHEN latest_draft_id = ? THEN NULL ELSE latest_draft_id END,
                    revision = revision + 1, updated_at = ?
                WHERE tenant_id = ?
                """,
                (selected_id, selected_id, now, tenant_id),
            )
            self._audit(conn, tenant_id, actor, "config.publish", selected_id)
        return self.resolve_active(tenant_id)

    def rollback(
        self,
        tenant_id: str,
        target_version_id: int,
        *,
        expected_revision: Optional[str] = None,
        actor: str = "admin",
    ) -> Dict:
        tenant = self.get_tenant(tenant_id)
        if expected_revision and str(tenant["revision"]) != str(expected_revision):
            raise ConfigConflictError("配置已被其他会话修改，请刷新页面后重试")
        with self._get_conn() as conn:
            target = conn.execute(
                "SELECT * FROM config_versions WHERE id = ? AND tenant_id = ?",
                (target_version_id, tenant_id),
            ).fetchone()
        if not target:
            raise ConfigManagerError("目标历史版本不存在")
        draft = self.save_draft(
            tenant_id,
            json.loads(target["config_json"]),
            expected_revision=str(tenant["revision"]),
            actor=actor,
            remark=f"回滚自 v{target['version_no']}",
        )
        result = self.publish(
            tenant_id,
            draft["id"],
            expected_revision=draft["revision"],
            actor=actor,
        )
        with self._get_conn() as conn:
            self._audit(
                conn,
                tenant_id,
                actor,
                "config.rollback",
                result["id"],
                {"source_version_id": target_version_id},
            )
        return result

    def versions(self, tenant_id: str, limit: int = 100) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT id, tenant_id, version_no, status, checksum, remark,
                       created_by, created_at, published_by, published_at, source_version_id
                FROM config_versions WHERE tenant_id = ?
                ORDER BY version_no DESC LIMIT ?
                """,
                (tenant_id, max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def read_audit(self, tenant_id: Optional[str] = None, limit: int = 100) -> List[Dict]:
        query = "SELECT * FROM config_audit"
        params: List[object] = []
        if tenant_id:
            query += " WHERE tenant_id = ?"
            params.append(tenant_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._get_conn() as conn:
            rows = conn.execute(query, params).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["detail"] = json.loads(value.pop("detail_json"))
            value["timestamp"] = value["created_at"]
            result.append(value)
        return result

    def create_job(
        self,
        tenant_id: str,
        config_version_id: int,
        mode: str,
        output_dir: str,
        *,
        actor: str = "cli",
    ) -> Dict:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        digest = hashlib.sha256(
            f"{tenant_id}:{config_version_id}:{mode}:{stamp}".encode("utf-8")
        ).hexdigest()[:10]
        job_id = f"job-{stamp}-{digest}"
        resolved_output_dir = str(
            Path(output_dir).expanduser() / "tenants" / tenant_id / "jobs" / job_id
        )
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO generation_jobs
                    (job_id, tenant_id, config_version_id, mode, status,
                     output_dir, created_by, created_at)
                VALUES (?, ?, ?, ?, 'running', ?, ?, ?)
                """,
                (
                    job_id,
                    tenant_id,
                    config_version_id,
                    mode,
                    resolved_output_dir,
                    actor,
                    _utc_now(),
                ),
            )
            self._audit(
                conn,
                tenant_id,
                actor,
                "job.create",
                config_version_id,
                {"job_id": job_id, "mode": mode},
            )
        return {"job_id": job_id, "output_dir": resolved_output_dir}

    def finish_job(self, job_id: str, status: str, stats: Optional[Dict] = None) -> None:
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ConfigValidationError(f"不支持的任务状态: {status}")
        with self._get_conn() as conn:
            conn.execute(
                """
                UPDATE generation_jobs
                SET status = ?, finished_at = ?, stats_json = ?
                WHERE job_id = ?
                """,
                (status, _utc_now(), _canonical_json(stats or {}), job_id),
            )


def get_default_config_store() -> TenantConfigStore:
    import os

    custom_path = os.environ.get("FAKE_CDN_CONFIG_DB_PATH")
    if custom_path:
        return TenantConfigStore(custom_path)
    project_root = Path(__file__).resolve().parents[2]
    return TenantConfigStore(project_root / "output" / "config.db")
