#!/bin/bash
# Fake CDN 快速启动脚本

set -e

# 获取项目根目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

echo "================================"
echo "Fake CDN 快速启动"
echo "================================"
echo ""

# 检查Python
if ! command -v python3 &> /dev/null; then
    echo "错误: 未找到python3"
    exit 1
fi

echo "1. 检查依赖..."
pip3 install -q -r requirements.txt
echo "   ✓ 依赖已安装"

echo ""
echo "2. 生成模拟数据..."
python3 -m fake_cdn simulation

echo ""
echo "================================"
echo "完成!"
echo "================================"
echo ""
echo "输出文件:"
echo "  - output/logs.jsonl          (所有日志)"
echo "  - output/stats.json          (统计信息)"
echo "  - output/bandwidth_curve.csv (带宽曲线)"
echo ""
echo "下一步:"
echo "  # 查看验证报告 (已在上面输出)"
echo "  # 或手动验证"
echo "  python3 -m fake_cdn validate --log-file output/logs.jsonl"
echo ""
echo "  # 如果要真实推送(仅限测试环境!):"
echo "  # 1. 编辑 config.json, 设置 dry_run=false"
echo "  # 2. 重新运行: python3 -m fake_cdn simulation"
echo ""
