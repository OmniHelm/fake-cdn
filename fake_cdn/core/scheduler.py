"""调度器：支持 catchup 与基于计划的 realtime。"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fake_cdn.core.generator import (
    CDNLogGenerator,
    FluxPlanPoint,
    TimeWindowBuilder,
    get_output_dir,
    normalize_config,
    parse_datetime_in_timezone,
    parse_timezone,
)
from fake_cdn.core.pusher import LocalSaver, LogPusher


class RealtimeScheduler:
    """
    实时调度器。

    设计要点：
    1. 首次启动生成整窗计划并持久化到 output/traffic_plan.json
    2. 每次按“当前对齐时间点”查找计划中的流量值
    3. 推送成功后记录到 state.json，避免重复推送
    """

    def __init__(
        self,
        config: Dict,
        state_file: Optional[str] = None,
        plan_file: Optional[str] = None,
        output_dir: Optional[str] = None,
    ):
        self.config = normalize_config(config)
        self.output_dir = output_dir or get_output_dir(self.config)
        os.makedirs(self.output_dir, exist_ok=True)

        self.state_file = state_file or os.path.join(self.output_dir, "state.json")
        self.plan_file = plan_file or os.path.join(self.output_dir, "traffic_plan.json")

        self.time_builder = TimeWindowBuilder(self.config)
        self.timezone = parse_timezone(self.config["time"]["timezone"])
        self.generator = CDNLogGenerator(self.config)
        self.pusher = LogPusher(self.config)
        self.plan_id = self._build_plan_id()
        self.state = self._load_state()
        self.plan_points = self._load_or_create_plan()
        self.plan_index = {point.timestamp_ms: point for point in self.plan_points}

    def _build_plan_id(self) -> str:
        runtime = self.config.get("_runtime", {})
        return "{tenant}_{version}_{checksum}_{start}_{end}_{seed}".format(
            tenant=self.config["dimensions"]["tenant_id"],
            version=runtime.get("config_version_id", "legacy"),
            checksum=str(runtime.get("config_checksum", "legacy"))[:12],
            start=self.config["time"]["start_datetime"].replace(" ", "T"),
            end=self.config["time"]["end_datetime"].replace(" ", "T"),
            seed=self.config["target"]["random_seed"],
        )

    def _load_state(self) -> Dict:
        if os.path.exists(self.state_file):
            with open(self.state_file, "r", encoding="utf-8") as file:
                state = json.load(file)
            if state.get("plan_id") == self.plan_id:
                pushed = state.get("pushed_timestamps", [])
                print(f"[状态] 已加载 state.json，已推送 {len(pushed)} 个时间点")
                return state

        return {
            "plan_id": self.plan_id,
            "pushed_timestamps": [],
        }

    def _save_state(self) -> None:
        with open(self.state_file, "w", encoding="utf-8") as file:
            json.dump(self.state, file, ensure_ascii=False, indent=2)

    def _load_or_create_plan(self) -> List[FluxPlanPoint]:
        if os.path.exists(self.plan_file):
            with open(self.plan_file, "r", encoding="utf-8") as file:
                payload = json.load(file)
            if payload.get("plan_id") == self.plan_id:
                print(f"[计划] 复用已有流量计划: {self.plan_file}")
                return [
                    FluxPlanPoint(
                        timestamp_ms=int(item["timestamp_ms"]), flux_bytes=int(item["flux_bytes"])
                    )
                    for item in payload.get("points", [])
                ]

        print("[计划] 未找到可复用计划，开始生成...")
        points = self.generator.generate_window_plan()
        payload = {
            "plan_id": self.plan_id,
            "time": self.config["time"],
            "points": [
                {"timestamp_ms": point.timestamp_ms, "flux_bytes": point.flux_bytes}
                for point in points
            ],
        }
        with open(self.plan_file, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
        print(f"[计划] 已保存到: {self.plan_file}")
        return points

    def _align_to_interval(self, dt: datetime) -> datetime:
        interval = int(self.config["time"]["interval_seconds"])
        timestamp = int(dt.timestamp())
        aligned = (timestamp // interval) * interval
        return datetime.fromtimestamp(aligned, tz=self.timezone)

    def _wait_until_next_interval(self) -> None:
        interval = int(self.config["time"]["interval_seconds"])
        now = datetime.now(self.timezone)
        aligned = self._align_to_interval(now)
        next_time = aligned + timedelta(seconds=interval)
        wait_seconds = (next_time - now).total_seconds()
        if wait_seconds > 0:
            print(
                f"[等待] 下次执行时间: {next_time.strftime('%Y-%m-%d %H:%M:%S %Z')} (等待 {wait_seconds:.1f} 秒)"
            )
            time.sleep(wait_seconds)

    def run_once(self, dry_run: bool = False) -> bool:
        current_time = self._align_to_interval(datetime.now(self.timezone))
        timestamp_ms = int(current_time.timestamp() * 1000)

        if timestamp_ms in self.state["pushed_timestamps"]:
            print(f"[跳过] {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')} 已推送")
            return True

        plan_point = self.plan_index.get(timestamp_ms)
        if plan_point is None:
            print(
                f"[完成] 当前时间点 {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')} 不在配置窗口内"
            )
            return False

        print(f"[执行] 推送 {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')} 的日志")
        logs = self.generator.generate_logs_for_slot(plan_point)
        result = self.pusher.push_batch(logs, dry_run)

        if result["success"] == len(logs):
            if self.config["mode"].get("save_local", True):
                LocalSaver.save_logs(
                    logs,
                    self.output_dir,
                    db_path=self.config.get("_runtime", {}).get("log_db_path"),
                )
            self.state["pushed_timestamps"].append(timestamp_ms)
            self.state["pushed_timestamps"] = sorted(set(self.state["pushed_timestamps"]))
            self._save_state()
            print(
                f"[成功] 推送 {result['success']} 条日志，时间点流量 {plan_point.flux_bytes:,} Byte"
            )
            return True

        print(f"[失败] 推送失败，成功 {result['success']} 条，失败 {result['failed']} 条")
        return False

    def run_forever(self, dry_run: bool = False, end_datetime: Optional[datetime] = None) -> None:
        print("[启动] 实时调度器启动")
        print(
            f"[配置] 窗口: {self.config['time']['start_datetime']} ~ {self.config['time']['end_datetime']}"
        )
        print(f"[配置] 粒度: {self.config['time']['interval_seconds']} 秒")
        if end_datetime:
            print(f"[配置] 结束时间: {end_datetime.strftime('%Y-%m-%d %H:%M:%S %Z')}")

        try:
            while True:
                if end_datetime and datetime.now(self.timezone) >= end_datetime:
                    print("[完成] 已到达结束时间，停止推送")
                    self._save_state()
                    break

                self._wait_until_next_interval()

                if end_datetime and datetime.now(self.timezone) >= end_datetime:
                    print("[完成] 已到达结束时间，停止推送")
                    self._save_state()
                    break

                success = self.run_once(dry_run)
                if not success:
                    # 如果当前时间点不在窗口内，说明计划已结束或尚未开始，此时退出即可。
                    self._save_state()
                    break
        except KeyboardInterrupt:
            print("\n[停止] 收到中断信号，状态已保存")
            self._save_state()


class CatchupScheduler:
    """补推指定时间窗口的数据。"""

    def __init__(
        self, config: Dict, start_datetime: Optional[str] = None, end_datetime: Optional[str] = None
    ):
        self.base_config = normalize_config(config)
        self.timezone = parse_timezone(self.base_config["time"]["timezone"])
        self.output_dir = get_output_dir(self.base_config)
        os.makedirs(self.output_dir, exist_ok=True)

        start_dt = (
            parse_datetime_in_timezone(start_datetime, self.timezone)
            if start_datetime
            else parse_datetime_in_timezone(
                self.base_config["time"]["start_datetime"], self.timezone
            )
        )
        end_dt = (
            parse_datetime_in_timezone(end_datetime, self.timezone)
            if end_datetime
            else parse_datetime_in_timezone(self.base_config["time"]["end_datetime"], self.timezone)
        )

        self.config = json.loads(json.dumps(self.base_config))
        self.config["time"]["start_datetime"] = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        self.config["time"]["end_datetime"] = end_dt.strftime("%Y-%m-%d %H:%M:%S")
        self.config = normalize_config(self.config)
        self.generator = CDNLogGenerator(self.config)
        self.pusher = LogPusher(self.config)

    def run(self, dry_run: bool = False) -> Dict:
        print("[补推] 开始补推数据")
        print(
            f"[时间] {self.config['time']['start_datetime']} ~ {self.config['time']['end_datetime']}"
        )

        logs, stats = self.generator.generate_window_logs()
        if self.config["mode"].get("save_local", True):
            LocalSaver.save_logs(
                logs,
                self.output_dir,
                db_path=self.config.get("_runtime", {}).get("log_db_path"),
            )
            LocalSaver.save_stats(stats, self.output_dir, "stats.json")
            LocalSaver.save_flux_curve(
                self.generator.generate_window_plan(), self.output_dir, "flux_curve.csv"
            )

        self.pusher.push_all(logs, dry_run, show_progress=True)
        return stats
