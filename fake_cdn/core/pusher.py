"""
日志推送客户端
实现HTTP推送、重试、批量处理
"""

import json
import os
import time
from datetime import datetime, timedelta
from typing import List, Dict, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .storage import CDNLogStorage


class LogPusher:
    """日志推送客户端"""

    def __init__(self, config: dict):
        self.config = config
        self.api_config = config["api"]

        # 创建带重试的HTTP session
        self.session = self._create_session()

        # 统计
        self.stats = {
            "total": 0,
            "success": 0,
            "failed": 0,
            "retries": 0,
        }

        # API 请求日志文件
        self.output_dir = config.get("output", {}).get("dir", "./output")
        os.makedirs(self.output_dir, exist_ok=True)
        self.api_log_file = os.path.join(self.output_dir, "api_requests.log")

    def _create_session(self) -> requests.Session:
        """
        创建HTTP会话,配置重试策略

        Linus说: "简单直接,别搞什么花里胡哨的"
        """
        session = requests.Session()

        # 重试策略: 只重试连接错误和5xx错误
        retry_strategy = Retry(
            total=self.api_config["retry"],
            backoff_factor=1,  # 1s, 2s, 4s
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["POST"]
        )

        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        return session

    def _log_api_request(self, log_entry: Dict, status_code: int, response_text: str, error: str = None):
        """记录 API 请求到日志文件"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(self.api_log_file, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*80}\n")
            f.write(f"[{timestamp}] POST {self.api_config['endpoint']}\n")
            f.write(f"Request: {json.dumps(log_entry, ensure_ascii=False)}\n")
            if error:
                f.write(f"Error: {error}\n")
            else:
                f.write(f"Response: HTTP {status_code} - {response_text}\n")

    def push_single(self, log_entry: Dict, dry_run: bool = False, verbose: bool = False) -> Tuple[bool, str]:
        """
        推送单条日志

        返回: (是否成功, 错误信息)
        """

        if dry_run:
            return True, "dry-run mode"

        try:
            if verbose:
                print(f"[API请求] POST {self.api_config['endpoint']}")
                print(f"[API请求体] {json.dumps(log_entry, ensure_ascii=False)}")

            response = self.session.post(
                self.api_config["endpoint"],
                json=log_entry,
                headers=self.api_config["headers"],
                timeout=self.api_config["timeout"]
            )

            if verbose:
                print(f"[API响应] HTTP {response.status_code}: {response.text[:500]}")

            # 记录到日志文件
            self._log_api_request(log_entry, response.status_code, response.text[:200])

            if response.status_code == 200:
                self.stats["success"] += 1
                return True, ""
            else:
                error_msg = f"HTTP {response.status_code}: {response.text[:200]}"
                self.stats["failed"] += 1
                return False, error_msg

        except requests.exceptions.RequestException as e:
            error_msg = f"请求异常: {str(e)}"
            self._log_api_request(log_entry, 0, "", error=error_msg)
            self.stats["failed"] += 1
            return False, error_msg

    def push_batch(self, log_entries: List[Dict], dry_run: bool = False) -> Dict:
        """
        批量推送日志

        注意: 原API不支持批量,这里是循环单条推送
        如果API支持批量,应该改为一次请求
        """

        self.stats["total"] += len(log_entries)

        results = {
            "success": 0,
            "failed": 0,
            "errors": []
        }

        for i, log_entry in enumerate(log_entries):
            success, error_msg = self.push_single(log_entry, dry_run)

            if success:
                results["success"] += 1
            else:
                results["failed"] += 1
                results["errors"].append({
                    "index": i,
                    "log": log_entry,
                    "error": error_msg
                })

            # 控制推送频率,避免打爆API
            if not dry_run and i < len(log_entries) - 1:
                time.sleep(0.01)  # 10ms间隔

        return results

    def push_all(self, all_logs: List[Dict], dry_run: bool = False, show_progress: bool = True):
        """
        推送所有日志,分批处理

        批量大小在config中配置
        """

        batch_size = self.api_config["batch_size"]
        total_batches = (len(all_logs) + batch_size - 1) // batch_size

        print(f"[推送] 开始推送 {len(all_logs)} 条日志, 分 {total_batches} 批")
        print(f"[模式] {'DRY-RUN (不真实推送)' if dry_run else '真实推送'}")

        start_time = time.time()

        for i in range(0, len(all_logs), batch_size):
            batch = all_logs[i:i + batch_size]
            batch_num = i // batch_size + 1

            result = self.push_batch(batch, dry_run)

            if show_progress and batch_num % 10 == 0:
                elapsed = time.time() - start_time
                speed = self.stats["total"] / elapsed if elapsed > 0 else 0
                print(f"  进度: {batch_num}/{total_batches} 批 "
                      f"({self.stats['total']} 条, "
                      f"{speed:.1f} 条/秒, "
                      f"成功率 {self.stats['success']/self.stats['total']*100:.1f}%)")

            # 如果失败率超过50%,停止推送
            if self.stats["total"] > 100:
                fail_rate = self.stats["failed"] / self.stats["total"]
                if fail_rate > 0.5:
                    print(f"[错误] 失败率过高 ({fail_rate*100:.1f}%), 停止推送")
                    break

        elapsed = time.time() - start_time

        print(f"[完成] 推送完成, 耗时 {elapsed:.1f} 秒")
        print(f"  总计: {self.stats['total']} 条")
        print(f"  成功: {self.stats['success']} 条")
        print(f"  失败: {self.stats['failed']} 条")

        return self.stats


class LocalSaver:
    """本地保存器 - 用于调试和审计"""

    _storage_instance = None

    @classmethod
    def get_storage(cls, output_dir: str) -> CDNLogStorage:
        """获取或创建 SQLite 存储实例"""
        if cls._storage_instance is None:
            db_path = os.path.join(output_dir, "cdn_logs.db")
            cls._storage_instance = CDNLogStorage(db_path)
        return cls._storage_instance

    @staticmethod
    def save_logs(logs: List[Dict], output_dir: str, filename: str = "cdn_logs.db"):
        """
        保存日志到 SQLite 数据库

        替代原有的 JSONL 存储，提升查询性能
        """
        os.makedirs(output_dir, exist_ok=True)
        storage = LocalSaver.get_storage(output_dir)
        storage.insert_logs(logs)

    @staticmethod
    def save_stats(stats: Dict, output_dir: str, filename: str = "stats.json"):
        """保存统计信息"""
        os.makedirs(output_dir, exist_ok=True)
        filepath = os.path.join(output_dir, filename)

        with open(filepath, 'w') as f:
            json.dump(stats, f, indent=2, ensure_ascii=False)

        print(f"[保存] 统计信息已保存到: {filepath}")

    @staticmethod
    def save_bandwidth_curve(bandwidth_curve: List[float], output_dir: str, filename: str = "bandwidth_curve.csv"):
        """保存带宽曲线 (CSV格式, 便于Excel分析)"""
        os.makedirs(output_dir, exist_ok=True)
        filepath = os.path.join(output_dir, filename)

        with open(filepath, 'w') as f:
            f.write("timestamp,bandwidth_gbps\n")

            start_time = datetime.now()
            for i, bw in enumerate(bandwidth_curve):
                ts = start_time + timedelta(seconds=i * 300)
                f.write(f"{ts.isoformat()},{bw:.4f}\n")

        print(f"[保存] 带宽曲线已保存到: {filepath}")
