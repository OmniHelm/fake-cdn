"""
CDN日志生成器 - 核心算法
按Linus哲学: 先设计好数据结构,代码自然清晰
"""

import math
import random
from datetime import datetime, timedelta
from typing import List, Dict, Tuple


class BandwidthCurveGenerator:
    """带宽曲线生成器 - 生成指定平均带宽的流量曲线"""

    def __init__(self, target_gbps: float, config: dict):
        self.target_gbps = target_gbps
        self.config = config

    def generate(self, duration_days: int, interval_seconds: int) -> List[float]:
        """
        生成一个月的带宽曲线 (单位: Gbps)

        算法本质:
        1. 生成基础曲线(有日周期、周周期、噪声)
        2. 注入5%的突发流量
        3. 线性调整使平均带宽精确等于目标值
        """
        total_points = duration_days * 24 * 60 // (interval_seconds // 60)

        # 基准带宽设为目标值, 后续会根据模式调整
        base_bw = self.target_gbps

        curve = []
        for i in range(total_points):
            minute_of_month = i * (interval_seconds // 60)

            # 时间特征提取
            hour_of_day = (minute_of_month // 60) % 24
            day_of_week = (minute_of_month // 1440) % 7
            day_of_month = minute_of_month // 1440

            # 1. 日周期: 凌晨低谷(0.6x), 晚高峰(1.3x)
            # 使用正弦函数模拟,峰值在20:00左右
            # 调整: 减少波动幅度,使95分位接近20 Gbps
            daily_factor = 0.6 + 0.7 * (
                0.5 + 0.5 * math.sin((hour_of_day - 6) * math.pi / 12)
            )

            # 2. 周周期: 周末略低
            weekly_factor = 0.85 if day_of_week in [5, 6] else 1.0

            # 3. 月趋势: 月初月末略高(促销/结算)
            if day_of_month < 3 or day_of_month >= duration_days - 3:
                monthly_factor = 1.15
            else:
                monthly_factor = 1.0

            # 4. 随机噪声: ±8% (调整后)
            noise_factor = random.uniform(0.92, 1.08)

            # 5. 突发流量: 5%概率出现2-3倍峰值(这是95计费的关键)
            burst_prob = self.config["realism"]["burst_probability"]
            if random.random() < burst_prob:
                burst_factor = random.uniform(2.0, 3.0)
            else:
                burst_factor = 1.0

            bw = base_bw * daily_factor * weekly_factor * monthly_factor * noise_factor * burst_factor
            curve.append(max(0.1, bw))  # 最低保持0.1Gbps

        # 验证并调整到精确的95分位
        curve = self._adjust_to_target(curve)

        return curve

    def _adjust_to_target(self, curve: List[float]) -> List[float]:
        """线性调整曲线,使日95带宽精确等于目标值"""

        # 按天分组计算每日95值
        points_per_day = 24 * 60 // (self.config["time"]["interval_seconds"] // 60)

        def calc_daily_p95(day_curve):
            """计算单日95计费值"""
            sorted_bw = sorted(day_curve)
            n = len(sorted_bw)
            idx = int(n * 0.95) - 1
            if idx < 0:
                idx = 0
            if idx >= n:
                idx = n - 1
            return sorted_bw[idx]

        # 计算当前各日的95值
        daily_p95_list = []
        for day_start in range(0, len(curve), points_per_day):
            day_curve = curve[day_start:day_start + points_per_day]
            if len(day_curve) >= points_per_day * 0.5:  # 至少半天数据
                daily_p95_list.append(calc_daily_p95(day_curve))

        if daily_p95_list:
            avg_daily_p95 = sum(daily_p95_list) / len(daily_p95_list)
        else:
            avg_daily_p95 = max(curve)

        # 缩放使日95等于目标值
        if abs(avg_daily_p95 - self.target_gbps) / self.target_gbps > 0.02:
            scale = self.target_gbps / avg_daily_p95
            curve = [bw * scale for bw in curve]
            new_avg = sum(curve) / len(curve)
            print(f"[调整] 日95从 {avg_daily_p95:.2f} Gbps 调整到 {self.target_gbps:.2f} Gbps (缩放 {scale:.3f}x)")
            print(f"[信息] 调整后平均带宽: {new_avg:.2f} Gbps")
        else:
            print(f"[信息] 日95: {avg_daily_p95:.2f} Gbps (目标: {self.target_gbps:.2f} Gbps)")

        return curve


class MetricsDerivator:
    """指标推导器 - 从带宽推导所有CDN指标"""

    def __init__(self, config: dict):
        self.config = config

    def derive(self, bandwidth_gbps: float, interval_seconds: int) -> Dict:
        """
        从带宽值推导完整的CDN指标

        核心逻辑:
        1. 流量 = 带宽 × 时间
        2. 请求数 = 流量 / 平均对象大小
        3. 缓存命中率决定回源比例
        4. 状态码按真实分布生成
        """

        # 1. 总流量 (bytes)
        flux_bytes = int(bandwidth_gbps * 1024 * 1024 * 1024 * interval_seconds / 8)

        # 2. 缓存命中率 (85-95%)
        cache_hit_rate = random.uniform(*self.config["realism"]["cache_hit_rate"])

        # 3. 回源流量
        bs_flux_bytes = int(flux_bytes * (1 - cache_hit_rate))
        hit_flux_bytes = flux_bytes - bs_flux_bytes

        # 4. 请求数推算 (平均对象大小 200KB-2MB)
        avg_obj_size = random.uniform(
            self.config["realism"]["avg_object_size_kb"][0] * 1024,
            self.config["realism"]["avg_object_size_kb"][1] * 1024
        )
        req_num = max(1, int(flux_bytes / avg_obj_size))

        # 5. 命中和回源请求数
        hit_num = int(req_num * cache_hit_rate)
        bs_num = req_num - hit_num

        # 6. 回源失败 (<1%)
        bs_fail_rate = random.uniform(*self.config["realism"]["origin_fail_rate"])
        bs_fail_num = max(0, int(bs_num * bs_fail_rate))

        # 7. HTTP状态码分布 (客户端侧)
        # 真实CDN: 2xx(75-90%), 4xx(5-15%), 3xx(2-8%), 5xx(剩余)
        http_2xx = int(req_num * random.uniform(0.75, 0.90))
        http_4xx = int(req_num * random.uniform(0.05, 0.15))
        http_3xx = int(req_num * random.uniform(0.02, 0.08))
        http_5xx = max(0, req_num - http_2xx - http_4xx - http_3xx)

        # 8. 回源状态码分布 (成功率更高)
        if bs_num > 0:
            bs_http_2xx = int(bs_num * random.uniform(0.85, 0.95))
            bs_http_4xx = int(bs_num * random.uniform(0.02, 0.08))
            bs_http_3xx = int(bs_num * random.uniform(0.01, 0.05))
            bs_http_5xx = max(0, bs_num - bs_http_2xx - bs_http_4xx - bs_http_3xx)
        else:
            bs_http_2xx = bs_http_4xx = bs_http_3xx = bs_http_5xx = 0

        # 9. 带宽转换 (Gbps -> Mbps)
        bw_mbps = int(bandwidth_gbps * 1024)
        bs_bw_mbps = int(bs_flux_bytes * 8 / interval_seconds / 1024 / 1024)

        return {
            "bw": bw_mbps,
            "flux": flux_bytes,
            "bs_bw": bs_bw_mbps,
            "bs_flux": bs_flux_bytes,
            "req_num": req_num,
            "hit_num": hit_num,
            "bs_num": bs_num,
            "bs_fail_num": bs_fail_num,
            "hit_flux": hit_flux_bytes,
            "http_code_2xx": http_2xx,
            "http_code_3xx": http_3xx,
            "http_code_4xx": http_4xx,
            "http_code_5xx": http_5xx,
            "bs_http_code_2xx": bs_http_2xx,
            "bs_http_code_3xx": bs_http_3xx,
            "bs_http_code_4xx": bs_http_4xx,
            "bs_http_code_5xx": bs_http_5xx,
        }


class AnomalyInjector:
    """异常注入器 - 让数据更真实"""

    def __init__(self, config: dict):
        self.config = config

    def inject(self, metrics: Dict, timestamp_ms: int) -> Dict:
        """
        注入真实CDN会出现的异常模式

        常见异常:
        1. 凌晨运维窗口: 5xx增加
        2. 源站故障: 回源失败率飙升
        3. 缓存清理/失效: 命中率骤降
        4. DDoS攻击: 4xx激增
        """

        dt = datetime.fromtimestamp(timestamp_ms / 1000)
        hour = dt.hour

        anomaly_prob = self.config["realism"]["anomaly_probability"]

        # 1. 凌晨运维窗口 (2-4点, 5%概率)
        if hour in [2, 3, 4] and random.random() < 0.05:
            spike_5xx = int(metrics["req_num"] * random.uniform(0.05, 0.15))
            metrics["http_code_5xx"] = spike_5xx
            metrics["http_code_2xx"] = max(0, metrics["http_code_2xx"] - spike_5xx)

        # 2. 源站故障 (0.1%概率, 很罕见但影响大)
        if random.random() < anomaly_prob:
            fail_rate = random.uniform(0.3, 0.8)
            metrics["bs_fail_num"] = int(metrics["bs_num"] * fail_rate)
            metrics["bs_http_code_5xx"] = metrics["bs_fail_num"]
            metrics["bs_http_code_2xx"] = max(0, metrics["bs_num"] - metrics["bs_fail_num"])

        # 3. 缓存清理 (1%概率, 命中率降到50-70%)
        if random.random() < 0.01:
            new_hit_rate = random.uniform(0.5, 0.7)
            metrics["hit_num"] = int(metrics["req_num"] * new_hit_rate)
            metrics["bs_num"] = metrics["req_num"] - metrics["hit_num"]

            # 重新计算流量分布
            metrics["hit_flux"] = int(metrics["flux"] * new_hit_rate)
            metrics["bs_flux"] = metrics["flux"] - metrics["hit_flux"]

        # 4. DDoS/爬虫 (0.5%概率, 4xx激增)
        if random.random() < 0.005:
            spike_4xx = int(metrics["req_num"] * random.uniform(0.2, 0.4))
            metrics["http_code_4xx"] = spike_4xx
            metrics["http_code_2xx"] = max(0, metrics["http_code_2xx"] - spike_4xx)

        return metrics


class MultiDimensionDistributor:
    """多维度分配器 - 处理多域名、多地区"""

    def __init__(self, config: dict):
        self.config = config
        self.domains = config["dimensions"]["domains"]
        self.regions = config["dimensions"]["regions"]

    def distribute(self, global_metrics: Dict, timestamp_ms: int) -> List[Dict]:
        """
        将全局指标按维度分配

        策略:
        1. 按地区权重分配
        2. 每个地区随机选择1-2个域名
        3. 保证总和等于全局指标
        """

        results = []

        for region_info in self.regions:
            weight = region_info["weight"]

            # 按权重分配指标 (带±10%随机波动)
            actual_weight = weight * random.uniform(0.9, 1.1)

            region_metrics = {
                k: int(v * actual_weight) if isinstance(v, int) else v
                for k, v in global_metrics.items()
            }

            # 随机选择域名
            domain = random.choice(self.domains)

            log_entry = {
                "tenantId": self.config["dimensions"]["tenant_id"],
                "start_time": timestamp_ms,
                "country": region_info["country"],
                "region": region_info["region"],
                "domain": domain,
                "interval": self.config["time"]["interval_seconds"],
                **region_metrics
            }

            results.append(log_entry)

        return results


class CDNLogGenerator:
    """CDN日志生成器 - 主入口"""

    def __init__(self, config: dict):
        self.config = config

        target_bw = config["target"]["bandwidth_gbps"]

        self.curve_gen = BandwidthCurveGenerator(target_bw, config)
        self.metrics_deriver = MetricsDerivator(config)
        self.anomaly_injector = AnomalyInjector(config)
        self.distributor = MultiDimensionDistributor(config)

    def generate_full_month(self) -> Tuple[List[Dict], Dict]:
        """
        生成完整一个月的日志

        返回:
        - logs: 所有日志条目
        - stats: 统计信息(用于验证95计费)
        """

        duration_days = self.config["time"]["duration_days"]
        interval_seconds = self.config["time"]["interval_seconds"]
        start_date = datetime.strptime(self.config["time"]["start_date"], "%Y-%m-%d")

        # 1. 生成带宽曲线
        print(f"[生成] 正在生成 {duration_days} 天的带宽曲线...")
        bandwidth_curve = self.curve_gen.generate(duration_days, interval_seconds)

        # 2. 统计信息
        sorted_bw = sorted(bandwidth_curve)
        p95_index = int(len(sorted_bw) * 0.95)

        stats = {
            "total_points": len(bandwidth_curve),
            "p50_gbps": sorted_bw[len(sorted_bw) // 2],
            "p95_gbps": sorted_bw[p95_index],
            "p99_gbps": sorted_bw[int(len(sorted_bw) * 0.99)],
            "max_gbps": sorted_bw[-1],
            "min_gbps": sorted_bw[0],
            "avg_gbps": sum(bandwidth_curve) / len(bandwidth_curve),
            "total_flux_tb": sum(bandwidth_curve) * interval_seconds / 8 / 1024,
        }

        print(f"[统计] 95分位带宽: {stats['p95_gbps']:.2f} Gbps")
        print(f"[统计] 平均带宽: {stats['avg_gbps']:.2f} Gbps")
        print(f"[统计] 峰值带宽: {stats['max_gbps']:.2f} Gbps")
        print(f"[统计] 总流量: {stats['total_flux_tb']:.2f} TB")

        # 3. 逐点生成日志
        print(f"[生成] 正在生成 {len(bandwidth_curve)} 个时间点的日志...")

        all_logs = []
        for i, bw_gbps in enumerate(bandwidth_curve):
            timestamp = start_date + timedelta(seconds=i * interval_seconds)
            timestamp_ms = int(timestamp.timestamp() * 1000)

            # 推导指标
            metrics = self.metrics_deriver.derive(bw_gbps, interval_seconds)

            # 注入异常
            metrics = self.anomaly_injector.inject(metrics, timestamp_ms)

            # 分配到多维度
            logs = self.distributor.distribute(metrics, timestamp_ms)
            all_logs.extend(logs)

            if (i + 1) % 1000 == 0:
                print(f"  进度: {i+1}/{len(bandwidth_curve)} ({(i+1)/len(bandwidth_curve)*100:.1f}%)")

        print(f"[完成] 共生成 {len(all_logs)} 条日志记录")

        return all_logs, stats
