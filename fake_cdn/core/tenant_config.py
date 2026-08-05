"""租户配置数据库、不可变版本、审计与生成任务管理。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Union

from fake_cdn.core.config_manager import (
    ConfigConflictError,
    ConfigManager,
    ConfigManagerError,
    ConfigValidationError,
)

TENANT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
JOB_MODES = {"simulation", "realtime", "catchup"}
JOB_ACTIVE_STATUSES = {"queued", "running", "cancel_requested"}
JOB_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "interrupted"}


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
                    parameters_json TEXT NOT NULL DEFAULT '{}',
                    dedupe_key TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    heartbeat_at TEXT,
                    worker_id TEXT,
                    pid INTEGER,
                    cancel_requested_at TEXT,
                    log_path TEXT,
                    error_text TEXT,
                    stats_json TEXT
                );

                CREATE TABLE IF NOT EXISTS job_workers (
                    worker_id TEXT PRIMARY KEY,
                    hostname TEXT NOT NULL,
                    pid INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    stopped_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_config_versions_tenant
                    ON config_versions(tenant_id, version_no DESC);
                CREATE INDEX IF NOT EXISTS idx_config_audit_tenant
                    ON config_audit(tenant_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_generation_jobs_tenant
                    ON generation_jobs(tenant_id, created_at DESC);
                """)
            self._migrate_generation_jobs(conn)

    @staticmethod
    def _migrate_generation_jobs(conn: sqlite3.Connection) -> None:
        """补齐任务队列字段与索引，兼容已有配置数据库。"""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(generation_jobs)")}
        additions = {
            "parameters_json": "TEXT NOT NULL DEFAULT '{}'",
            "dedupe_key": "TEXT",
            "started_at": "TEXT",
            "heartbeat_at": "TEXT",
            "worker_id": "TEXT",
            "pid": "INTEGER",
            "cancel_requested_at": "TEXT",
            "log_path": "TEXT",
            "error_text": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE generation_jobs ADD COLUMN {name} {definition}")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_generation_jobs_status_created "
            "ON generation_jobs(status, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_generation_jobs_tenant_status "
            "ON generation_jobs(tenant_id, status, created_at DESC)"
        )
        duplicate_tenants = conn.execute("""
            SELECT tenant_id
            FROM generation_jobs
            WHERE status IN ('queued', 'running', 'cancel_requested')
            GROUP BY tenant_id
            HAVING COUNT(*) > 1
            """).fetchall()
        migration_time = _utc_now()
        for duplicate in duplicate_tenants:
            rows = conn.execute(
                """
                SELECT job_id FROM generation_jobs
                WHERE tenant_id = ?
                  AND status IN ('queued', 'running', 'cancel_requested')
                ORDER BY created_at DESC, job_id DESC
                """,
                (duplicate["tenant_id"],),
            ).fetchall()
            for row in rows[1:]:
                conn.execute(
                    """
                    UPDATE generation_jobs
                    SET status = 'interrupted', finished_at = ?, heartbeat_at = ?,
                        error_text = '队列升级时合并了重复的活动任务'
                    WHERE job_id = ?
                    """,
                    (migration_time, migration_time, row["job_id"]),
                )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_generation_jobs_active_tenant "
            "ON generation_jobs(tenant_id) "
            "WHERE status IN ('queued', 'running', 'cancel_requested')"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_generation_jobs_active_dedupe "
            "ON generation_jobs(dedupe_key) "
            "WHERE dedupe_key IS NOT NULL "
            "AND status IN ('queued', 'running', 'cancel_requested')"
        )

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
    def _row_to_job(row: sqlite3.Row) -> Dict:
        value = dict(row)
        value["parameters"] = json.loads(value.pop("parameters_json") or "{}")
        value["stats"] = json.loads(value.pop("stats_json") or "{}")
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

    def resolve_version(self, tenant_id: str, version_id: int) -> Dict:
        """读取指定租户的不可变配置版本，不受当前发布版本变化影响。"""
        tenant_id = self.validate_tenant_id(tenant_id)
        with self._get_conn() as conn:
            tenant = conn.execute(
                "SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
            version = conn.execute(
                "SELECT * FROM config_versions WHERE id = ? AND tenant_id = ?",
                (int(version_id), tenant_id),
            ).fetchone()
        if not tenant:
            raise ConfigManagerError(f"租户不存在: {tenant_id}")
        if not version:
            raise ConfigManagerError(f"配置版本不存在: {tenant_id} / {version_id}")
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
        status: str = "running",
        parameters: Optional[Dict] = None,
        dedupe_key: Optional[str] = None,
        worker_id: Optional[str] = None,
        source_job_id: Optional[str] = None,
    ) -> Dict:
        tenant_id = self.validate_tenant_id(tenant_id)
        if mode not in JOB_MODES:
            raise ConfigValidationError(f"不支持的任务模式: {mode}")
        if status not in JOB_ACTIVE_STATUSES:
            raise ConfigValidationError(f"不支持的任务初始状态: {status}")
        self.resolve_version(tenant_id, config_version_id)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        digest = hashlib.sha256(
            f"{tenant_id}:{config_version_id}:{mode}:{stamp}".encode("utf-8")
        ).hexdigest()[:10]
        job_id = f"job-{stamp}-{digest}"
        resolved_output_dir = str(
            Path(output_dir).expanduser() / "tenants" / tenant_id / "jobs" / job_id
        )
        log_path = str(Path(resolved_output_dir) / "job.log")
        now = _utc_now()
        try:
            with self._get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO generation_jobs
                        (job_id, tenant_id, config_version_id, mode, status,
                         output_dir, created_by, created_at, parameters_json,
                         dedupe_key, started_at, heartbeat_at, log_path, worker_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        tenant_id,
                        config_version_id,
                        mode,
                        status,
                        resolved_output_dir,
                        actor,
                        now,
                        _canonical_json(parameters or {}),
                        dedupe_key,
                        now if status == "running" else None,
                        now if status == "running" else None,
                        log_path,
                        worker_id if status == "running" else None,
                    ),
                )
                audit_detail = {"job_id": job_id, "mode": mode, "status": status}
                if source_job_id:
                    audit_detail["source_job_id"] = source_job_id
                self._audit(
                    conn,
                    tenant_id,
                    actor,
                    (
                        "job.retry"
                        if source_job_id
                        else ("job.enqueue" if status == "queued" else "job.create")
                    ),
                    config_version_id,
                    audit_detail,
                )
        except sqlite3.IntegrityError as exc:
            raise ConfigConflictError(f"租户 {tenant_id} 已有排队或运行中的任务") from exc
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> Dict:
        with self._get_conn() as conn:
            row = conn.execute(
                """
                SELECT j.*, v.version_no, v.checksum AS config_checksum,
                       t.display_name
                FROM generation_jobs j
                JOIN config_versions v ON v.id = j.config_version_id
                JOIN tenants t ON t.tenant_id = j.tenant_id
                WHERE j.job_id = ?
                """,
                (job_id,),
            ).fetchone()
        if not row:
            raise ConfigManagerError(f"任务不存在: {job_id}")
        return self._row_to_job(row)

    def list_jobs(
        self,
        *,
        tenant_id: Optional[str] = None,
        status: Optional[str] = None,
        mode: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict]:
        where = []
        params: List[object] = []
        if tenant_id:
            where.append("j.tenant_id = ?")
            params.append(self.validate_tenant_id(tenant_id))
        if status:
            where.append("j.status = ?")
            params.append(status)
        if mode:
            if mode not in JOB_MODES:
                raise ConfigValidationError(f"不支持的任务模式: {mode}")
            where.append("j.mode = ?")
            params.append(mode)
        query = """
            SELECT j.*, v.version_no, v.checksum AS config_checksum,
                   t.display_name
            FROM generation_jobs j
            JOIN config_versions v ON v.id = j.config_version_id
            JOIN tenants t ON t.tenant_id = j.tenant_id
        """
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY j.created_at DESC LIMIT ? OFFSET ?"
        params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
        with self._get_conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_job(row) for row in rows]

    def job_counts(self) -> Dict[str, int]:
        """返回全量任务状态统计，不受任务列表分页限制。"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM generation_jobs GROUP BY status"
            ).fetchall()
        counts = {"all": sum(int(row["count"]) for row in rows)}
        counts.update({row["status"]: int(row["count"]) for row in rows})
        counts["active"] = sum(counts.get(status, 0) for status in JOB_ACTIVE_STATUSES)
        return counts

    def claim_job(self, job_id: str, worker_id: str) -> Optional[Dict]:
        """原子领取指定排队任务，避免多个 Worker 重复执行。"""
        now = _utc_now()
        with self._get_conn() as conn:
            cursor = conn.execute(
                """
                UPDATE generation_jobs
                SET status = 'running', worker_id = ?, started_at = ?, heartbeat_at = ?
                WHERE job_id = ? AND status = 'queued'
                """,
                (worker_id, now, now, job_id),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT tenant_id, config_version_id, mode FROM generation_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            self._audit(
                conn,
                row["tenant_id"],
                worker_id,
                "job.start",
                row["config_version_id"],
                {"job_id": job_id, "mode": row["mode"]},
            )
        return self.get_job(job_id)

    def claim_next_job(
        self, worker_id: str, *, modes: Optional[Iterable[str]] = None
    ) -> Optional[Dict]:
        allowed = sorted(set(modes or JOB_MODES))
        if not allowed or any(mode not in JOB_MODES for mode in allowed):
            raise ConfigValidationError("Worker 任务模式无效")
        placeholders = ",".join("?" for _ in allowed)
        with self._get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"""
                SELECT job_id FROM generation_jobs
                WHERE status = 'queued' AND mode IN ({placeholders})
                ORDER BY created_at ASC LIMIT 1
                """,
                allowed,
            ).fetchone()
            if not row:
                return None
            job_id = row["job_id"]
            now = _utc_now()
            cursor = conn.execute(
                """
                UPDATE generation_jobs
                SET status = 'running', worker_id = ?, started_at = ?, heartbeat_at = ?
                WHERE job_id = ? AND status = 'queued'
                """,
                (worker_id, now, now, job_id),
            )
            if cursor.rowcount != 1:
                return None
            started = conn.execute(
                "SELECT tenant_id, config_version_id, mode FROM generation_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            self._audit(
                conn,
                started["tenant_id"],
                worker_id,
                "job.start",
                started["config_version_id"],
                {"job_id": job_id, "mode": started["mode"]},
            )
        return self.get_job(job_id)

    def set_job_process(self, job_id: str, worker_id: str, pid: int) -> None:
        with self._get_conn() as conn:
            cursor = conn.execute(
                """
                UPDATE generation_jobs
                SET worker_id = ?, pid = ?, heartbeat_at = ?
                WHERE job_id = ? AND status IN ('running', 'cancel_requested')
                """,
                (worker_id, int(pid), _utc_now(), job_id),
            )
        if cursor.rowcount != 1:
            raise ConfigConflictError(f"任务不在可运行状态: {job_id}")

    def heartbeat_job(self, job_id: str, *, pid: Optional[int] = None) -> bool:
        assignments = ["heartbeat_at = ?"]
        params: List[object] = [_utc_now()]
        if pid is not None:
            assignments.append("pid = ?")
            params.append(int(pid))
        params.append(job_id)
        with self._get_conn() as conn:
            cursor = conn.execute(
                f"UPDATE generation_jobs SET {', '.join(assignments)} "
                "WHERE job_id = ? AND status IN ('running', 'cancel_requested')",
                params,
            )
        return cursor.rowcount == 1

    def request_cancel_job(self, job_id: str, *, actor: str = "admin") -> Dict:
        now = _utc_now()
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM generation_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if not row:
                raise ConfigManagerError(f"任务不存在: {job_id}")
            if row["status"] == "queued":
                next_status = "cancelled"
                conn.execute(
                    """
                    UPDATE generation_jobs
                    SET status = 'cancelled', cancel_requested_at = ?, finished_at = ?
                    WHERE job_id = ?
                    """,
                    (now, now, job_id),
                )
                action = "job.cancel"
            elif row["status"] == "running":
                next_status = "cancel_requested"
                conn.execute(
                    """
                    UPDATE generation_jobs
                    SET status = 'cancel_requested', cancel_requested_at = ?
                    WHERE job_id = ?
                    """,
                    (now, job_id),
                )
                action = "job.cancel.request"
            elif row["status"] == "cancel_requested":
                return self.get_job(job_id)
            else:
                raise ConfigConflictError(f"任务已经结束，当前状态: {row['status']}")
            self._audit(
                conn,
                row["tenant_id"],
                actor,
                action,
                row["config_version_id"],
                {"job_id": job_id, "previous_status": row["status"], "status": next_status},
            )
        return self.get_job(job_id)

    def finish_job(
        self,
        job_id: str,
        status: str,
        stats: Optional[Dict] = None,
        *,
        error_text: Optional[str] = None,
        actor: str = "worker",
    ) -> Dict:
        if status not in JOB_TERMINAL_STATUSES:
            raise ConfigValidationError(f"不支持的任务状态: {status}")
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM generation_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if not row:
                raise ConfigManagerError(f"任务不存在: {job_id}")
            if row["status"] in JOB_TERMINAL_STATUSES:
                return self.get_job(job_id)
            resolved_status = "cancelled" if row["status"] == "cancel_requested" else status
            conn.execute(
                """
                UPDATE generation_jobs
                SET status = ?, finished_at = ?, heartbeat_at = ?, stats_json = ?, error_text = ?
                WHERE job_id = ?
                """,
                (
                    resolved_status,
                    _utc_now(),
                    _utc_now(),
                    _canonical_json(stats or {}),
                    error_text,
                    job_id,
                ),
            )
            self._audit(
                conn,
                row["tenant_id"],
                actor,
                f"job.{resolved_status}",
                row["config_version_id"],
                {"job_id": job_id, "mode": row["mode"], "error": error_text or ""},
            )
        return self.get_job(job_id)

    def recover_active_jobs(self, *, actor: str = "worker") -> int:
        """Worker 启动时中断上次进程遗留的活动任务。"""
        now = _utc_now()
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM generation_jobs
                WHERE status IN ('running', 'cancel_requested')
                """).fetchall()
            recovered_rows = []
            for row in rows:
                owner = row["worker_id"]
                known_worker = (
                    owner
                    and conn.execute(
                        "SELECT 1 FROM job_workers WHERE worker_id = ?", (owner,)
                    ).fetchone()
                )
                pid = row["pid"]
                process_alive = False
                if pid:
                    try:
                        os.kill(int(pid), 0)
                        process_alive = True
                    except (OSError, ValueError):
                        process_alive = False
                if process_alive:
                    continue
                if owner and not known_worker and not pid:
                    continue
                conn.execute(
                    """
                    UPDATE generation_jobs
                    SET status = 'interrupted', finished_at = ?, heartbeat_at = ?,
                        error_text = 'Worker 重启，原任务进程已中断'
                    WHERE job_id = ?
                    """,
                    (now, now, row["job_id"]),
                )
                recovered_rows.append(row)
                self._audit(
                    conn,
                    row["tenant_id"],
                    actor,
                    "job.interrupted",
                    row["config_version_id"],
                    {"job_id": row["job_id"], "reason": "worker_restart"},
                )
        return len(recovered_rows)

    def register_worker(self, worker_id: str, hostname: str, pid: int) -> None:
        now = _utc_now()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO job_workers
                    (worker_id, hostname, pid, status, started_at, heartbeat_at, stopped_at)
                VALUES (?, ?, ?, 'online', ?, ?, NULL)
                ON CONFLICT(worker_id) DO UPDATE SET
                    hostname = excluded.hostname,
                    pid = excluded.pid,
                    status = 'online',
                    started_at = excluded.started_at,
                    heartbeat_at = excluded.heartbeat_at,
                    stopped_at = NULL
                """,
                (worker_id, hostname, int(pid), now, now),
            )

    def heartbeat_worker(self, worker_id: str) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE job_workers SET status = 'online', heartbeat_at = ? WHERE worker_id = ?",
                (_utc_now(), worker_id),
            )

    def stop_worker(self, worker_id: str) -> None:
        now = _utc_now()
        with self._get_conn() as conn:
            conn.execute(
                """
                UPDATE job_workers
                SET status = 'stopped', heartbeat_at = ?, stopped_at = ?
                WHERE worker_id = ?
                """,
                (now, now, worker_id),
            )

    def list_workers(self, *, stale_after_seconds: int = 15) -> List[Dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(1, stale_after_seconds))
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM job_workers ORDER BY heartbeat_at DESC LIMIT 20"
            ).fetchall()
        workers = []
        for row in rows:
            value = dict(row)
            heartbeat = datetime.fromisoformat(value["heartbeat_at"])
            value["online"] = value["status"] == "online" and heartbeat >= cutoff
            workers.append(value)
        return workers


def get_default_config_store() -> TenantConfigStore:
    import os

    custom_path = os.environ.get("FAKE_CDN_CONFIG_DB_PATH")
    if custom_path:
        return TenantConfigStore(custom_path)
    project_root = Path(__file__).resolve().parents[2]
    return TenantConfigStore(project_root / "output" / "config.db")
