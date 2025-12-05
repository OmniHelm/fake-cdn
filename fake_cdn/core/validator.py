"""
95计费验证器
验证生成的日志是否符合95计费目标
"""

import json
from typing import List, Dict
from collections import defaultdict


class Percentile95Validator:
    """95计费验证器"""

    @staticmethod
    def calculate_p95(values: List[float]) -> Dict:
        """
        计算95分位及相关统计

        这就是95计费的核心算法:
        1. 排序所有带宽值
        2. 去掉前5%的峰值
        3. 取第95%位置的值
        """

        if not values:
            return {"error": "空数据"}

        sorted_values = sorted(values)
        total = len(sorted_values)

        # 计算各分位点
        p50_index = total // 2
        p95_index = int(total * 0.95)
        p99_index = int(total * 0.99)

        stats = {
            "total_points": total,
            "min": sorted_values[0],
            "max": sorted_values[-1],
            "avg": sum(sorted_values) / total,
            "p50": sorted_values[p50_index],
            "p95": sorted_values[p95_index],
            "p99": sorted_values[p99_index],

            # 95计费的关键: 有5%的时间可以跑高带宽
            "top_5_percent": {
                "count": total - p95_index,
                "min": sorted_values[p95_index],
                "max": sorted_values[-1],
                "avg": sum(sorted_values[p95_index:]) / (total - p95_index)
            }
        }

        return stats

    @staticmethod
    def validate_logs(logs: List[Dict], target_gbps: float) -> Dict:
        """
        从日志中提取带宽并验证

        日志格式: {"bw": 15360, ...}  # Mbps
        验证平均带宽是否达到目标
        """

        # 提取带宽值 (Mbps -> Gbps)
        bandwidths = [log["bw"] / 1024 for log in logs]

        # 按维度分组统计(可选)
        by_region = defaultdict(list)
        by_domain = defaultdict(list)

        for log in logs:
            bw_gbps = log["bw"] / 1024
            by_region[log["region"]].append(bw_gbps)
            by_domain[log["domain"]].append(bw_gbps)

        # 计算统计
        overall_stats = Percentile95Validator.calculate_p95(bandwidths)

        # 验证平均带宽是否达标
        actual_avg = overall_stats["avg"]
        deviation = abs(actual_avg - target_gbps) / target_gbps * 100

        validation = {
            "target_gbps": target_gbps,
            "actual_avg_gbps": actual_avg,
            "actual_p95_gbps": overall_stats["p95"],
            "deviation_percent": deviation,
            "passed": deviation < 5.0,  # 允许5%误差
        }

        # 区域统计
        region_stats = {
            region: Percentile95Validator.calculate_p95(bws)
            for region, bws in by_region.items()
        }

        # 域名统计
        domain_stats = {
            domain: Percentile95Validator.calculate_p95(bws)
            for domain, bws in by_domain.items()
        }

        return {
            "validation": validation,
            "overall": overall_stats,
            "by_region": region_stats,
            "by_domain": domain_stats,
        }

    @staticmethod
    def print_report(result: Dict):
        """打印验证报告"""

        val = result["validation"]

        print("\n" + "=" * 60)
        print("带宽验证报告")
        print("=" * 60)

        # 验证结果
        status = "✓ 通过" if val["passed"] else "✗ 未通过"

        print(f"\n【验证结果】 {status}")
        print(f"  目标平均带宽: {val['target_gbps']:.2f} Gbps")
        print(f"  实际平均带宽: {val['actual_avg_gbps']:.2f} Gbps")
        print(f"  偏差: {val['deviation_percent']:.2f}%")

        # 计算每天流量
        daily_tb = val['actual_avg_gbps'] * 10.54 / 1024
        print(f"  每天流量: {daily_tb:.2f} TB")

        print(f"  95分位: {val['actual_p95_gbps']:.2f} Gbps (参考)")

        # 整体统计
        overall = result["overall"]
        print(f"\n【整体统计】")
        print(f"  数据点数: {overall['total_points']}")
        print(f"  最小带宽: {overall['min']:.2f} Gbps")
        print(f"  最大带宽: {overall['max']:.2f} Gbps")
        print(f"  平均带宽: {overall['avg']:.2f} Gbps")
        print(f"  中位带宽(P50): {overall['p50']:.2f} Gbps")
        print(f"  95分位(P95): {overall['p95']:.2f} Gbps")
        print(f"  99分位(P99): {overall['p99']:.2f} Gbps")

        # Top 5%统计(这是95计费可以"免费"跑高的部分)
        top5 = overall["top_5_percent"]
        print(f"\n【Top 5%峰值】(这些不计费!)")
        print(f"  数量: {top5['count']} 个点")
        print(f"  范围: {top5['min']:.2f} - {top5['max']:.2f} Gbps")
        print(f"  平均: {top5['avg']:.2f} Gbps")

        # 区域统计
        print(f"\n【按地区统计】")
        for region, stats in result["by_region"].items():
            print(f"  {region:20s} P95: {stats['p95']:8.2f} Gbps  "
                  f"(Avg: {stats['avg']:6.2f}, Max: {stats['max']:6.2f})")

        # 域名统计
        print(f"\n【按域名统计】")
        for domain, stats in result["by_domain"].items():
            print(f"  {domain:30s} P95: {stats['p95']:8.2f} Gbps  "
                  f"(Avg: {stats['avg']:6.2f}, Max: {stats['max']:6.2f})")

        print("=" * 60 + "\n")


class BillingCalculator:
    """计费计算器 - 模拟真实CDN计费"""

    @staticmethod
    def calculate_95_billing(bandwidth_curve: List[float], unit_price: float = 100.0) -> Dict:
        """
        计算95计费金额

        bandwidth_curve: 带宽曲线 (Gbps)
        unit_price: 单价 (元/Gbps/月)

        返回: 计费详情
        """

        stats = Percentile95Validator.calculate_p95(bandwidth_curve)
        p95_gbps = stats["p95"]

        # 计算费用
        monthly_cost = p95_gbps * unit_price

        # 计算实际使用的总流量
        interval_seconds = 300  # 假设5分钟粒度
        total_flux_gb = sum(bandwidth_curve) * interval_seconds / 8

        # 如果按流量计费,费用是多少? (对比)
        flux_unit_price = 0.8  # 假设0.8元/GB
        flux_cost = total_flux_gb * flux_unit_price

        # 节省了多少?
        saving = flux_cost - monthly_cost
        saving_percent = (saving / flux_cost * 100) if flux_cost > 0 else 0

        return {
            "p95_bandwidth_gbps": p95_gbps,
            "unit_price": unit_price,
            "monthly_cost": monthly_cost,
            "total_flux_gb": total_flux_gb,
            "flux_cost_comparison": flux_cost,
            "saving": saving,
            "saving_percent": saving_percent,
            "stats": stats
        }

    @staticmethod
    def print_billing_report(result: Dict):
        """打印计费报告"""

        print("\n" + "=" * 60)
        print("CDN计费报告 (95计费)")
        print("=" * 60)

        print(f"\n【计费带宽】")
        print(f"  95分位带宽: {result['p95_bandwidth_gbps']:.2f} Gbps")
        print(f"  单价: {result['unit_price']:.2f} 元/Gbps/月")

        print(f"\n【费用】")
        print(f"  月费用(95计费): {result['monthly_cost']:,.2f} 元")

        print(f"\n【流量统计】")
        print(f"  总流量: {result['total_flux_gb']:,.2f} GB ({result['total_flux_gb']/1024:.2f} TB)")

        print(f"\n【对比: 如果按流量计费】")
        print(f"  流量费用: {result['flux_cost_comparison']:,.2f} 元")
        print(f"  95计费节省: {result['saving']:,.2f} 元 ({result['saving_percent']:.1f}%)")

        if result['saving'] > 0:
            print(f"  ✓ 95计费更划算!")
        else:
            print(f"  ✗ 流量计费更划算!")

        print("=" * 60 + "\n")


def load_logs_from_file(filepath: str) -> List[Dict]:
    """从JSONL文件加载日志"""
    logs = []
    with open(filepath, 'r') as f:
        for line in f:
            logs.append(json.loads(line))
    return logs


def validate_from_file(filepath: str, target_gbps: float):
    """从文件验证"""
    print(f"[加载] 正在从 {filepath} 加载日志...")
    logs = load_logs_from_file(filepath)
    print(f"[加载] 共加载 {len(logs)} 条日志")

    print(f"[验证] 开始验证...")
    result = Percentile95Validator.validate_logs(logs, target_gbps)

    Percentile95Validator.print_report(result)

    return result
