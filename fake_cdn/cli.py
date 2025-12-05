#!/usr/bin/env python3
"""
Fake CDN - CDN日志模拟系统 CLI

用途: 生成符合95计费策略的模拟CDN监控数据
"""

import sys
import json
import argparse

from fake_cdn.core.generator import CDNLogGenerator
from fake_cdn.core.pusher import LogPusher, LocalSaver
from fake_cdn.core.scheduler import RealtimeScheduler, CatchupScheduler
from fake_cdn.core.validator import Percentile95Validator, BillingCalculator, validate_from_file


def load_config(config_path: str = "./config.json") -> dict:
    """加载配置文件，支持环境变量覆盖"""
    import os

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
    except FileNotFoundError:
        print(f"[错误] 配置文件不存在: {config_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"[错误] 配置文件格式错误: {e}")
        sys.exit(1)

    # 环境变量覆盖 API 配置
    if os.environ.get("CDN_API_ENDPOINT"):
        config["api"]["endpoint"] = os.environ["CDN_API_ENDPOINT"]
        print(f"[环境变量] API endpoint: {config['api']['endpoint']}")

    if os.environ.get("CDN_API_VIP"):
        config["api"]["headers"]["vip"] = os.environ["CDN_API_VIP"]
        print(f"[环境变量] API vip: {config['api']['headers']['vip']}")

    return config


def mode_simulation(config: dict, args):
    """
    模拟模式: 一次性生成完整月度数据
    """
    print("\n" + "=" * 60)
    print("模式: 模拟生成 (Simulation)")
    print("=" * 60 + "\n")

    # 1. 生成数据
    generator = CDNLogGenerator(config)
    logs, stats = generator.generate_full_month()

    # 2. 保存到本地
    if config["mode"].get("save_local", True):
        output_dir = config["mode"]["output_dir"]
        LocalSaver.save_logs(logs, output_dir, "logs.jsonl")
        LocalSaver.save_stats(stats, output_dir, "stats.json")

        # 提取带宽曲线并保存
        bandwidth_curve = [log["bw"] / 1024 for log in logs if log["domain"] == config["dimensions"]["domains"][0]]
        LocalSaver.save_bandwidth_curve(bandwidth_curve, output_dir, "bandwidth_curve.csv")

    # 3. 验证
    print("\n[验证] 开始验证...")
    target_bw = config['target']['bandwidth_gbps']

    result = Percentile95Validator.validate_logs(logs, target_bw)
    Percentile95Validator.print_report(result)

    # 4. 计费报告
    bandwidth_curve = [log["bw"] / 1024 for log in logs]
    billing = BillingCalculator.calculate_95_billing(bandwidth_curve)
    BillingCalculator.print_billing_report(billing)

    # 5. 推送(可选)
    if not config["mode"].get("dry_run", True):
        print("\n[推送] 开始推送到API...")
        pusher = LogPusher(config)
        pusher.push_all(logs, dry_run=False)
    else:
        print("\n[跳过] dry_run=true, 不推送到API")

    print("\n[完成] 模拟生成完成!\n")


def mode_realtime(config: dict, args):
    """
    实时模式: 按时间间隔实时生成并推送
    """
    print("\n" + "=" * 60)
    print("模式: 实时推送 (Realtime)")
    print("=" * 60 + "\n")

    scheduler = RealtimeScheduler(config)
    dry_run = config["mode"].get("dry_run", True)

    if args.once:
        # 只执行一次
        scheduler.run_once(dry_run)
    else:
        # 持续运行
        scheduler.run_forever(dry_run)


def mode_catchup(config: dict, args):
    """
    补推模式: 补推历史数据
    """
    print("\n" + "=" * 60)
    print("模式: 补推历史数据 (Catchup)")
    print("=" * 60 + "\n")

    if not args.start_date or not args.end_date:
        print("[错误] 补推模式需要指定 --start-date 和 --end-date")
        sys.exit(1)

    scheduler = CatchupScheduler(config, args.start_date, args.end_date)
    dry_run = config["mode"].get("dry_run", True)

    stats = scheduler.run(dry_run)

    print("\n[完成] 补推完成!")
    print(f"  总流量: {stats['total_flux_tb']:.2f} TB")
    print(f"  95分位: {stats['p95_gbps']:.2f} Gbps")


def mode_validate(config: dict, args):
    """
    验证模式: 验证已生成的日志
    """
    print("\n" + "=" * 60)
    print("模式: 验证 (Validate)")
    print("=" * 60 + "\n")

    if not args.log_file:
        print("[错误] 验证模式需要指定 --log-file")
        sys.exit(1)

    target_bw = config['target']['bandwidth_gbps']
    validate_from_file(args.log_file, target_bw)


def mode_dashboard(config: dict, args):
    """
    仪表板模式: 启动可视化仪表板
    """
    from fake_cdn.dashboard.app import run_dashboard
    run_dashboard(port=args.port or 8050)


def mode_migrate(config: dict, args):
    """
    迁移模式: 将 JSONL 数据导入 SQLite
    """
    import os
    from pathlib import Path
    from fake_cdn.core.storage import get_default_storage

    print("\n" + "=" * 60)
    print("模式: 数据迁移 (Migrate JSONL → SQLite)")
    print("=" * 60 + "\n")

    # 确定 JSONL 文件路径
    if args.log_file:
        jsonl_path = Path(args.log_file)
    else:
        project_root = Path(__file__).parent.parent
        jsonl_path = project_root / "output" / "logs.jsonl"

    if not jsonl_path.exists():
        print(f"[错误] JSONL 文件不存在: {jsonl_path}")
        sys.exit(1)

    print(f"[源文件] {jsonl_path}")

    # 读取 JSONL
    logs = []
    with open(jsonl_path, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip():
                logs.append(json.loads(line))

    print(f"[读取] 共 {len(logs):,} 条记录")

    # 获取 SQLite 存储
    storage = get_default_storage()
    print(f"[目标] {storage.db_path}")

    # 清空现有数据（可选）
    if args.clear:
        storage.clear()
        print("[清空] 已清空现有数据")

    # 导入数据
    storage.insert_logs(logs)

    # 验证
    count = storage.get_record_count()
    print(f"\n[完成] SQLite 中共 {count:,} 条记录")

    # 可选：删除原 JSONL 文件
    if args.delete_source:
        os.remove(jsonl_path)
        print(f"[删除] 已删除源文件: {jsonl_path}")


def print_banner():
    """打印 ASCII Art Banner"""
    print("""
    ███████╗ █████╗ ██╗  ██╗███████╗     ██████╗██████╗ ███╗   ██╗
    ██╔════╝██╔══██╗██║ ██╔╝██╔════╝    ██╔════╝██╔══██╗████╗  ██║
    █████╗  ███████║█████╔╝ █████╗      ██║     ██║  ██║██╔██╗ ██║
    ██╔══╝  ██╔══██║██╔═██╗ ██╔══╝      ██║     ██║  ██║██║╚██╗██║
    ██║     ██║  ██║██║  ██╗███████╗    ╚██████╗██████╔╝██║ ╚████║
    ╚═╝     ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝     ╚═════╝╚═════╝ ╚═╝  ╚═══╝

    CDN日志模拟系统 - 95计费专用
    """)


def main():
    """主函数"""
    print_banner()

    # 命令行参数
    parser = argparse.ArgumentParser(
        description="Fake CDN - 生成符合95计费的模拟CDN日志",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
运行模式:
  simulation    一次性生成完整月度数据 (默认)
  realtime      按时间间隔实时生成并推送
  catchup       补推历史数据
  validate      验证已生成的日志
  dashboard     启动可视化仪表板
  migrate       将 JSONL 数据迁移到 SQLite

示例:
  # 模拟生成30天数据
  python -m fake_cdn simulation

  # 实时推送(持续运行)
  python -m fake_cdn realtime

  # 实时推送(只执行一次)
  python -m fake_cdn realtime --once

  # 补推历史数据
  python -m fake_cdn catchup --start-date 2025-01-01 --end-date 2025-01-31

  # 验证日志文件
  python -m fake_cdn validate --log-file output/logs.jsonl

  # 启动仪表板
  python -m fake_cdn dashboard

  # 迁移 JSONL 到 SQLite
  python -m fake_cdn migrate --log-file output/logs.jsonl

警告: 仅用于测试环境! 禁止向生产系统推送假数据!
        """
    )

    parser.add_argument(
        "mode",
        nargs="?",
        default="simulation",
        choices=["simulation", "realtime", "catchup", "validate", "dashboard", "migrate"],
        help="运行模式"
    )

    parser.add_argument(
        "--config",
        default="./config.json",
        help="配置文件路径 (默认: ./config.json)"
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="实时模式下只执行一次"
    )

    parser.add_argument(
        "--start-date",
        help="补推模式: 开始日期 (YYYY-MM-DD)"
    )

    parser.add_argument(
        "--end-date",
        help="补推模式: 结束日期 (YYYY-MM-DD)"
    )

    parser.add_argument(
        "--log-file",
        help="验证模式: 日志文件路径"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="不真实推送到API"
    )

    parser.add_argument(
        "--port",
        type=int,
        help="仪表板端口 (默认: 8050)"
    )

    # migrate 专用参数
    parser.add_argument(
        "--clear",
        action="store_true",
        help="迁移模式: 清空现有 SQLite 数据"
    )

    parser.add_argument(
        "--delete-source",
        action="store_true",
        help="迁移模式: 迁移后删除源 JSONL 文件"
    )

    parser.add_argument(
        "-y", "--yes",
        action="store_true",
        help="跳过确认提示 (用于非交互式运行)"
    )

    args = parser.parse_args()

    # 仪表板和迁移模式不需要加载配置
    if args.mode == "dashboard":
        mode_dashboard(None, args)
        return

    if args.mode == "migrate":
        mode_migrate(None, args)
        return

    # 加载配置
    config = load_config(args.config)

    # 命令行参数覆盖配置
    if args.dry_run:
        config["mode"]["dry_run"] = True

    # 打印配置摘要
    target_bw = config['target']['bandwidth_gbps']
    daily_tb = target_bw * 10.54 / 1024  # Gbps -> TB/天

    print(f"[配置] 目标平均带宽: {target_bw} Gbps")
    print(f"[配置] 预计流量: {daily_tb:.2f} TB/天, {daily_tb * config['time']['duration_days']:.2f} TB/{config['time']['duration_days']}天")
    print(f"[配置] 时间粒度: {config['time']['interval_seconds']} 秒")
    print(f"[配置] 持续天数: {config['time']['duration_days']} 天")
    print(f"[配置] Dry-Run: {config['mode']['dry_run']}")
    print()

    # 安全警告
    if not config["mode"]["dry_run"]:
        # 检查 API 配置
        if not config["api"]["endpoint"]:
            print("[错误] 未配置 API endpoint")
            print("请设置环境变量: export CDN_API_ENDPOINT=<your_endpoint>")
            sys.exit(1)
        if not config["api"]["headers"].get("vip"):
            print("[错误] 未配置 API vip")
            print("请设置环境变量: export CDN_API_VIP=<your_vip>")
            sys.exit(1)

        print("⚠️  警告: Dry-Run已关闭, 将真实推送数据到API!")
        print(f"⚠️  目标API: {config['api']['endpoint']}")

        # 检查是否需要交互确认
        if args.yes:
            print("[跳过确认] 使用 -y/--yes 参数")
        elif sys.stdin.isatty():
            response = input("确认继续? (yes/no): ")
            if response.lower() != "yes":
                print("已取消")
                sys.exit(0)
        else:
            print("[错误] 非交互模式下需要 -y/--yes 参数确认")
            sys.exit(1)

    # 路由到对应模式
    try:
        if args.mode == "simulation":
            mode_simulation(config, args)
        elif args.mode == "realtime":
            mode_realtime(config, args)
        elif args.mode == "catchup":
            mode_catchup(config, args)
        elif args.mode == "validate":
            mode_validate(config, args)
    except KeyboardInterrupt:
        print("\n\n[中断] 用户中断执行")
        sys.exit(0)
    except Exception as e:
        print(f"\n[错误] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
