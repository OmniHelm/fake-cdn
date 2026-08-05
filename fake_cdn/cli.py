#!/usr/bin/env python3
"""Fake CDN CLI - 基于总流量 + 时间窗口生成 CDN 推送日志。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

from fake_cdn.core.config_manager import ConfigManagerError
from fake_cdn.core.generator import (
    CDNLogGenerator,
    TimeWindowBuilder,
    TrafficTargetParser,
    decimal_pb,
    decimal_tb,
    get_output_dir,
    normalize_config,
    parse_datetime_in_timezone,
    parse_timezone,
)
from fake_cdn.core.pusher import LocalSaver, LogPusher
from fake_cdn.core.scheduler import CatchupScheduler, RealtimeScheduler
from fake_cdn.core.storage import get_default_storage
from fake_cdn.core.tenant_config import TenantConfigStore, get_default_config_store
from fake_cdn.core.validator import (
    BillingCalculator,
    FluxWindowValidator,
    validate_from_file,
)


def load_config(config_path: str = "./config.json") -> Dict:
    try:
        with open(config_path, "r", encoding="utf-8") as file:
            config = json.load(file)
    except FileNotFoundError:
        print(f"[错误] 配置文件不存在: {config_path}")
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"[错误] 配置文件格式错误: {exc}")
        sys.exit(1)

    try:
        return apply_api_environment(config)
    except ValueError as exc:
        print(f"[错误] 配置校验失败: {exc}")
        sys.exit(1)


def apply_api_environment(config: Dict) -> Dict:
    """在运行时覆盖敏感 API 参数，数据库中不持久化环境变量值。"""
    config = deepcopy(config)
    if os.environ.get("CDN_API_ENDPOINT"):
        config.setdefault("api", {}).setdefault("headers", {})
        config["api"]["endpoint"] = os.environ["CDN_API_ENDPOINT"]
        print(f"[环境变量] API endpoint: {config['api']['endpoint']}")
    if os.environ.get("CDN_API_VIP"):
        config.setdefault("api", {}).setdefault("headers", {})
        config["api"]["headers"]["vip"] = os.environ["CDN_API_VIP"]
        print(f"[环境变量] API vip: {config['api']['headers']['vip']}")
    return normalize_config(config)


def load_tenant_config(args) -> Tuple[Dict, Optional[TenantConfigStore], Optional[str]]:
    """优先从租户配置库读取已发布版本；未指定租户时保留文件兼容模式。"""
    if not args.tenant_id:
        print("[兼容模式] 未指定 --tenant-id，继续读取 config.json；建议迁移到配置数据库")
        return load_config(args.config), None, None

    store = TenantConfigStore(args.config_db) if args.config_db else get_default_config_store()
    store.bootstrap([args.config])
    snapshot = store.resolve_active(args.tenant_id)
    config = apply_api_environment(snapshot["config"])
    if args.mode not in {"simulation", "realtime", "catchup"}:
        config["_runtime"] = {
            "tenant_id": args.tenant_id,
            "config_version_id": snapshot["id"],
            "config_checksum": snapshot["checksum"],
        }
        return config, store, None

    job = store.create_job(
        args.tenant_id,
        snapshot["id"],
        args.mode,
        config.get("mode", {}).get("output_dir", "./output"),
    )
    config.setdefault("mode", {})["output_dir"] = job["output_dir"]
    central_log_db = os.environ.get("FAKE_CDN_DB_PATH") or str(
        Path(__file__).resolve().parent.parent / "output" / "cdn_logs.db"
    )
    config["_runtime"] = {
        "tenant_id": args.tenant_id,
        "config_version_id": snapshot["id"],
        "config_checksum": snapshot["checksum"],
        "generation_job_id": job["job_id"],
        "log_db_path": central_log_db,
    }
    print(
        f"[租户配置] {args.tenant_id} · v{snapshot['version_no']} · "
        f"{snapshot['checksum'][:12]} · {job['job_id']}"
    )
    return config, store, job["job_id"]


def build_config_summary(config: Dict) -> Dict:
    builder = TimeWindowBuilder(config)
    slots = builder.build_slots()
    total_flux_bytes = TrafficTargetParser.parse_total_flux_bytes(config)
    total_seconds = len(slots) * config["time"]["interval_seconds"]
    equivalent_avg_gbps = (
        total_flux_bytes * 8 / total_seconds / 1_000_000_000 if total_seconds else 0.0
    )
    return {
        "total_flux_bytes": total_flux_bytes,
        "total_flux_pb": decimal_pb(total_flux_bytes),
        "total_flux_tb": decimal_tb(total_flux_bytes),
        "total_points": len(slots),
        "equivalent_avg_gbps": equivalent_avg_gbps,
        "domain_count": len(config["dimensions"]["domains"]),
        "region_count": len(config["dimensions"]["regions"]),
    }


def print_config_summary(config: Dict) -> None:
    summary = build_config_summary(config)
    print(
        f"[配置] 总流量目标: {summary['total_flux_pb']:.4f} PB ({summary['total_flux_tb']:.2f} TB)"
    )
    print(f"[配置] 时间窗口: {config['time']['start_datetime']} ~ {config['time']['end_datetime']}")
    print(f"[配置] 时间粒度: {config['time']['interval_seconds']} 秒")
    print(f"[配置] 时间点数: {summary['total_points']}")
    print(f"[配置] 等效平均带宽: {summary['equivalent_avg_gbps']:.2f} Gbps")
    print(f"[配置] 域名数 / 地区数: {summary['domain_count']} / {summary['region_count']}")
    print(f"[配置] Dry-Run: {config['mode']['dry_run']}")
    deployment = config.get("deployment", {})
    print(f"[配置] 部署: {deployment.get('platform', '—')} ({deployment.get('mode', 'preview')})")
    print()


def ensure_push_confirmed(config: Dict, args) -> None:
    if config["mode"]["dry_run"]:
        return

    if not config["api"]["endpoint"]:
        print("[错误] 未配置 API endpoint")
        print("请设置环境变量: export CDN_API_ENDPOINT=<your_endpoint>")
        sys.exit(1)
    if not config["api"]["headers"].get("vip"):
        print("[错误] 未配置 API vip")
        print("请设置环境变量: export CDN_API_VIP=<your_vip>")
        sys.exit(1)

    print("⚠️  警告: Dry-Run 已关闭，将真实推送数据到 API!")
    print(f"⚠️  目标 API: {config['api']['endpoint']}")

    if args.yes:
        print("[跳过确认] 使用 -y/--yes 参数")
        return

    if sys.stdin.isatty():
        response = input("请确认是否继续? (输入 yes 继续): ")
        if response.strip().lower() != "yes":
            print("已取消")
            sys.exit(0)
        return

    print("[错误] 非交互模式下需要 -y/--yes 参数确认")
    sys.exit(1)


def mode_simulation(config: Dict, args) -> None:
    print("\n" + "=" * 72)
    print("模式: 模拟生成 (Simulation)")
    print("=" * 72 + "\n")

    generator = CDNLogGenerator(config)
    plan = generator.generate_window_plan()
    logs, stats = generator.generate_window_logs()

    if config["mode"].get("save_local", True):
        output_dir = get_output_dir(config)
        LocalSaver.save_logs(
            logs,
            output_dir,
            db_path=config.get("_runtime", {}).get("log_db_path"),
        )
        LocalSaver.save_stats(stats, output_dir, "stats.json")
        LocalSaver.save_flux_curve(
            plan, output_dir, "flux_curve.csv", config["time"]["interval_seconds"]
        )

    print("\n[验证] 开始校验...")
    result = FluxWindowValidator.validate_logs(logs, config)
    FluxWindowValidator.print_report(result)

    slot_gbps = [
        point.flux_bytes * 8 / config["time"]["interval_seconds"] / 1_000_000_000 for point in plan
    ]
    billing = BillingCalculator.calculate_95_billing(slot_gbps)
    BillingCalculator.print_billing_report(billing)

    if not config["mode"].get("dry_run", True):
        print("\n[推送] 开始推送到 API...")
        pusher = LogPusher(config)
        pusher.push_all(logs, dry_run=False)
    else:
        print("\n[跳过] dry_run=true，不真实推送到 API")

    print("\n[完成] 模拟生成完成!\n")


def parse_realtime_end_datetime(raw_value: Optional[str], timezone_name: str) -> Optional[datetime]:
    if not raw_value:
        return None
    timezone = parse_timezone(timezone_name)
    return parse_datetime_in_timezone(raw_value, timezone)


def mode_realtime(config: Dict, args) -> None:
    print("\n" + "=" * 72)
    print("模式: 实时推送 (Realtime)")
    print("=" * 72 + "\n")

    scheduler = RealtimeScheduler(config)
    dry_run = config["mode"].get("dry_run", True)
    end_datetime = parse_realtime_end_datetime(args.end_datetime, config["time"]["timezone"])

    if end_datetime:
        print(f"[配置] 将在 {end_datetime.strftime('%Y-%m-%d %H:%M:%S %Z')} 自动停止")

    if args.once:
        scheduler.run_once(dry_run)
    else:
        scheduler.run_forever(dry_run, end_datetime=end_datetime)


def normalize_catchup_window(config: Dict, args) -> Tuple[Optional[str], Optional[str]]:
    interval = int(config["time"]["interval_seconds"])
    timezone = parse_timezone(config["time"]["timezone"])

    start_datetime = args.start_datetime
    end_datetime = args.end_datetime

    if args.start_date and not start_datetime:
        start_datetime = f"{args.start_date} 00:00:00"

    if args.end_date and not end_datetime:
        next_day = datetime.strptime(args.end_date, "%Y-%m-%d") + timedelta(days=1)
        end_dt = next_day - timedelta(seconds=interval)
        end_datetime = end_dt.strftime("%Y-%m-%d %H:%M:%S")

    if start_datetime:
        parse_datetime_in_timezone(start_datetime, timezone)
    if end_datetime:
        parse_datetime_in_timezone(end_datetime, timezone)

    return start_datetime, end_datetime


def mode_catchup(config: Dict, args) -> None:
    print("\n" + "=" * 72)
    print("模式: 补推历史数据 (Catchup)")
    print("=" * 72 + "\n")

    start_datetime, end_datetime = normalize_catchup_window(config, args)
    scheduler = CatchupScheduler(config, start_datetime=start_datetime, end_datetime=end_datetime)
    dry_run = config["mode"].get("dry_run", True)
    stats = scheduler.run(dry_run)

    print("\n[完成] 补推完成!")
    print(f"  总流量: {stats['actual_total_flux_pb']:.4f} PB")
    print(f"  等效平均带宽: {stats['equivalent_avg_gbps']:.2f} Gbps")
    print(f"  工作日 / 周末: {stats['weekday_avg_tb']:.2f} / {stats['weekend_avg_tb']:.2f} TB/天")


def mode_validate(config: Dict, args) -> None:
    print("\n" + "=" * 72)
    print("模式: 验证 (Validate)")
    print("=" * 72 + "\n")

    log_file = args.log_file or str(Path(get_output_dir(config)) / "cdn_logs.db")
    validate_from_file(log_file, config)


def mode_dashboard(config: Dict, args) -> None:
    from fake_cdn.dashboard.app import run_dashboard

    run_dashboard(
        port=args.port or 8050,
        config_path=args.config,
        config_db_path=args.config_db,
    )


def mode_tenant_migrate(args) -> None:
    """导入主配置与日志库中的真实租户，并报告未映射租户。"""
    store = TenantConfigStore(args.config_db) if args.config_db else get_default_config_store()
    primary_config = Path(args.config).expanduser().resolve()
    root = primary_config.parent
    candidates = sorted(root.glob("config*.json"))
    log_tenants = set(get_default_storage().get_tenants())

    try:
        if args.tenant_id:
            allowed_tenants = {store.validate_tenant_id(args.tenant_id)}
        else:
            primary_payload = json.loads(primary_config.read_text(encoding="utf-8"))
            primary_tenant = store.validate(primary_payload)["dimensions"]["tenant_id"]
            allowed_tenants = log_tenants | {primary_tenant}
        imported = store.bootstrap(candidates, allowed_tenant_ids=allowed_tenants)
    except (OSError, json.JSONDecodeError, ConfigManagerError) as exc:
        print(f"[失败] {exc}")
        sys.exit(1)

    for item in imported:
        print(f"[导入] {item['tenant_id']}: v{item['version_no']}")

    print(f"[配置库] {store.db_path}")
    print(f"[扫描] {len(candidates)} 个配置文件，新导入 {len(imported)} 个租户")
    configured = {item["tenant_id"] for item in store.list_tenants()}
    if args.tenant_id and args.tenant_id not in configured:
        print(f"[失败] 找不到租户配置: {args.tenant_id}")
        sys.exit(1)

    missing = sorted(log_tenants - configured)
    if missing:
        print("[待映射] 以下历史日志 tenant_id 尚无配置，系统不会自动合并：")
        for tenant_id in missing:
            print(f"  - {tenant_id}")
    for item in store.list_tenants():
        print(
            f"  {item['tenant_id']} · active=v{item.get('active_version_no') or '—'} "
            f"· versions={item['version_count']}"
        )


def mode_migrate(config: Dict, args) -> None:
    import os

    print("\n" + "=" * 72)
    print("模式: 数据迁移 (Migrate JSONL → SQLite)")
    print("=" * 72 + "\n")

    if args.log_file:
        jsonl_path = Path(args.log_file)
    else:
        project_root = Path(__file__).parent.parent
        jsonl_path = project_root / "output" / "logs.jsonl"

    if not jsonl_path.exists():
        print(f"[错误] JSONL 文件不存在: {jsonl_path}")
        sys.exit(1)

    print(f"[源文件] {jsonl_path}")
    logs = []
    with open(jsonl_path, "r", encoding="utf-8") as file:
        for line in file:
            if line.strip():
                logs.append(json.loads(line))
    print(f"[读取] 共 {len(logs):,} 条记录")

    storage = get_default_storage()
    print(f"[目标] {storage.db_path}")
    if args.clear:
        storage.clear()
        print("[清空] 已清空现有数据")

    storage.insert_logs(logs)
    count = storage.get_record_count()
    print(f"\n[完成] SQLite 中共 {count:,} 条记录")

    if args.delete_source:
        os.remove(jsonl_path)
        print(f"[删除] 已删除源文件: {jsonl_path}")


def print_banner() -> None:
    print("""
    ███████╗ █████╗ ██╗  ██╗███████╗     ██████╗██████╗ ███╗   ██╗
    ██╔════╝██╔══██╗██║ ██╔╝██╔════╝    ██╔════╝██╔══██╗████╗  ██║
    █████╗  ███████║█████╔╝ █████╗      ██║     ██║  ██║██╔██╗ ██║
    ██╔══╝  ██╔══██║██╔═██╗ ██╔══╝      ██║     ██║  ██║██║╚██╗██║
    ██║     ██║  ██║██║  ██╗███████╗    ╚██████╗██████╔╝██║ ╚████║
    ╚═╝     ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝     ╚═════╝╚═════╝ ╚═╝  ╚═══╝

    CDN日志模拟系统 - 流量窗口推送版
    """)


def main() -> None:
    print_banner()

    parser = argparse.ArgumentParser(
        description="Fake CDN - 生成符合总流量窗口目标的模拟 CDN 日志",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
运行模式:
  simulation    一次性生成并可选推送整窗数据 (默认)
  realtime      按真实时间间隔推送当前窗口内时间点
  catchup       补推指定时间窗口数据
  validate      校验已生成日志
  dashboard     启动可视化仪表板
  migrate       将 JSONL 数据导入 SQLite
  tenant-migrate 将 config*.json 显式导入租户配置数据库

示例:
  python -m fake_cdn simulation
  python -m fake_cdn catchup --start-datetime 2026-03-11T00:00:00 --end-datetime 2026-04-10T23:55:00
  python -m fake_cdn realtime --once
  python -m fake_cdn validate --log-file output/cdn_logs.db
        """,
    )

    parser.add_argument(
        "mode",
        nargs="?",
        default="simulation",
        choices=[
            "simulation",
            "realtime",
            "catchup",
            "validate",
            "dashboard",
            "migrate",
            "tenant-migrate",
        ],
        help="运行模式",
    )
    parser.add_argument(
        "--config", default="./config.json", help="配置文件路径 (默认: ./config.json)"
    )
    parser.add_argument("--tenant-id", help="从配置数据库加载该租户的已发布版本")
    parser.add_argument("--config-db", help="租户配置 SQLite 路径（默认: output/config.db）")
    parser.add_argument("--once", action="store_true", help="实时模式下只执行一次")
    parser.add_argument(
        "--start-datetime", help="补推模式: 开始时间 (YYYY-MM-DD HH:MM:SS 或 YYYY-MM-DDTHH:MM:SS)"
    )
    parser.add_argument(
        "--end-datetime",
        help="补推/实时模式: 结束时间 (YYYY-MM-DD HH:MM:SS 或 YYYY-MM-DDTHH:MM:SS)",
    )
    parser.add_argument("--start-date", help="补推模式快捷参数: 开始日期 (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="补推模式快捷参数: 结束日期 (YYYY-MM-DD)")
    parser.add_argument("--log-file", help="验证模式: 日志文件路径 (支持 .db / .jsonl)")
    parser.add_argument("--dry-run", action="store_true", help="不真实推送到 API")
    parser.add_argument("--port", type=int, help="仪表板端口 (默认: 8050)")
    parser.add_argument("--clear", action="store_true", help="迁移模式: 清空现有 SQLite 数据")
    parser.add_argument("--delete-source", action="store_true", help="迁移模式: 删除源 JSONL 文件")
    parser.add_argument("-y", "--yes", action="store_true", help="跳过确认提示")

    args = parser.parse_args()

    if args.mode == "dashboard":
        mode_dashboard({}, args)
        return
    if args.mode == "migrate":
        mode_migrate({}, args)
        return
    if args.mode == "tenant-migrate":
        mode_tenant_migrate(args)
        return

    config, config_store, job_id = load_tenant_config(args)
    if args.dry_run:
        config = deepcopy(config)
        config["mode"]["dry_run"] = True

    print_config_summary(config)
    ensure_push_confirmed(config, args)

    try:
        if args.mode == "simulation":
            mode_simulation(config, args)
        elif args.mode == "realtime":
            mode_realtime(config, args)
        elif args.mode == "catchup":
            mode_catchup(config, args)
        elif args.mode == "validate":
            mode_validate(config, args)
        if config_store and job_id:
            config_store.finish_job(job_id, "succeeded")
    except KeyboardInterrupt:
        if config_store and job_id:
            config_store.finish_job(job_id, "cancelled")
        print("\n\n[中断] 用户中断执行")
        sys.exit(0)
    except Exception as exc:
        if config_store and job_id:
            config_store.finish_job(job_id, "failed", {"error": str(exc)})
        print(f"\n[错误] {exc}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
