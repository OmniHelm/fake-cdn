"""日志推送与本地保存。"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Dict, Optional, Sequence, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from fake_cdn.core.generator import FluxPlanPoint, get_output_dir
from fake_cdn.core.storage import CDNLogStorage


class LogPusher:
    """日志推送客户端。"""

    def __init__(self, config: Dict):
        self.config = config
        self.api_config = config["api"]
        self.session = self._create_session()
        self.stats = {
            "total": 0,
            "success": 0,
            "failed": 0,
            "retries": 0,
        }
        self.output_dir = get_output_dir(config)
        os.makedirs(self.output_dir, exist_ok=True)
        self.api_log_file = os.path.join(self.output_dir, "api_requests.log")

    def _create_session(self) -> requests.Session:
        session = requests.Session()
        retry_strategy = Retry(
            total=self.api_config["retry"],
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["POST"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def _log_api_request(self, log_entry: Dict, status_code: int, response_text: str, error: Optional[str] = None) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(self.api_log_file, "a", encoding="utf-8") as file:
            file.write(f"\n{'=' * 80}\n")
            file.write(f"[{timestamp}] POST {self.api_config['endpoint']}\n")
            file.write(f"Request: {json.dumps(log_entry, ensure_ascii=False)}\n")
            if error:
                file.write(f"Error: {error}\n")
            else:
                file.write(f"Response: HTTP {status_code} - {response_text}\n")

    def push_single(self, log_entry: Dict, dry_run: bool = False, verbose: bool = False) -> Tuple[bool, str]:
        if dry_run:
            self.stats["success"] += 1
            return True, "dry-run mode"

        try:
            if verbose:
                print(f"[API请求] POST {self.api_config['endpoint']}")
                print(f"[API请求体] {json.dumps(log_entry, ensure_ascii=False)}")

            response = self.session.post(
                self.api_config["endpoint"],
                json=log_entry,
                headers=self.api_config["headers"],
                timeout=self.api_config["timeout"],
            )

            if verbose:
                print(f"[API响应] HTTP {response.status_code}: {response.text[:500]}")

            self._log_api_request(log_entry, response.status_code, response.text[:200])

            if response.status_code == 200:
                self.stats["success"] += 1
                return True, ""

            error_msg = f"HTTP {response.status_code}: {response.text[:200]}"
            self.stats["failed"] += 1
            return False, error_msg
        except requests.exceptions.RequestException as exc:
            error_msg = f"请求异常: {str(exc)}"
            self._log_api_request(log_entry, 0, "", error=error_msg)
            self.stats["failed"] += 1
            return False, error_msg

    def push_batch(self, log_entries: Sequence[Dict], dry_run: bool = False) -> Dict:
        self.stats["total"] += len(log_entries)
        results = {
            "success": 0,
            "failed": 0,
            "errors": [],
        }

        for index, log_entry in enumerate(log_entries):
            success, error_msg = self.push_single(log_entry, dry_run)
            if success:
                results["success"] += 1
            else:
                results["failed"] += 1
                results["errors"].append({"index": index, "log": log_entry, "error": error_msg})

            if not dry_run and index < len(log_entries) - 1:
                time.sleep(0.01)

        return results

    def push_all(self, all_logs: Sequence[Dict], dry_run: bool = False, show_progress: bool = True) -> Dict:
        batch_size = int(self.api_config["batch_size"])
        total_batches = (len(all_logs) + batch_size - 1) // batch_size if all_logs else 0
        print(f"[推送] 开始推送 {len(all_logs)} 条日志，分 {total_batches} 批")
        print(f"[模式] {'DRY-RUN (不真实推送)' if dry_run else '真实推送'}")

        start_time = time.time()
        for offset in range(0, len(all_logs), batch_size):
            batch = all_logs[offset: offset + batch_size]
            batch_num = offset // batch_size + 1
            self.push_batch(batch, dry_run)

            if show_progress and (batch_num % 10 == 0 or batch_num == total_batches):
                elapsed = max(time.time() - start_time, 0.001)
                speed = self.stats["total"] / elapsed
                success_rate = self.stats["success"] / max(self.stats["total"], 1) * 100
                print(
                    f"  进度: {batch_num}/{total_batches} 批 "
                    f"({self.stats['total']} 条, {speed:.1f} 条/秒, 成功率 {success_rate:.1f}%)"
                )

            if self.stats["total"] > 100:
                fail_rate = self.stats["failed"] / max(self.stats["total"], 1)
                if fail_rate > 0.5:
                    print(f"[错误] 失败率过高 ({fail_rate * 100:.1f}%)，停止推送")
                    break

        elapsed = time.time() - start_time
        print(f"[完成] 推送完成，耗时 {elapsed:.1f} 秒")
        print(f"  总计: {self.stats['total']} 条")
        print(f"  成功: {self.stats['success']} 条")
        print(f"  失败: {self.stats['failed']} 条")
        return self.stats


class LocalSaver:
    """本地保存器。"""

    _storage_instances: Dict[str, CDNLogStorage] = {}

    @classmethod
    def get_storage(cls, output_dir: str) -> CDNLogStorage:
        db_path = os.path.join(output_dir, "cdn_logs.db")
        if db_path not in cls._storage_instances:
            cls._storage_instances[db_path] = CDNLogStorage(db_path)
        return cls._storage_instances[db_path]

    @staticmethod
    def save_logs(
        logs: Sequence[Dict],
        output_dir: str,
        filename: str = "cdn_logs.db",
        db_path: Optional[str] = None,
    ) -> None:
        del filename  # 统一使用 SQLite，保留参数仅为兼容调用方签名。
        os.makedirs(output_dir, exist_ok=True)
        if db_path:
            resolved = os.path.abspath(db_path)
            if resolved not in LocalSaver._storage_instances:
                LocalSaver._storage_instances[resolved] = CDNLogStorage(resolved)
            storage = LocalSaver._storage_instances[resolved]
        else:
            storage = LocalSaver.get_storage(output_dir)
        storage.insert_logs(list(logs))

    @staticmethod
    def save_stats(stats: Dict, output_dir: str, filename: str = "stats.json") -> None:
        os.makedirs(output_dir, exist_ok=True)
        filepath = os.path.join(output_dir, filename)
        with open(filepath, "w", encoding="utf-8") as file:
            json.dump(stats, file, ensure_ascii=False, indent=2)
        print(f"[保存] 统计信息已保存到: {filepath}")

    @staticmethod
    def save_flux_curve(
        plan_points: Sequence[FluxPlanPoint],
        output_dir: str,
        filename: str = "flux_curve.csv",
        interval_seconds: Optional[int] = None,
    ) -> None:
        os.makedirs(output_dir, exist_ok=True)
        filepath = os.path.join(output_dir, filename)

        resolved_interval = interval_seconds
        if resolved_interval is None and len(plan_points) >= 2:
            resolved_interval = int((plan_points[1].timestamp_ms - plan_points[0].timestamp_ms) / 1000)
        resolved_interval = resolved_interval or 300

        with open(filepath, "w", encoding="utf-8") as file:
            file.write("timestamp,flux_bytes,equivalent_gbps\n")
            for point in plan_points:
                timestamp = datetime.fromtimestamp(point.timestamp_ms / 1000).isoformat()
                equivalent_gbps = point.flux_bytes * 8 / resolved_interval / 1_000_000_000
                file.write(f"{timestamp},{point.flux_bytes},{equivalent_gbps:.6f}\n")

        print(f"[保存] 流量曲线已保存到: {filepath}")
