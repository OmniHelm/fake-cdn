from __future__ import annotations

"""流量窗口校验器。"""

import json
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Sequence

from fake_cdn.core.generator import (
    TrafficTargetParser,
    decimal_pb,
    decimal_tb,
    normalize_config,
    parse_timezone,
)
from fake_cdn.core.storage import CDNLogStorage


class Percentile95Validator:
    """保留通用分位统计能力，用于等效带宽分析。"""

    @staticmethod
    def calculate_p95(values: Sequence[float]) -> Dict:
        if not values:
            return {
                "total_points": 0,
                "min": 0.0,
                "max": 0.0,
                "avg": 0.0,
                "p50": 0.0,
                "p95": 0.0,
                "p99": 0.0,
                "top_5_percent": {"count": 0, "min": 0.0, "max": 0.0, "avg": 0.0},
            }

        sorted_values = sorted(values)
        total = len(sorted_values)
        p50_index = min(total - 1, max(0, total // 2))
        p95_index = min(total - 1, max(0, int((total - 1) * 0.95)))
        p99_index = min(total - 1, max(0, int((total - 1) * 0.99)))
        tail = sorted_values[p95_index:]

        return {
            "total_points": total,
            "min": sorted_values[0],
            "max": sorted_values[-1],
            "avg": sum(sorted_values) / total,
            "p50": sorted_values[p50_index],
            "p95": sorted_values[p95_index],
            "p99": sorted_values[p99_index],
            "top_5_percent": {
                "count": len(tail),
                "min": tail[0],
                "max": tail[-1],
                "avg": sum(tail) / len(tail),
            },
        }


class FluxWindowValidator:
    """校验整窗总流量、时间分布、字段守恒和域名占比。"""

    @staticmethod
    def validate_logs(logs: Sequence[Dict], config: Dict) -> Dict:
        config = normalize_config(config)
        interval_seconds = int(config["time"]["interval_seconds"])
        timezone = parse_timezone(config["time"]["timezone"])
        target_total_flux_bytes = TrafficTargetParser.parse_total_flux_bytes(config)

        slot_flux = defaultdict(int)
        slot_bw = defaultdict(int)
        per_day_flux = defaultdict(int)
        domain_flux = defaultdict(int)
        integrity_issues = defaultdict(int)

        total_flux_bytes = 0
        for log in logs:
            start_time = int(log["start_time"])
            flux = int(log.get("flux", 0))
            bw = int(log.get("bw", 0))
            total_flux_bytes += flux
            slot_flux[start_time] += flux
            slot_bw[start_time] += bw
            domain_flux[log["domain"]] += flux

            dt = datetime.fromtimestamp(start_time / 1000, tz=timezone)
            per_day_flux[dt.date().isoformat()] += flux

            if bw != flux * 8:
                integrity_issues["bw_flux_mismatch"] += 1
            if int(log.get("hit_flux", 0)) + int(log.get("bs_flux", 0)) != flux:
                integrity_issues["flux_split_mismatch"] += 1
            if int(log.get("hit_num", 0)) + int(log.get("bs_num", 0)) != int(log.get("req_num", 0)):
                integrity_issues["request_split_mismatch"] += 1
            if (
                int(log.get("http_code_2xx", 0))
                + int(log.get("http_code_3xx", 0))
                + int(log.get("http_code_4xx", 0))
                + int(log.get("http_code_5xx", 0))
                != int(log.get("req_num", 0))
            ):
                integrity_issues["client_http_code_mismatch"] += 1
            if (
                int(log.get("bs_http_code_2xx", 0))
                + int(log.get("bs_http_code_3xx", 0))
                + int(log.get("bs_http_code_4xx", 0))
                + int(log.get("bs_http_code_5xx", 0))
                != int(log.get("bs_num", 0))
            ):
                integrity_issues["origin_http_code_mismatch"] += 1
            if int(log.get("bs_bw", 0)) != int(log.get("bs_flux", 0)) * 8:
                integrity_issues["origin_bw_flux_mismatch"] += 1

        actual_total_flux_bytes = total_flux_bytes
        deviation_percent = (
            abs(actual_total_flux_bytes - target_total_flux_bytes) / target_total_flux_bytes * 100
            if target_total_flux_bytes
            else 0.0
        )

        slot_gbps = [flux * 8 / interval_seconds / 1_000_000_000 for _, flux in sorted(slot_flux.items())]
        slot_stats = Percentile95Validator.calculate_p95(slot_gbps)

        weekday_values = []
        weekend_values = []
        for day_string, total_flux in per_day_flux.items():
            dt = datetime.strptime(day_string, "%Y-%m-%d")
            if dt.weekday() >= 5:
                weekend_values.append(total_flux)
            else:
                weekday_values.append(total_flux)

        domain_weight_sum = sum(max(0.0, float(item["weight"])) for item in config["dimensions"]["domains"])
        domain_expectation = {
            item["name"]: float(item["weight"]) / domain_weight_sum
            for item in config["dimensions"]["domains"]
        }

        domain_stats = []
        for domain, flux in sorted(domain_flux.items(), key=lambda item: item[1], reverse=True):
            actual_share = flux / actual_total_flux_bytes if actual_total_flux_bytes else 0.0
            expected_share = domain_expectation.get(domain, 0.0)
            domain_stats.append(
                {
                    "domain": domain,
                    "flux_pb": decimal_pb(flux),
                    "actual_share": actual_share,
                    "expected_share": expected_share,
                    "share_gap_percent": abs(actual_share - expected_share) * 100,
                }
            )

        result = {
            "validation": {
                "target_total_flux_bytes": target_total_flux_bytes,
                "target_total_flux_pb": decimal_pb(target_total_flux_bytes),
                "actual_total_flux_bytes": actual_total_flux_bytes,
                "actual_total_flux_pb": decimal_pb(actual_total_flux_bytes),
                "deviation_percent": deviation_percent,
                "passed": deviation_percent <= 0.1 and sum(integrity_issues.values()) == 0,
                "integrity_issue_count": sum(integrity_issues.values()),
            },
            "overall": {
                "total_records": len(logs),
                "total_slots": len(slot_flux),
                "equivalent_avg_gbps": slot_stats["avg"],
                "p50_gbps": slot_stats["p50"],
                "p95_gbps": slot_stats["p95"],
                "p99_gbps": slot_stats["p99"],
                "min_gbps": slot_stats["min"],
                "max_gbps": slot_stats["max"],
                "peak_to_valley_ratio": slot_stats["max"] / max(slot_stats["min"], 0.000001),
                "weekday_avg_tb": decimal_tb(int(sum(weekday_values) / len(weekday_values))) if weekday_values else 0.0,
                "weekend_avg_tb": decimal_tb(int(sum(weekend_values) / len(weekend_values))) if weekend_values else 0.0,
                "weekday_gt_weekend": (sum(weekday_values) / len(weekday_values)) > (sum(weekend_values) / len(weekend_values))
                if weekday_values and weekend_values
                else True,
            },
            "integrity": dict(integrity_issues),
            "by_domain": domain_stats,
        }
        return result

    @staticmethod
    def print_report(result: Dict) -> None:
        validation = result["validation"]
        overall = result["overall"]
        integrity = result["integrity"]
        top_domains = result["by_domain"][:10]

        print("\n" + "=" * 72)
        print("流量窗口校验报告")
        print("=" * 72)
        print(f"\n【校验结果】 {'✓ 通过' if validation['passed'] else '✗ 未通过'}")
        print(f"  目标总流量: {validation['target_total_flux_pb']:.4f} PB")
        print(f"  实际总流量: {validation['actual_total_flux_pb']:.4f} PB")
        print(f"  偏差: {validation['deviation_percent']:.4f}%")
        print(f"  字段一致性问题: {validation['integrity_issue_count']}")

        print("\n【整体统计】")
        print(f"  日志条数: {overall['total_records']}")
        print(f"  时间点数: {overall['total_slots']}")
        print(f"  等效平均带宽: {overall['equivalent_avg_gbps']:.2f} Gbps")
        print(f"  P50/P95/P99: {overall['p50_gbps']:.2f} / {overall['p95_gbps']:.2f} / {overall['p99_gbps']:.2f} Gbps")
        print(f"  峰谷范围: {overall['min_gbps']:.2f} ~ {overall['max_gbps']:.2f} Gbps")
        print(f"  峰谷比: {overall['peak_to_valley_ratio']:.2f}")
        print(f"  工作日平均: {overall['weekday_avg_tb']:.2f} TB/天")
        print(f"  周末平均: {overall['weekend_avg_tb']:.2f} TB/天")
        print(f"  工作日高于周末: {'是' if overall['weekday_gt_weekend'] else '否'}")

        if integrity:
            print("\n【字段一致性问题】")
            for key, count in sorted(integrity.items()):
                print(f"  {key}: {count}")

        print("\n【域名流量占比 Top 10】")
        for item in top_domains:
            print(
                "  {domain:30s} 实际 {actual:.2%}  期望 {expected:.2%}  差值 {gap:.2f}%".format(
                    domain=item["domain"][:30],
                    actual=item["actual_share"],
                    expected=item["expected_share"],
                    gap=item["share_gap_percent"],
                )
            )

        print("=" * 72 + "\n")


class BillingCalculator:
    """保留一个简单带宽参考报告，便于观察等效带宽统计。"""

    @staticmethod
    def calculate_95_billing(slot_gbps: Sequence[float], unit_price: float = 100.0) -> Dict:
        stats = Percentile95Validator.calculate_p95(slot_gbps)
        p95_gbps = stats["p95"]
        monthly_cost = p95_gbps * unit_price
        return {
            "p95_bandwidth_gbps": p95_gbps,
            "unit_price": unit_price,
            "monthly_cost": monthly_cost,
            "stats": stats,
        }

    @staticmethod
    def print_billing_report(result: Dict) -> None:
        print("\n" + "=" * 72)
        print("等效带宽参考报告")
        print("=" * 72)
        print(f"  P95 等效带宽: {result['p95_bandwidth_gbps']:.2f} Gbps")
        print(f"  单价: {result['unit_price']:.2f} 元/Gbps/月")
        print(f"  参考月费用: {result['monthly_cost']:,.2f} 元")
        print("=" * 72 + "\n")


def load_logs_from_file(filepath: str) -> List[Dict]:
    if filepath.endswith(".db"):
        storage = CDNLogStorage(filepath)
        return storage.query_logs()

    logs: List[Dict] = []
    with open(filepath, "r", encoding="utf-8") as file:
        for line in file:
            if line.strip():
                logs.append(json.loads(line))
    return logs


def validate_from_file(filepath: str, config: Dict) -> Dict:
    print(f"[加载] 正在从 {filepath} 加载日志...")
    logs = load_logs_from_file(filepath)
    print(f"[加载] 共加载 {len(logs)} 条日志")
    result = FluxWindowValidator.validate_logs(logs, config)
    FluxWindowValidator.print_report(result)
    return result
