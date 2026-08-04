"""后台配置的读取、校验、原子保存和审计。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from fake_cdn.core.generator import (
    TimeWindowBuilder,
    TrafficTargetParser,
    decimal_pb,
    normalize_config,
)

DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+" r"[a-zA-Z]{2,63}$"
)


class ConfigManagerError(RuntimeError):
    """配置管理基础异常。"""


class ConfigConflictError(ConfigManagerError):
    """配置在编辑期间已被其他进程修改。"""


class ConfigValidationError(ConfigManagerError):
    """配置不符合后台管理约束。"""


class ConfigManager:
    """以 ``config.json`` 为单一事实来源管理后台配置。

    保存时会先复用生成器的 ``normalize_config`` 校验，再通过 revision
    做乐观并发控制，最后使用同目录临时文件 + ``os.replace`` 原子替换。
    """

    def __init__(self, config_path: Path, audit_dir: Optional[Path] = None):
        self.config_path = Path(config_path).expanduser().resolve()
        self.audit_dir = (
            Path(audit_dir).expanduser().resolve()
            if audit_dir
            else self.config_path.parent / "output" / "config-management"
        )
        self._lock = threading.RLock()

    @staticmethod
    def _revision(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def _read_bytes(self) -> bytes:
        try:
            return self.config_path.read_bytes()
        except FileNotFoundError as exc:
            raise ConfigManagerError(f"配置文件不存在: {self.config_path}") from exc
        except OSError as exc:
            raise ConfigManagerError(f"无法读取配置文件: {exc}") from exc

    @staticmethod
    def _decode(content: bytes) -> Dict:
        try:
            decoded = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigValidationError(f"配置文件不是有效的 UTF-8 JSON: {exc}") from exc
        if not isinstance(decoded, dict):
            raise ConfigValidationError("配置根节点必须是 JSON 对象")
        return decoded

    @staticmethod
    def _validate_range(
        realism: Dict,
        key: str,
        *,
        minimum: float,
        maximum: Optional[float] = None,
        strictly_positive: bool = False,
    ) -> None:
        value = realism.get(key)
        if not isinstance(value, dict):
            raise ConfigValidationError(f"realism.{key} 必须包含 min / max")
        try:
            low = float(value["min"])
            high = float(value["max"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigValidationError(f"realism.{key}.min / max 必须是有效数字") from exc

        lower_bound_valid = low > minimum if strictly_positive else low >= minimum
        if not lower_bound_valid:
            operator = "大于" if strictly_positive else "大于等于"
            raise ConfigValidationError(f"realism.{key}.min 必须{operator} {minimum}")
        if high < low:
            raise ConfigValidationError(f"realism.{key}.max 必须大于等于 min")
        if maximum is not None and high > maximum:
            raise ConfigValidationError(f"realism.{key}.max 不能超过 {maximum}")

    @classmethod
    def _validate_management_rules(cls, config: Dict) -> None:
        domains = config["dimensions"]["domains"]
        names = [str(item["name"]).strip().lower() for item in domains]
        if len(names) != len(set(names)):
            raise ConfigValidationError("域名不能重复")
        for name in names:
            if not DOMAIN_PATTERN.fullmatch(name):
                raise ConfigValidationError(f"域名格式不正确: {name}")
        if any(float(item["weight"]) < 0 for item in domains):
            raise ConfigValidationError("域名权重不能为负数")

        if len(domains) > 1000:
            raise ConfigValidationError("单个配置最多支持 1000 个域名")
        if len(config["dimensions"]["regions"]) > 200:
            raise ConfigValidationError("单个配置最多支持 200 个地区")

        realism = config["realism"]
        for key in (
            "day_noise_ratio",
            "slot_noise_ratio",
            "month_edge_boost",
            "anomaly_probability",
        ):
            try:
                ratio = float(realism[key])
            except (KeyError, TypeError, ValueError) as exc:
                raise ConfigValidationError(f"realism.{key} 必须是有效数字") from exc
            if not 0 <= ratio <= 1:
                raise ConfigValidationError(f"realism.{key} 必须在 0 到 1 之间")

        weekday_factors = realism.get("weekday_factors", {})
        try:
            invalid_weekday = any(float(value) <= 0 for value in weekday_factors.values())
        except (TypeError, ValueError) as exc:
            raise ConfigValidationError("工作日流量因子必须是有效数字") from exc
        if invalid_weekday:
            raise ConfigValidationError("工作日流量因子必须大于 0")

        cls._validate_range(realism, "cache_hit_rate", minimum=0, maximum=1)
        cls._validate_range(realism, "origin_fail_rate", minimum=0, maximum=1)
        cls._validate_range(
            realism,
            "avg_object_size_kb",
            minimum=0,
            strictly_positive=True,
        )

        deployment_mode = config.get("deployment", {}).get("mode", "preview")
        if deployment_mode == "preview" and not config["mode"].get("dry_run", True):
            raise ConfigValidationError("预览模式必须启用 Dry-Run")

    @classmethod
    def validate(cls, candidate: Dict) -> Dict:
        """返回可落盘的标准化配置，不修改调用方传入的数据。"""
        try:
            normalized = normalize_config(candidate)
        except (TypeError, ValueError) as exc:
            raise ConfigValidationError(str(exc)) from exc
        cls._validate_management_rules(normalized)
        return normalized

    @staticmethod
    def summarize(config: Dict) -> Dict:
        slots = TimeWindowBuilder(config).build_slots()
        total_flux_bytes = TrafficTargetParser.parse_total_flux_bytes(config)
        interval_seconds = int(config["time"]["interval_seconds"])
        total_seconds = len(slots) * interval_seconds
        average_gbps = (
            total_flux_bytes * 8 / total_seconds / 1_000_000_000 if total_seconds else 0.0
        )
        domain_count = len(config["dimensions"]["domains"])
        region_count = len(config["dimensions"]["regions"])
        return {
            "target_total_flux_pb": decimal_pb(total_flux_bytes),
            "slot_count": len(slots),
            "estimated_record_count": len(slots) * domain_count * region_count,
            "equivalent_average_gbps": average_gbps,
            "domain_count": domain_count,
            "region_count": region_count,
            "dry_run": bool(config["mode"].get("dry_run", True)),
            "deployment_mode": config.get("deployment", {}).get("mode", "preview"),
        }

    def load(self) -> Dict:
        with self._lock:
            content = self._read_bytes()
            config = self.validate(self._decode(content))
            return {
                "config": config,
                "revision": self._revision(content),
                "summary": self.summarize(config),
                "path": str(self.config_path),
            }

    def _create_backup(self, content: bytes, revision: str) -> Path:
        backup_dir = self.audit_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup_path = backup_dir / f"{timestamp}-{revision[:12]}.json"
        backup_path.write_bytes(content)
        return backup_path

    def _append_audit(self, record: Dict) -> Optional[str]:
        try:
            self.audit_dir.mkdir(parents=True, exist_ok=True)
            audit_path = self.audit_dir / "audit.jsonl"
            with audit_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            return None
        except OSError as exc:
            return str(exc)

    def save(
        self,
        candidate: Dict,
        *,
        expected_revision: Optional[str],
        actor: str = "dashboard",
        action: str = "save",
    ) -> Dict:
        normalized = self.validate(candidate)
        serialized = (
            json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
        ).encode("utf-8")

        with self._lock:
            previous_content = self._read_bytes()
            previous_revision = self._revision(previous_content)
            if expected_revision and expected_revision != previous_revision:
                raise ConfigConflictError("配置已被其他会话修改，请刷新页面后重试")

            backup_path = self._create_backup(previous_content, previous_revision)
            temp_path: Optional[Path] = None
            try:
                file_mode = stat.S_IMODE(self.config_path.stat().st_mode)
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=str(self.config_path.parent),
                    prefix=f".{self.config_path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temp_file:
                    temp_file.write(serialized)
                    temp_file.flush()
                    os.fsync(temp_file.fileno())
                    temp_path = Path(temp_file.name)
                os.chmod(temp_path, file_mode)
                os.replace(temp_path, self.config_path)
                temp_path = None
            except OSError as exc:
                raise ConfigManagerError(f"保存配置失败: {exc}") from exc
            finally:
                if temp_path and temp_path.exists():
                    temp_path.unlink()

            new_revision = self._revision(serialized)
            audit_record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "actor": actor or "dashboard",
                "action": action,
                "previous_revision": previous_revision,
                "revision": new_revision,
                "backup": str(backup_path),
                "config_path": str(self.config_path),
            }
            audit_error = self._append_audit(audit_record)

            return {
                "config": normalized,
                "revision": new_revision,
                "summary": self.summarize(normalized),
                "path": str(self.config_path),
                "backup": str(backup_path),
                "audit_error": audit_error,
            }

    def read_audit(self, limit: int = 20) -> List[Dict]:
        audit_path = self.audit_dir / "audit.jsonl"
        if not audit_path.exists():
            return []
        try:
            lines = audit_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ConfigManagerError(f"读取配置审计日志失败: {exc}") from exc

        records: List[Dict] = []
        for line in reversed(lines[-max(1, limit) :]):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
        return records
