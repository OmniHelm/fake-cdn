"""Fake CDN - 基于总流量窗口的 CDN 日志模拟系统。"""

__version__ = "2.0.0"
__author__ = "jez"

from fake_cdn.core.generator import CDNLogGenerator, FluxCurveGenerator, TrafficTargetParser
from fake_cdn.core.pusher import LocalSaver, LogPusher
from fake_cdn.core.scheduler import CatchupScheduler, RealtimeScheduler
from fake_cdn.core.validator import BillingCalculator, FluxWindowValidator, Percentile95Validator

__all__ = [
    "CDNLogGenerator",
    "FluxCurveGenerator",
    "TrafficTargetParser",
    "LogPusher",
    "LocalSaver",
    "RealtimeScheduler",
    "CatchupScheduler",
    "FluxWindowValidator",
    "Percentile95Validator",
    "BillingCalculator",
]
