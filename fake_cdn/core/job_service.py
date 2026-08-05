"""生成任务的校验、排队与查询服务。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from fake_cdn.core.config_manager import ConfigConflictError, ConfigValidationError
from fake_cdn.core.generator import parse_datetime_in_timezone, parse_timezone
from fake_cdn.core.tenant_config import JOB_MODES, TenantConfigStore


class JobService:
    """统一管理 CLI 与 Dashboard 创建的租户任务。"""

    def __init__(self, store: TenantConfigStore):
        self.store = store

    @staticmethod
    def _canonical_json(value: Dict) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _normalize_datetime(value: str, timezone) -> tuple:
        resolved = parse_datetime_in_timezone(str(value), timezone)
        return resolved, resolved.strftime("%Y-%m-%d %H:%M:%S")

    @classmethod
    def normalize_parameters(
        cls,
        config: Dict,
        mode: str,
        parameters: Optional[Dict] = None,
        *,
        push_confirmed: bool = False,
    ) -> Dict:
        if mode not in JOB_MODES:
            raise ConfigValidationError(f"不支持的任务模式: {mode}")

        supplied = dict(parameters or {})
        allowed = {
            "simulation": {"force_dry_run"},
            "catchup": {"start_datetime", "end_datetime", "force_dry_run"},
            "realtime": {"end_datetime", "once", "force_dry_run"},
        }[mode]
        unknown = sorted(set(supplied) - allowed)
        if unknown:
            raise ConfigValidationError(f"任务参数不受支持: {', '.join(unknown)}")

        timezone = parse_timezone(config["time"]["timezone"])
        config_start, _ = cls._normalize_datetime(config["time"]["start_datetime"], timezone)
        config_end, _ = cls._normalize_datetime(config["time"]["end_datetime"], timezone)
        interval = int(config["time"]["interval_seconds"])

        normalized: Dict = {"force_dry_run": bool(supplied.get("force_dry_run", False))}
        if mode == "catchup":
            if not supplied.get("start_datetime") or not supplied.get("end_datetime"):
                raise ConfigValidationError("补推任务必须填写开始和结束时间")
            start, start_text = cls._normalize_datetime(supplied["start_datetime"], timezone)
            end, end_text = cls._normalize_datetime(supplied["end_datetime"], timezone)
            if end < start:
                raise ConfigValidationError("任务结束时间必须晚于或等于开始时间")
            if start < config_start or end > config_end:
                raise ConfigValidationError("补推时间必须位于已发布配置的时间窗口内")
            if int(start.timestamp()) % interval or int(end.timestamp()) % interval:
                raise ConfigValidationError(f"补推时间必须按 {interval} 秒粒度对齐")
            normalized.update({"start_datetime": start_text, "end_datetime": end_text})
        elif mode == "realtime":
            once = bool(supplied.get("once", False))
            now = datetime.now(timezone)
            if now < config_start or now > config_end:
                raise ConfigValidationError("当前时间不在已发布配置窗口内，不能启动实时任务")
            normalized["once"] = once
            if supplied.get("end_datetime"):
                end, end_text = cls._normalize_datetime(supplied["end_datetime"], timezone)
                if end <= now:
                    raise ConfigValidationError("实时任务结束时间必须晚于当前时间")
                if end > config_end:
                    raise ConfigValidationError("实时任务结束时间不能超过配置窗口")
                if int(end.timestamp()) % interval:
                    raise ConfigValidationError(f"实时任务结束时间必须按 {interval} 秒粒度对齐")
                normalized["end_datetime"] = end_text
            elif not once:
                raise ConfigValidationError("持续实时任务必须设置结束时间")

        effective_dry_run = bool(config["mode"].get("dry_run", True)) or normalized["force_dry_run"]
        normalized["effective_dry_run"] = effective_dry_run
        normalized["push_confirmed"] = bool(push_confirmed and not effective_dry_run)
        if not effective_dry_run:
            endpoint = os.environ.get("CDN_API_ENDPOINT") or config.get("api", {}).get("endpoint")
            vip = os.environ.get("CDN_API_VIP") or config.get("api", {}).get("headers", {}).get(
                "vip"
            )
            if not endpoint or not vip:
                raise ConfigValidationError("真实推送任务缺少 CDN_API_ENDPOINT 或 CDN_API_VIP")
        if not effective_dry_run and not push_confirmed:
            raise ConfigValidationError("真实推送任务需要管理员二次确认")
        return normalized

    @classmethod
    def _dedupe_key(cls, tenant_id: str, version_id: int, mode: str, parameters: Dict) -> str:
        payload = cls._canonical_json(
            {
                "tenant_id": tenant_id,
                "config_version_id": int(version_id),
                "mode": mode,
                "parameters": parameters,
            }
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def create_from_snapshot(
        self,
        snapshot: Dict,
        mode: str,
        parameters: Optional[Dict],
        *,
        actor: str,
        status: str,
        push_confirmed: bool = False,
        worker_id: Optional[str] = None,
        source_job_id: Optional[str] = None,
    ) -> Dict:
        if status not in {"queued", "running"}:
            raise ConfigValidationError(f"不支持的任务初始状态: {status}")
        normalized = self.normalize_parameters(
            snapshot["config"], mode, parameters, push_confirmed=push_confirmed
        )
        dedupe_key = self._dedupe_key(snapshot["tenant_id"], snapshot["id"], mode, normalized)
        return self.store.create_job(
            snapshot["tenant_id"],
            snapshot["id"],
            mode,
            snapshot["config"].get("mode", {}).get("output_dir", "./output"),
            actor=actor,
            status=status,
            parameters=normalized,
            dedupe_key=dedupe_key,
            worker_id=worker_id,
            source_job_id=source_job_id,
        )

    def enqueue(
        self,
        tenant_id: str,
        mode: str,
        parameters: Optional[Dict],
        *,
        actor: str,
        push_confirmed: bool = False,
    ) -> Dict:
        tenant = self.store.get_tenant(tenant_id)
        if tenant["status"] != "active":
            raise ConfigConflictError(f"租户已停用: {tenant_id}")
        snapshot = self.store.resolve_active(tenant_id)
        return self.create_from_snapshot(
            snapshot,
            mode,
            parameters,
            actor=actor,
            status="queued",
            push_confirmed=push_confirmed,
        )

    def create_direct(
        self,
        tenant_id: str,
        mode: str,
        parameters: Optional[Dict],
        *,
        actor: str = "cli",
        push_confirmed: bool = False,
        worker_id: Optional[str] = None,
    ) -> Dict:
        snapshot = self.store.resolve_active(tenant_id)
        return self.create_from_snapshot(
            snapshot,
            mode,
            parameters,
            actor=actor,
            status="running",
            push_confirmed=push_confirmed,
            worker_id=worker_id or actor,
        )

    def retry(self, job_id: str, *, actor: str, push_confirmed: bool = False) -> Dict:
        source = self.store.get_job(job_id)
        snapshot = self.store.resolve_version(source["tenant_id"], source["config_version_id"])
        parameters = {
            key: value
            for key, value in source["parameters"].items()
            if key not in {"effective_dry_run", "push_confirmed"}
        }
        return self.create_from_snapshot(
            snapshot,
            source["mode"],
            parameters,
            actor=actor,
            status="queued",
            push_confirmed=push_confirmed,
            source_job_id=source["job_id"],
        )

    def request_cancel(self, job_id: str, *, actor: str) -> Dict:
        return self.store.request_cancel_job(job_id, actor=actor)

    def active_snapshot(self, tenant_id: str) -> Dict:
        snapshot = self.store.resolve_active(tenant_id)
        summary = dict(snapshot["summary"])
        summary.update(
            {
                "tenant_id": snapshot["tenant_id"],
                "version_id": snapshot["id"],
                "version_no": snapshot["version_no"],
                "checksum": snapshot["checksum"],
                "time_start": snapshot["config"]["time"]["start_datetime"],
                "time_end": snapshot["config"]["time"]["end_datetime"],
                "timezone": snapshot["config"]["time"]["timezone"],
            }
        )
        return summary

    def counts(self) -> Dict[str, int]:
        return self.store.job_counts()

    def worker_online(self) -> bool:
        return any(worker["online"] for worker in self.store.list_workers())

    def read_log_tail(self, job_id: str, *, max_bytes: int = 64 * 1024) -> str:
        job = self.store.get_job(job_id)
        log_path = Path(job["log_path"] or Path(job["output_dir"]) / "job.log").resolve()
        output_dir = Path(job["output_dir"]).resolve()
        if output_dir not in log_path.parents:
            raise ConfigValidationError("任务日志路径越界")
        if not log_path.exists():
            return ""
        with log_path.open("rb") as file:
            size = file.seek(0, 2)
            file.seek(max(0, size - max(1024, int(max_bytes))))
            return file.read().decode("utf-8", errors="replace")
