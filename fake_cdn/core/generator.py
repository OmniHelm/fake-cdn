"""
CDN 日志生成器 - 流量窗口驱动模型

核心思想：
1. 先根据总流量 + 时间窗口生成每个时间点的流量计划
2. 再按域名/地区拆分流量
3. 最后从 flux 反推 bw / req / hit / bs / http_code 等指标
"""

from __future__ import annotations

import calendar
import copy
import hashlib
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Dict, List, Optional, Sequence, Tuple

try:  # pragma: no cover - Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 fallback
    from backports.zoneinfo import ZoneInfo


WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULT_WEEKDAY_FACTORS = {
    "mon": 1.08,
    "tue": 1.10,
    "wed": 1.12,
    "thu": 1.08,
    "fri": 1.02,
    "sat": 0.82,
    "sun": 0.78,
}

DEFAULT_PROFILE_NAME = "balanced_platform"
DEFAULT_PROFILES = {
    "enterprise_b2b": {
        "shape_anchors": [
            (0.0, 0.52),
            (4.0, 0.42),
            (6.0, 0.50),
            (8.0, 0.95),
            (10.0, 1.22),
            (12.0, 1.28),
            (13.5, 1.05),
            (15.0, 1.15),
            (17.0, 1.30),
            (19.0, 1.10),
            (21.0, 0.90),
            (23.0, 0.68),
            (24.0, 0.52),
        ],
        "weekend_multiplier": 0.94,
        "cache_hit_rate": (0.90, 0.96),
        "avg_object_size_kb": (240, 900),
        "origin_fail_rate": (0.0001, 0.0004),
        "http_code_weights": {
            "2xx": (0.86, 0.92),
            "3xx": (0.02, 0.05),
            "4xx": (0.05, 0.10),
            "5xx": (0.003, 0.015),
        },
        "origin_http_code_weights": {
            "2xx": (0.90, 0.96),
            "3xx": (0.01, 0.04),
            "4xx": (0.01, 0.05),
            "5xx": (0.002, 0.02),
        },
    },
    "balanced_platform": {
        "shape_anchors": [
            (0.0, 0.60),
            (4.0, 0.48),
            (7.0, 0.72),
            (10.0, 1.05),
            (13.0, 1.12),
            (16.0, 1.18),
            (19.0, 1.10),
            (21.0, 0.96),
            (23.0, 0.78),
            (24.0, 0.60),
        ],
        "weekend_multiplier": 0.98,
        "cache_hit_rate": (0.88, 0.95),
        "avg_object_size_kb": (256, 1200),
        "origin_fail_rate": (0.0001, 0.0005),
        "http_code_weights": {
            "2xx": (0.84, 0.90),
            "3xx": (0.02, 0.06),
            "4xx": (0.06, 0.12),
            "5xx": (0.004, 0.02),
        },
        "origin_http_code_weights": {
            "2xx": (0.88, 0.95),
            "3xx": (0.01, 0.05),
            "4xx": (0.02, 0.06),
            "5xx": (0.003, 0.025),
        },
    },
    "game_content": {
        "shape_anchors": [
            (0.0, 0.78),
            (4.0, 0.58),
            (8.0, 0.62),
            (11.0, 0.80),
            (14.0, 0.96),
            (17.0, 1.20),
            (19.0, 1.40),
            (21.0, 1.55),
            (23.0, 1.10),
            (24.0, 0.78),
        ],
        "weekend_multiplier": 1.06,
        "cache_hit_rate": (0.85, 0.93),
        "avg_object_size_kb": (512, 4096),
        "origin_fail_rate": (0.0001, 0.0007),
        "http_code_weights": {
            "2xx": (0.82, 0.89),
            "3xx": (0.01, 0.04),
            "4xx": (0.08, 0.14),
            "5xx": (0.004, 0.025),
        },
        "origin_http_code_weights": {
            "2xx": (0.86, 0.94),
            "3xx": (0.01, 0.04),
            "4xx": (0.02, 0.07),
            "5xx": (0.004, 0.03),
        },
    },
}


@dataclass(frozen=True)
class WindowContext:
    start: datetime
    end: datetime
    timezone: ZoneInfo
    interval_seconds: int


@dataclass(frozen=True)
class FluxPlanPoint:
    timestamp_ms: int
    flux_bytes: int


def stable_seed(*parts: object) -> int:
    """把多个维度稳定映射成可复现随机种子。"""
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def decimal_tb(value_bytes: int) -> float:
    return value_bytes / float(1000**4)


def decimal_pb(value_bytes: int) -> float:
    return value_bytes / float(1000**5)


def sample_range(rng: random.Random, value: object, default: Tuple[float, float]) -> float:
    low, high = normalize_range(value, default)
    if math.isclose(low, high):
        return low
    return rng.uniform(low, high)


def normalize_range(value: object, default: Tuple[float, float]) -> Tuple[float, float]:
    """支持 [min, max] 或 {min, max} 两种配置格式。"""
    if value is None:
        return default

    if isinstance(value, dict):
        low = float(value.get("min", default[0]))
        high = float(value.get("max", default[1]))
        return (min(low, high), max(low, high))

    if isinstance(value, (list, tuple)) and len(value) == 2:
        low = float(value[0])
        high = float(value[1])
        return (min(low, high), max(low, high))

    return default


def allocate_integer_by_weights(total: int, weights: Sequence[float]) -> List[int]:
    """
    使用最大余数法做整数守恒分配。

    关键保证：
    1. 每个分配结果都是整数
    2. sum(result) == total
    3. 分配尽量接近权重比例
    """
    if total < 0:
        raise ValueError("total 不能为负数")

    if not weights:
        return []

    cleaned = [max(0.0, float(weight)) for weight in weights]
    total_weight = sum(cleaned)
    if total_weight <= 0:
        cleaned = [1.0] * len(weights)
        total_weight = float(len(weights))

    decimal_total = Decimal(total)
    decimal_weight_sum = sum(Decimal(str(weight)) for weight in cleaned)

    raw_values = []
    floors = []
    for index, weight in enumerate(cleaned):
        decimal_weight = Decimal(str(weight))
        raw = decimal_total * decimal_weight / decimal_weight_sum
        floor_value = int(raw.to_integral_value(rounding=ROUND_FLOOR))
        raw_values.append((index, raw, floor_value))
        floors.append(floor_value)

    remainder = total - sum(floors)
    if remainder > 0:
        ranked = sorted(
            raw_values,
            key=lambda item: (item[1] - Decimal(item[2]), -item[0]),
            reverse=True,
        )
        for index in range(remainder):
            floors[ranked[index][0]] += 1

    return floors


def parse_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception as exc:  # pragma: no cover - 只在非法时区时触发
        raise ValueError(f"非法时区: {name}") from exc


def parse_datetime_in_timezone(value: str, timezone: ZoneInfo) -> datetime:
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.replace(tzinfo=timezone)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间: {value}")


def ensure_aligned(dt: datetime, interval_seconds: int) -> None:
    timestamp = int(dt.timestamp())
    if timestamp % interval_seconds != 0:
        raise ValueError(
            f"时间 {dt.strftime('%Y-%m-%d %H:%M:%S %Z')} 未与 {interval_seconds}s 粒度对齐"
        )


def get_output_dir(config: Dict) -> str:
    return config.get("mode", {}).get("output_dir", "./output")


def normalize_config(config: Dict) -> Dict:
    """统一配置格式并补齐默认值。"""
    cfg = copy.deepcopy(config)

    target = cfg.setdefault("target", {})
    target.setdefault("mode", "flux_window")
    if target.get("mode") != "flux_window":
        raise ValueError("当前版本仅支持 target.mode = 'flux_window'")

    total_flux = target.get("total_flux")
    if not isinstance(total_flux, dict):
        raise ValueError("target.total_flux 配置缺失")
    if float(total_flux.get("value", 0)) <= 0:
        raise ValueError("target.total_flux.value 必须大于 0")
    total_flux.setdefault("unit", "PB")
    total_flux.setdefault("base", 1000)

    target.setdefault("profile", DEFAULT_PROFILE_NAME)
    target.setdefault("random_seed", 20260311)

    time_cfg = cfg.setdefault("time", {})
    if not time_cfg.get("start_datetime") or not time_cfg.get("end_datetime"):
        raise ValueError("time.start_datetime / time.end_datetime 必填")
    interval_seconds = int(time_cfg.get("interval_seconds", 0))
    if interval_seconds <= 0:
        raise ValueError("time.interval_seconds 必须大于 0")
    time_cfg["interval_seconds"] = interval_seconds
    time_cfg.setdefault("timezone", "Asia/Shanghai")

    timezone = parse_timezone(time_cfg["timezone"])
    start_dt = parse_datetime_in_timezone(time_cfg["start_datetime"], timezone)
    end_dt = parse_datetime_in_timezone(time_cfg["end_datetime"], timezone)
    ensure_aligned(start_dt, interval_seconds)
    ensure_aligned(end_dt, interval_seconds)
    if end_dt < start_dt:
        raise ValueError("time.end_datetime 必须晚于 time.start_datetime")

    dimensions = cfg.setdefault("dimensions", {})
    dimensions.setdefault("tenant_id", "fake-cdn")
    dimensions.setdefault("project", dimensions.get("tenant_id") or "默认")

    domain_items = dimensions.get("domains") or []
    if not domain_items:
        raise ValueError("dimensions.domains 至少需要一个域名")

    normalized_domains = []
    for item in domain_items:
        if isinstance(item, str):
            normalized_domains.append(
                {
                    "name": item,
                    "weight": 1.0,
                    "profile": target["profile"],
                }
            )
            continue

        if not isinstance(item, dict) or not item.get("name"):
            raise ValueError("domains 中每项必须是字符串或包含 name 的对象")

        normalized_domains.append(
            {
                "name": item["name"],
                "weight": float(item.get("weight", 1.0)),
                "profile": item.get("profile", target["profile"]),
            }
        )

    if sum(max(0.0, item["weight"]) for item in normalized_domains) <= 0:
        raise ValueError("domains.weight 总和必须大于 0")
    dimensions["domains"] = normalized_domains

    raw_regions = dimensions.get("regions") or [
        {"country": "cn", "region": "mainland_china", "weight": 1.0}
    ]
    merged_regions: Dict[Tuple[str, str], float] = defaultdict(float)
    for item in raw_regions:
        if not isinstance(item, dict):
            raise ValueError("regions 中每项必须是对象")
        country = item.get("country", "cn")
        region = item.get("region", "mainland_china")
        merged_regions[(country, region)] += float(item.get("weight", 1.0))

    regions = [
        {"country": country, "region": region, "weight": weight}
        for (country, region), weight in merged_regions.items()
        if weight > 0
    ]
    if not regions:
        raise ValueError("regions.weight 总和必须大于 0")
    dimensions["regions"] = regions

    realism = cfg.setdefault("realism", {})
    realism.setdefault("weekday_factors", DEFAULT_WEEKDAY_FACTORS)
    realism.setdefault("day_noise_ratio", 0.03)
    realism.setdefault("slot_noise_ratio", 0.05)
    realism.setdefault("month_edge_boost", 0.05)
    realism.setdefault("anomaly_probability", 0.001)
    realism.setdefault("events", [])
    realism.setdefault("cache_hit_rate", {"min": 0.88, "max": 0.95})
    realism.setdefault("avg_object_size_kb", {"min": 200, "max": 2048})
    realism.setdefault("origin_fail_rate", {"min": 0.0001, "max": 0.0005})
    realism.setdefault("profiles", {})

    api = cfg.setdefault("api", {})
    api.setdefault("endpoint", "")
    api.setdefault("headers", {})
    api["headers"].setdefault("vip", "")
    api["headers"].setdefault("Content-Type", "application/json")
    api.setdefault("timeout", 10)
    api.setdefault("retry", 3)
    api.setdefault("batch_size", 10)

    deployment = cfg.setdefault("deployment", {})
    deployment.setdefault("platform", "CUG")
    deployment.setdefault("mode", "preview")
    if deployment["mode"] not in ("preview", "push"):
        raise ValueError(f"deployment.mode 必须是 preview 或 push，收到 {deployment['mode']!r}")

    mode = cfg.setdefault("mode", {})
    mode.setdefault("run_mode", "simulation")
    mode.setdefault("dry_run", True)
    mode.setdefault("save_local", True)
    mode.setdefault("output_dir", "./output")

    # preview 模式强制 dry_run=true，消除"看着推了其实没推"歧义
    if deployment["mode"] == "preview":
        mode["dry_run"] = True

    # 归一化 weekday 因子，缺失键回退默认值
    normalized_weekdays = DEFAULT_WEEKDAY_FACTORS.copy()
    normalized_weekdays.update(realism.get("weekday_factors", {}))
    realism["weekday_factors"] = normalized_weekdays

    return cfg


class TrafficTargetParser:
    """解析总流量配置。"""

    UNIT_POWERS = {
        "B": 0,
        "KB": 1,
        "MB": 2,
        "GB": 3,
        "TB": 4,
        "PB": 5,
        "KIB": 1,
        "MIB": 2,
        "GIB": 3,
        "TIB": 4,
        "PIB": 5,
    }

    @classmethod
    def parse_total_flux_bytes(cls, config: Dict) -> int:
        total_flux_cfg = config["target"]["total_flux"]
        unit = str(total_flux_cfg.get("unit", "PB")).upper()
        if unit not in cls.UNIT_POWERS:
            raise ValueError(f"不支持的流量单位: {unit}")

        configured_base = int(total_flux_cfg.get("base", 1000))
        if configured_base not in (1000, 1024):
            raise ValueError("target.total_flux.base 仅支持 1000 或 1024")

        # 若使用二进制单位但 base 未显式设置为 1024，则以 unit 优先。
        if unit.endswith("IB"):
            configured_base = 1024

        value = Decimal(str(total_flux_cfg["value"]))
        multiplier = Decimal(configured_base) ** cls.UNIT_POWERS[unit]
        return int((value * multiplier).to_integral_value(rounding=ROUND_HALF_UP))


class TimeWindowBuilder:
    """负责构建带时区的时间窗口。"""

    def __init__(self, config: Dict):
        self.config = normalize_config(config)
        time_cfg = self.config["time"]
        self.timezone = parse_timezone(time_cfg["timezone"])
        self.interval_seconds = int(time_cfg["interval_seconds"])
        self.start = parse_datetime_in_timezone(time_cfg["start_datetime"], self.timezone)
        self.end = parse_datetime_in_timezone(time_cfg["end_datetime"], self.timezone)

    def build_context(self) -> WindowContext:
        return WindowContext(
            start=self.start,
            end=self.end,
            timezone=self.timezone,
            interval_seconds=self.interval_seconds,
        )

    def build_slots(self) -> List[datetime]:
        slots: List[datetime] = []
        current = self.start
        while current <= self.end:
            slots.append(current)
            current += timedelta(seconds=self.interval_seconds)
        return slots


class TrafficProfileLibrary:
    """业务画像库：决定日内形态与指标参数。"""

    def __init__(self, config: Dict):
        self.config = normalize_config(config)
        self._profiles = copy.deepcopy(DEFAULT_PROFILES)
        self._profiles.update(self.config.get("realism", {}).get("profiles", {}))
        self._mean_cache: Dict[str, float] = {}

    def get(self, name: str) -> Dict:
        profile_name = name if name in self._profiles else self.config["target"]["profile"]
        if profile_name not in self._profiles:
            profile_name = DEFAULT_PROFILE_NAME
        profile = copy.deepcopy(self._profiles[profile_name])

        realism = self.config["realism"]
        profile.setdefault(
            "cache_hit_rate", normalize_range(realism.get("cache_hit_rate"), (0.88, 0.95))
        )
        profile.setdefault(
            "avg_object_size_kb", normalize_range(realism.get("avg_object_size_kb"), (200, 2048))
        )
        profile.setdefault(
            "origin_fail_rate", normalize_range(realism.get("origin_fail_rate"), (0.0001, 0.0005))
        )
        profile.setdefault("weekend_multiplier", 1.0)
        return profile

    def slot_factor(self, name: str, dt: datetime) -> float:
        profile = self.get(name)
        factor = self._interpolate_factor(profile["shape_anchors"], dt.hour + dt.minute / 60.0)
        mean_factor = self._profile_mean(name)
        normalized = factor / mean_factor if mean_factor > 0 else factor
        if dt.weekday() >= 5:
            normalized *= float(profile.get("weekend_multiplier", 1.0))
        return max(0.05, normalized)

    def _profile_mean(self, name: str) -> float:
        if name in self._mean_cache:
            return self._mean_cache[name]

        anchors = self.get(name)["shape_anchors"]
        samples = []
        for minute in range(0, 24 * 60, 5):
            hour = minute / 60.0
            samples.append(self._interpolate_factor(anchors, hour))
        mean_value = sum(samples) / len(samples)
        self._mean_cache[name] = mean_value
        return mean_value

    @staticmethod
    def _interpolate_factor(anchors: Sequence[Tuple[float, float]], hour: float) -> float:
        sorted_anchors = sorted((float(x), float(y)) for x, y in anchors)
        if hour <= sorted_anchors[0][0]:
            return sorted_anchors[0][1]
        if hour >= sorted_anchors[-1][0]:
            return sorted_anchors[-1][1]

        for left, right in zip(sorted_anchors, sorted_anchors[1:]):
            left_x, left_y = left
            right_x, right_y = right
            if left_x <= hour <= right_x:
                ratio = (hour - left_x) / (right_x - left_x)
                return left_y + (right_y - left_y) * ratio
        return sorted_anchors[-1][1]


class FluxCurveGenerator:
    """根据总流量和时间窗口生成流量计划。"""

    def __init__(self, config: Dict):
        self.config = normalize_config(config)
        self.time_builder = TimeWindowBuilder(self.config)
        self.profile_library = TrafficProfileLibrary(self.config)
        self.total_flux_bytes = TrafficTargetParser.parse_total_flux_bytes(self.config)
        self.base_seed = int(self.config["target"]["random_seed"])
        self.realism = self.config["realism"]
        self.parsed_events = self._parse_events()

    def generate(self) -> List[FluxPlanPoint]:
        slots = self.time_builder.build_slots()
        if not slots:
            return []

        rng = random.Random(self.base_seed)
        unique_days = []
        day_to_index: Dict[str, int] = {}
        for slot in slots:
            day_key = slot.date().isoformat()
            if day_key not in day_to_index:
                day_to_index[day_key] = len(unique_days)
                unique_days.append(slot.date())

        day_noise = self._smoothed_noise_series(
            len(unique_days),
            float(self.realism.get("day_noise_ratio", 0.03)),
            rng,
            alpha=0.82,
        )
        slot_noise = self._smoothed_noise_series(
            len(slots),
            float(self.realism.get("slot_noise_ratio", 0.05)),
            rng,
            alpha=0.88,
        )

        target_profile = self.config["target"]["profile"]
        raw_weights: List[float] = []
        for index, slot in enumerate(slots):
            day_key = slot.date().isoformat()
            weekday_key = WEEKDAY_KEYS[slot.weekday()]
            weekday_factor = float(self.realism["weekday_factors"].get(weekday_key, 1.0))
            month_edge_factor = self._month_edge_factor(slot)
            day_factor = weekday_factor * month_edge_factor * day_noise[day_to_index[day_key]]
            slot_factor = self.profile_library.slot_factor(target_profile, slot)
            event_factor = self._event_factor(slot)
            raw_weights.append(
                max(0.000001, day_factor * slot_factor * slot_noise[index] * event_factor)
            )

        allocated_fluxes = allocate_integer_by_weights(self.total_flux_bytes, raw_weights)
        return [
            FluxPlanPoint(timestamp_ms=int(slot.timestamp() * 1000), flux_bytes=flux_bytes)
            for slot, flux_bytes in zip(slots, allocated_fluxes)
        ]

    def _month_edge_factor(self, dt: datetime) -> float:
        boost = float(self.realism.get("month_edge_boost", 0.05))
        if boost <= 0:
            return 1.0
        last_day = calendar.monthrange(dt.year, dt.month)[1]
        if dt.day <= 2 or dt.day >= last_day - 1:
            return 1.0 + boost
        return 1.0

    def _parse_events(self) -> List[Tuple[datetime, datetime, float]]:
        timezone = self.time_builder.timezone
        events = []
        for item in self.realism.get("events", []):
            if not isinstance(item, dict):
                continue
            start = item.get("start") or item.get("start_datetime")
            end = item.get("end") or item.get("end_datetime")
            if not start or not end:
                continue
            start_dt = parse_datetime_in_timezone(str(start), timezone)
            end_dt = parse_datetime_in_timezone(str(end), timezone)
            if end_dt < start_dt:
                continue
            factor = float(item.get("factor", 1.0 + float(item.get("boost", 0.0))))
            events.append((start_dt, end_dt, max(0.01, factor)))
        return events

    def _event_factor(self, dt: datetime) -> float:
        factor = 1.0
        for start, end, boost_factor in self.parsed_events:
            if start <= dt <= end:
                factor *= boost_factor
        return factor

    @staticmethod
    def _smoothed_noise_series(
        count: int,
        ratio: float,
        rng: random.Random,
        alpha: float,
    ) -> List[float]:
        if count <= 0:
            return []
        if ratio <= 0:
            return [1.0] * count

        values = []
        current = 1.0
        lower = max(0.1, 1.0 - ratio)
        upper = 1.0 + ratio
        for _ in range(count):
            shock = 1.0 + rng.uniform(-ratio, ratio)
            current = alpha * current + (1.0 - alpha) * shock
            current = min(upper, max(lower, current))
            values.append(current)
        return values


class MetricsDerivator:
    """从 flux 推导 CDN 各项指标。"""

    def __init__(self, config: Dict, profile_library: Optional[TrafficProfileLibrary] = None):
        self.config = normalize_config(config)
        self.profile_library = profile_library or TrafficProfileLibrary(self.config)

    def derive_from_flux(
        self,
        flux_bytes: int,
        interval_seconds: int,
        profile_name: str,
        rng: random.Random,
    ) -> Dict:
        if flux_bytes <= 0:
            return {
                "bw": 0,
                "flux": 0,
                "bs_bw": 0,
                "bs_flux": 0,
                "req_num": 0,
                "hit_num": 0,
                "bs_num": 0,
                "bs_fail_num": 0,
                "hit_flux": 0,
                "http_code_2xx": 0,
                "http_code_3xx": 0,
                "http_code_4xx": 0,
                "http_code_5xx": 0,
                "bs_http_code_2xx": 0,
                "bs_http_code_3xx": 0,
                "bs_http_code_4xx": 0,
                "bs_http_code_5xx": 0,
            }

        profile = self.profile_library.get(profile_name)

        cache_hit_rate = sample_range(rng, profile.get("cache_hit_rate"), (0.88, 0.95))
        avg_object_size_kb = sample_range(rng, profile.get("avg_object_size_kb"), (200, 2048))
        avg_object_size_bytes = max(1.0, avg_object_size_kb * 1024)
        origin_fail_rate = sample_range(rng, profile.get("origin_fail_rate"), (0.0001, 0.0005))

        bw_bits = int(flux_bytes * 8)

        flux_splits = allocate_integer_by_weights(
            flux_bytes, [cache_hit_rate, 1.0 - cache_hit_rate]
        )
        hit_flux_bytes, bs_flux_bytes = flux_splits[0], flux_splits[1]
        bs_bw_bits = int(bs_flux_bytes * 8)

        req_num = max(1, int(round(flux_bytes / avg_object_size_bytes)))
        req_splits = allocate_integer_by_weights(req_num, [cache_hit_rate, 1.0 - cache_hit_rate])
        hit_num, bs_num = req_splits[0], req_splits[1]
        bs_fail_num = allocate_integer_by_weights(
            bs_num, [origin_fail_rate, 1.0 - origin_fail_rate]
        )[0]

        http_counts = self._allocate_http_codes(req_num, profile["http_code_weights"], rng)
        origin_http_counts = self._allocate_http_codes(
            bs_num, profile["origin_http_code_weights"], rng
        )

        return {
            "bw": bw_bits,
            "flux": flux_bytes,
            "bs_bw": bs_bw_bits,
            "bs_flux": bs_flux_bytes,
            "req_num": req_num,
            "hit_num": hit_num,
            "bs_num": bs_num,
            "bs_fail_num": bs_fail_num,
            "hit_flux": hit_flux_bytes,
            "http_code_2xx": http_counts[0],
            "http_code_3xx": http_counts[1],
            "http_code_4xx": http_counts[2],
            "http_code_5xx": http_counts[3],
            "bs_http_code_2xx": origin_http_counts[0],
            "bs_http_code_3xx": origin_http_counts[1],
            "bs_http_code_4xx": origin_http_counts[2],
            "bs_http_code_5xx": origin_http_counts[3],
        }

    @staticmethod
    def _allocate_http_codes(
        total: int, weight_ranges: Dict[str, Tuple[float, float]], rng: random.Random
    ) -> List[int]:
        if total <= 0:
            return [0, 0, 0, 0]

        weights = [
            sample_range(rng, weight_ranges.get("2xx"), (0.84, 0.90)),
            sample_range(rng, weight_ranges.get("3xx"), (0.02, 0.06)),
            sample_range(rng, weight_ranges.get("4xx"), (0.06, 0.12)),
            sample_range(rng, weight_ranges.get("5xx"), (0.004, 0.02)),
        ]
        return allocate_integer_by_weights(total, weights)


class AnomalyInjector:
    """注入更贴近真实业务的指标异常，但不修改总 flux。"""

    def __init__(self, config: Dict):
        self.config = normalize_config(config)
        self.realism = self.config["realism"]

    def inject(self, metrics: Dict, timestamp_ms: int, rng: random.Random) -> Dict:
        metrics = copy.deepcopy(metrics)
        if metrics["req_num"] <= 0:
            return metrics

        dt = datetime.fromtimestamp(
            timestamp_ms / 1000, tz=parse_timezone(self.config["time"]["timezone"])
        )
        anomaly_probability = float(self.realism.get("anomaly_probability", 0.001))

        # 1. 凌晨维护窗口：轻微提升客户端 5xx
        if dt.hour in (2, 3, 4) and rng.random() < 0.04:
            shift = min(metrics["http_code_2xx"], int(metrics["req_num"] * rng.uniform(0.03, 0.08)))
            metrics["http_code_2xx"] -= shift
            metrics["http_code_5xx"] += shift

        # 2. 源站故障：回源失败与回源 5xx 升高
        if rng.random() < anomaly_probability:
            fail_rate = rng.uniform(0.25, 0.65)
            metrics["bs_fail_num"] = allocate_integer_by_weights(
                metrics["bs_num"], [fail_rate, 1.0 - fail_rate]
            )[0]
            origin_success = max(0, metrics["bs_num"] - metrics["bs_fail_num"])
            metrics["bs_http_code_5xx"] = metrics["bs_fail_num"]
            remaining = allocate_integer_by_weights(origin_success, [0.92, 0.03, 0.05])
            metrics["bs_http_code_2xx"] = remaining[0]
            metrics["bs_http_code_3xx"] = remaining[1]
            metrics["bs_http_code_4xx"] = remaining[2]

        # 3. 缓存刷新：命中率短时下降，回源流量提升，但总流量保持不变
        if rng.random() < 0.008:
            new_hit_rate = rng.uniform(0.55, 0.75)
            req_split = allocate_integer_by_weights(
                metrics["req_num"], [new_hit_rate, 1.0 - new_hit_rate]
            )
            metrics["hit_num"] = req_split[0]
            metrics["bs_num"] = req_split[1]

            flux_split = allocate_integer_by_weights(
                metrics["flux"], [new_hit_rate, 1.0 - new_hit_rate]
            )
            metrics["hit_flux"] = flux_split[0]
            metrics["bs_flux"] = flux_split[1]
            metrics["bs_bw"] = metrics["bs_flux"] * 8

            metrics["bs_fail_num"] = min(metrics["bs_fail_num"], metrics["bs_num"])
            origin_weights = [
                max(metrics["bs_http_code_2xx"], 1),
                max(metrics["bs_http_code_3xx"], 1),
                max(metrics["bs_http_code_4xx"], 1),
                max(metrics["bs_http_code_5xx"], 1),
            ]
            rebalanced = allocate_integer_by_weights(metrics["bs_num"], origin_weights)
            metrics["bs_http_code_2xx"] = rebalanced[0]
            metrics["bs_http_code_3xx"] = rebalanced[1]
            metrics["bs_http_code_4xx"] = rebalanced[2]
            metrics["bs_http_code_5xx"] = rebalanced[3]

        # 4. 爬虫 / 攻击：4xx 抬升
        if rng.random() < 0.004:
            shift = min(metrics["http_code_2xx"], int(metrics["req_num"] * rng.uniform(0.10, 0.22)))
            metrics["http_code_2xx"] -= shift
            metrics["http_code_4xx"] += shift

        return metrics


class MultiDimensionDistributor:
    """按域名 / 地区拆分时间点总流量。"""

    def __init__(self, config: Dict, profile_library: Optional[TrafficProfileLibrary] = None):
        self.config = normalize_config(config)
        self.profile_library = profile_library or TrafficProfileLibrary(self.config)
        self.domains = self.config["dimensions"]["domains"]
        self.regions = self.config["dimensions"]["regions"]

    def distribute_flux(self, total_flux_bytes: int, timestamp_ms: int) -> List[Dict]:
        dt = datetime.fromtimestamp(
            timestamp_ms / 1000, tz=parse_timezone(self.config["time"]["timezone"])
        )

        domain_weights = []
        for domain in self.domains:
            base_weight = float(domain["weight"])
            time_factor = self.profile_library.slot_factor(domain["profile"], dt)
            domain_weights.append(base_weight * time_factor)

        domain_fluxes = allocate_integer_by_weights(total_flux_bytes, domain_weights)

        distributed: List[Dict] = []
        region_weights = [float(region["weight"]) for region in self.regions]
        for domain, domain_flux in zip(self.domains, domain_fluxes):
            if domain_flux <= 0:
                continue
            region_fluxes = allocate_integer_by_weights(domain_flux, region_weights)
            for region, region_flux in zip(self.regions, region_fluxes):
                if region_flux <= 0:
                    continue
                distributed.append(
                    {
                        "domain": domain["name"],
                        "profile": domain["profile"],
                        "country": region["country"],
                        "region": region["region"],
                        "flux_bytes": region_flux,
                    }
                )
        return distributed


class CDNLogGenerator:
    """CDN 日志主生成器。"""

    def __init__(self, config: Dict):
        self.config = normalize_config(config)
        self.time_builder = TimeWindowBuilder(self.config)
        self.profile_library = TrafficProfileLibrary(self.config)
        self.curve_generator = FluxCurveGenerator(self.config)
        self.metrics_derivator = MetricsDerivator(self.config, self.profile_library)
        self.anomaly_injector = AnomalyInjector(self.config)
        self.distributor = MultiDimensionDistributor(self.config, self.profile_library)
        self.base_seed = int(self.config["target"]["random_seed"])

    def generate_window_plan(self) -> List[FluxPlanPoint]:
        return self.curve_generator.generate()

    def generate_logs_for_slot(self, plan_point: FluxPlanPoint) -> List[Dict]:
        distributed_fluxes = self.distributor.distribute_flux(
            total_flux_bytes=plan_point.flux_bytes,
            timestamp_ms=plan_point.timestamp_ms,
        )
        logs: List[Dict] = []
        interval_seconds = self.config["time"]["interval_seconds"]
        timezone = parse_timezone(self.config["time"]["timezone"])
        slot_dt = datetime.fromtimestamp(plan_point.timestamp_ms / 1000, tz=timezone)
        runtime = self.config.get("_runtime", {})

        for item in distributed_fluxes:
            rng_seed = stable_seed(
                self.base_seed,
                plan_point.timestamp_ms,
                item["domain"],
                item["country"],
                item["region"],
            )
            rng = random.Random(rng_seed)
            metrics = self.metrics_derivator.derive_from_flux(
                flux_bytes=item["flux_bytes"],
                interval_seconds=interval_seconds,
                profile_name=item["profile"],
                rng=rng,
            )
            metrics = self.anomaly_injector.inject(metrics, plan_point.timestamp_ms, rng)

            logs.append(
                {
                    "tenantId": self.config["dimensions"]["tenant_id"],
                    "project": self.config["dimensions"].get("project")
                    or self.config["dimensions"].get("tenant_id")
                    or "默认",
                    "configVersionId": runtime.get("config_version_id"),
                    "generationJobId": runtime.get("generation_job_id"),
                    "start_time": plan_point.timestamp_ms,
                    "country": item["country"],
                    "region": item["region"],
                    "domain": item["domain"],
                    "interval": interval_seconds,
                    "slot_time": slot_dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
                    **metrics,
                }
            )

        return logs

    def generate_window_logs(self) -> Tuple[List[Dict], Dict]:
        plan = self.generate_window_plan()
        print(f"[生成] 时间点数量: {len(plan)}")
        print("[生成] 开始生成日志...")

        logs: List[Dict] = []
        for index, plan_point in enumerate(plan, start=1):
            logs.extend(self.generate_logs_for_slot(plan_point))
            if index % 500 == 0 or index == len(plan):
                print(f"  进度: {index}/{len(plan)} ({index / len(plan) * 100:.1f}%)")

        stats = self.calculate_stats(plan, logs)
        print(f"[完成] 共生成 {len(logs)} 条日志记录")
        print(f"[统计] 目标总流量: {stats['target_total_flux_pb']:.4f} PB")
        print(f"[统计] 实际总流量: {stats['actual_total_flux_pb']:.4f} PB")
        print(f"[统计] 等效平均带宽: {stats['equivalent_avg_gbps']:.2f} Gbps")
        print(f"[统计] 工作日平均: {stats['weekday_avg_tb']:.2f} TB/天")
        print(f"[统计] 周末平均: {stats['weekend_avg_tb']:.2f} TB/天")
        return logs, stats

    def calculate_stats(self, plan: Sequence[FluxPlanPoint], logs: Sequence[Dict]) -> Dict:
        if not plan:
            return {}

        target_total_flux_bytes = TrafficTargetParser.parse_total_flux_bytes(self.config)
        actual_total_flux_bytes = sum(item.flux_bytes for item in plan)
        interval_seconds = self.config["time"]["interval_seconds"]
        slot_gbps = [item.flux_bytes * 8 / interval_seconds / 1_000_000_000 for item in plan]

        domain_flux = defaultdict(int)
        per_day_flux = defaultdict(int)
        for log in logs:
            domain_flux[log["domain"]] += int(log["flux"])
            dt = datetime.fromtimestamp(
                log["start_time"] / 1000,
                tz=parse_timezone(self.config["time"]["timezone"]),
            )
            per_day_flux[dt.date().isoformat()] += int(log["flux"])

        weekday_values = []
        weekend_values = []
        for day_string, total_flux in per_day_flux.items():
            dt = datetime.strptime(day_string, "%Y-%m-%d")
            if dt.weekday() >= 5:
                weekend_values.append(total_flux)
            else:
                weekday_values.append(total_flux)

        domain_shares = []
        for domain, flux in sorted(domain_flux.items(), key=lambda item: item[1], reverse=True):
            share = flux / actual_total_flux_bytes if actual_total_flux_bytes else 0.0
            domain_shares.append({"domain": domain, "flux_pb": decimal_pb(flux), "share": share})

        return {
            "window_start": self.config["time"]["start_datetime"],
            "window_end": self.config["time"]["end_datetime"],
            "interval_seconds": interval_seconds,
            "total_points": len(plan),
            "target_total_flux_bytes": target_total_flux_bytes,
            "target_total_flux_pb": decimal_pb(target_total_flux_bytes),
            "actual_total_flux_bytes": actual_total_flux_bytes,
            "actual_total_flux_pb": decimal_pb(actual_total_flux_bytes),
            "actual_total_flux_tb": decimal_tb(actual_total_flux_bytes),
            "equivalent_avg_gbps": actual_total_flux_bytes
            * 8
            / (len(plan) * interval_seconds)
            / 1_000_000_000,
            "p50_gbps": _percentile(slot_gbps, 0.50),
            "p95_gbps": _percentile(slot_gbps, 0.95),
            "p99_gbps": _percentile(slot_gbps, 0.99),
            "max_gbps": max(slot_gbps),
            "min_gbps": min(slot_gbps),
            "weekday_avg_tb": (
                decimal_tb(int(sum(weekday_values) / len(weekday_values)))
                if weekday_values
                else 0.0
            ),
            "weekend_avg_tb": (
                decimal_tb(int(sum(weekend_values) / len(weekend_values)))
                if weekend_values
                else 0.0
            ),
            "peak_to_valley_ratio": (max(slot_gbps) / max(min(slot_gbps), 0.000001)),
            "top_domains": domain_shares[:10],
        }


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    index = int((len(sorted_values) - 1) * percentile)
    index = max(0, min(len(sorted_values) - 1, index))
    return sorted_values[index]
