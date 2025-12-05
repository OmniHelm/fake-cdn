"""
实时推送调度器
用于按时间间隔实时生成并推送日志
"""

import json
import os
import time
from datetime import datetime, timedelta
from typing import Dict

from fake_cdn.core.generator import CDNLogGenerator, BandwidthCurveGenerator
from fake_cdn.core.pusher import LogPusher


class RealtimeScheduler:
    """
    实时调度器

    模式: 每5分钟执行一次,生成当前时间点的日志并推送

    状态管理: 记录已推送的时间点,支持断点续传
    """

    def __init__(self, config: dict, state_file: str = "./state.json"):
        self.config = config
        self.state_file = state_file

        self.generator = CDNLogGenerator(config)
        self.pusher = LogPusher(config)

        # 加载状态
        self.state = self._load_state()

        # 预生成带宽曲线(避免每次重新生成)
        self.bandwidth_curve = None

    def _load_state(self) -> Dict:
        """加载调度器状态"""
        if os.path.exists(self.state_file):
            with open(self.state_file, 'r') as f:
                state = json.load(f)
                print(f"[状态] 加载状态: 已推送 {len(state.get('pushed_timestamps', []))} 个时间点")
                return state
        else:
            return {
                "pushed_timestamps": [],
                "start_date": self.config["time"]["start_date"],
                "current_index": 0
            }

    def _save_state(self):
        """保存调度器状态"""
        with open(self.state_file, 'w') as f:
            json.dump(self.state, f, indent=2)

    def _align_to_interval(self, dt: datetime) -> datetime:
        """
        对齐到时间间隔整点

        例如: interval=300秒(5分钟)
        14:03:27 -> 14:00:00
        14:07:56 -> 14:05:00
        """
        interval = self.config["time"]["interval_seconds"]
        timestamp = int(dt.timestamp())
        aligned_timestamp = (timestamp // interval) * interval
        return datetime.fromtimestamp(aligned_timestamp)

    def _wait_until_next_interval(self):
        """等待到下一个时间间隔整点"""
        interval = self.config["time"]["interval_seconds"]
        now = datetime.now()
        aligned = self._align_to_interval(now)
        next_time = aligned + timedelta(seconds=interval)

        wait_seconds = (next_time - now).total_seconds()

        if wait_seconds > 0:
            print(f"[等待] 下次执行时间: {next_time.strftime('%Y-%m-%d %H:%M:%S')} (等待 {wait_seconds:.1f} 秒)")
            time.sleep(wait_seconds)

    def run_once(self, dry_run: bool = False) -> bool:
        """
        执行一次推送任务

        返回: 是否成功
        """

        # 1. 确定当前时间点
        current_time = self._align_to_interval(datetime.now())
        timestamp_ms = int(current_time.timestamp() * 1000)

        # 检查是否已推送
        if timestamp_ms in self.state["pushed_timestamps"]:
            print(f"[跳过] 时间点 {current_time} 已推送过")
            return True

        print(f"[执行] 开始推送 {current_time.strftime('%Y-%m-%d %H:%M:%S')} 的日志")

        # 2. 生成带宽曲线(如果还没生成)
        if self.bandwidth_curve is None:
            print("[初始化] 预生成带宽曲线...")
            curve_gen = BandwidthCurveGenerator(
                self.config["target"]["bandwidth_gbps"],
                self.config
            )
            self.bandwidth_curve = curve_gen.generate(
                self.config["time"]["duration_days"],
                self.config["time"]["interval_seconds"]
            )

        # 3. 获取当前时间点的带宽值
        index = self.state["current_index"]
        if index >= len(self.bandwidth_curve):
            print("[完成] 已推送完所有时间点")
            return False

        bandwidth_gbps = self.bandwidth_curve[index]

        # 4. 生成指标
        metrics = self.generator.metrics_deriver.derive(
            bandwidth_gbps,
            self.config["time"]["interval_seconds"]
        )

        # 5. 注入异常
        metrics = self.generator.anomaly_injector.inject(metrics, timestamp_ms)

        # 6. 分配到多维度
        logs = self.generator.distributor.distribute(metrics, timestamp_ms)

        # 7. 推送
        result = self.pusher.push_batch(logs, dry_run)

        if result["success"] > 0:
            # 更新状态
            self.state["pushed_timestamps"].append(timestamp_ms)
            self.state["current_index"] = index + 1
            self._save_state()

            print(f"[成功] 推送 {result['success']} 条日志, 带宽: {bandwidth_gbps:.2f} Gbps")
            return True
        else:
            print(f"[失败] 推送失败: {result['errors']}")
            return False

    def run_forever(self, dry_run: bool = False):
        """
        持续运行,每个时间间隔执行一次

        适用场景: 长期运行,模拟真实CDN节点
        """

        print(f"[启动] 实时调度器启动")
        print(f"[配置] 时间间隔: {self.config['time']['interval_seconds']} 秒")
        print(f"[配置] 目标带宽: {self.config['target']['bandwidth_gbps']} Gbps")

        try:
            while True:
                # 等待到下一个整点
                self._wait_until_next_interval()

                # 执行推送
                success = self.run_once(dry_run)

                if not success:
                    print("[警告] 推送失败,1分钟后重试")
                    time.sleep(60)

        except KeyboardInterrupt:
            print("\n[停止] 收到中断信号,正在停止...")
            self._save_state()
            print("[停止] 状态已保存")


class CatchupScheduler:
    """
    补推调度器

    用于补推历史数据(例如从1月1日开始补推30天的数据)
    """

    def __init__(self, config: dict, start_date: str, end_date: str):
        self.config = config
        self.start_date = datetime.strptime(start_date, "%Y-%m-%d")
        self.end_date = datetime.strptime(end_date, "%Y-%m-%d")

        self.generator = CDNLogGenerator(config)
        self.pusher = LogPusher(config)

    def run(self, dry_run: bool = False, delay_ms: int = 100):
        """
        补推指定时间段的数据

        delay_ms: 每条日志之间的延迟(毫秒),避免打爆API
        """

        print(f"[补推] 开始补推数据")
        print(f"[时间] {self.start_date.date()} 到 {self.end_date.date()}")

        # 生成数据
        duration_days = (self.end_date - self.start_date).days
        self.config["time"]["start_date"] = self.start_date.strftime("%Y-%m-%d")
        self.config["time"]["duration_days"] = duration_days

        logs, stats = self.generator.generate_full_month()

        # 推送
        self.pusher.push_all(logs, dry_run, show_progress=True)

        return stats
