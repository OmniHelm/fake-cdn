#!/bin/bash
# Fake CDN 一键部署脚本
# 用途: 快速部署和启动 CDN 日志模拟系统

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# 项目目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

# 打印带颜色的消息
info() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# 分隔线
separator() {
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
}

# 打印 banner
print_banner() {
    echo ""
    echo -e "${BLUE}"
    echo "  ███████╗ █████╗ ██╗  ██╗███████╗     ██████╗██████╗ ███╗   ██╗"
    echo "  ██╔════╝██╔══██╗██║ ██╔╝██╔════╝    ██╔════╝██╔══██╗████╗  ██║"
    echo "  █████╗  ███████║█████╔╝ █████╗      ██║     ██║  ██║██╔██╗ ██║"
    echo "  ██╔══╝  ██╔══██║██╔═██╗ ██╔══╝      ██║     ██║  ██║██║╚██╗██║"
    echo "  ██║     ██║  ██║██║  ██╗███████╗    ╚██████╗██████╔╝██║ ╚████║"
    echo "  ╚═╝     ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝     ╚═════╝╚═════╝ ╚═╝  ╚═══╝"
    echo -e "${NC}"
    echo "  CDN 日志模拟系统 - 一键部署脚本"
    echo ""
}

# 显示帮助
show_help() {
    echo "用法: $0 [选项] [模式]"
    echo ""
    echo "模式:"
    echo "  simulation    一次性生成完整月度数据"
    echo "  realtime      按真实时间推送日志"
    echo "  catchup       快速补推历史数据"
    echo "  validate      验证已生成的日志"
    echo "  dashboard     启动可视化仪表板"
    echo "  status        查看数据状态"
    echo ""
    echo "选项:"
    echo "  -h, --help    显示帮助信息"
    echo "  --skip-deps   跳过依赖安装"
    echo ""
    echo "环境变量 (真实推送时需要):"
    echo "  CDN_API_ENDPOINT  API 端点地址"
    echo "  CDN_API_VIP       API VIP 标识"
    echo ""
    echo "示例:"
    echo "  $0                    # 交互式菜单"
    echo "  $0 simulation         # 直接运行模拟模式"
    echo "  $0 dashboard          # 启动仪表板"
    echo "  $0 --skip-deps status # 跳过依赖安装，查看状态"
    exit 0
}

# 检查 Python 环境
check_python() {
    info "检查 Python 环境..."

    if command -v python3 &> /dev/null; then
        PYTHON_VERSION=$(python3 --version 2>&1 | cut -d' ' -f2)
        success "Python3 已安装: $PYTHON_VERSION"
    else
        error "未找到 python3，请先安装 Python 3.8+"
    fi

    # 检查版本号 >= 3.8
    MAJOR=$(echo "$PYTHON_VERSION" | cut -d'.' -f1)
    MINOR=$(echo "$PYTHON_VERSION" | cut -d'.' -f2)
    if [ "$MAJOR" -lt 3 ] || ([ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 8 ]); then
        error "Python 版本过低，需要 3.8+，当前: $PYTHON_VERSION"
    fi
}

# 创建/激活虚拟环境
setup_venv() {
    info "配置虚拟环境..."

    if [ ! -d "venv" ]; then
        info "创建虚拟环境..."
        python3 -m venv venv
        success "虚拟环境已创建"
    else
        success "虚拟环境已存在"
    fi

    # 激活虚拟环境
    source venv/bin/activate
    success "虚拟环境已激活"
}

# 安装依赖
install_deps() {
    info "安装依赖包..."

    pip install --upgrade pip -q
    pip install -r requirements.txt -q

    # 以开发模式安装项目
    pip install -e . -q

    success "依赖安装完成"
}

# 检查配置文件
check_config() {
    info "检查配置文件..."

    if [ ! -f "config.json" ]; then
        error "配置文件 config.json 不存在"
    fi

    # 验证 JSON 格式
    if python3 -c "import json; json.load(open('config.json'))" 2>/dev/null; then
        success "配置文件格式正确"
    else
        error "配置文件 JSON 格式错误"
    fi
}

# 创建输出目录
setup_output() {
    info "准备输出目录..."
    mkdir -p output
    success "输出目录已就绪"
}

# 检查推送配置
check_push_config() {
    echo ""
    echo -e "${CYAN}推送配置:${NC}"

    # 读取 dry_run 状态
    DRY_RUN=$(python3 -c "import json; print(json.load(open('config.json'))['mode']['dry_run'])" 2>/dev/null)
    if [ "$DRY_RUN" = "True" ]; then
        echo -e "  dry_run: ${GREEN}true${NC} (不会实际推送)"
    else
        echo -e "  dry_run: ${RED}false${NC} (会实际推送!)"
    fi

    # 检查环境变量
    if [ -n "$CDN_API_ENDPOINT" ]; then
        echo -e "  CDN_API_ENDPOINT: ${GREEN}已配置${NC}"
    else
        echo -e "  CDN_API_ENDPOINT: ${YELLOW}未配置${NC}"
    fi
    if [ -n "$CDN_API_VIP" ]; then
        echo -e "  CDN_API_VIP: ${GREEN}已配置${NC}"
    else
        echo -e "  CDN_API_VIP: ${YELLOW}未配置${NC}"
    fi
}

# 显示数据状态
show_status() {
    echo ""
    echo -e "${CYAN}数据状态:${NC}"

    if [ -f "output/cdn_logs.db" ]; then
        # 使用 Python 查询 SQLite
        python3 -c "
from fake_cdn.core.storage import get_default_storage
from datetime import datetime
storage = get_default_storage()
count = storage.get_record_count()
if count == 0:
    print('  数据库: 空 (请运行 simulation 生成数据)')
else:
    min_time, max_time = storage.get_time_range()
    domains = len(storage.get_domains())
    print(f'  数据库: output/cdn_logs.db')
    print(f'  记录数: {count:,} 条')
    if min_time and max_time:
        print(f'  时间范围: {datetime.fromtimestamp(min_time/1000).strftime(\"%Y-%m-%d\")} - {datetime.fromtimestamp(max_time/1000).strftime(\"%Y-%m-%d\")}')
    print(f'  域名数: {domains} 个')
" 2>/dev/null || echo "  数据库: 读取失败"
    else
        echo -e "  数据库: ${YELLOW}未创建${NC}"
        echo "  提示: 运行 simulation 生成测试数据"
    fi
}

# 显示菜单
show_menu() {
    separator
    echo -e "${GREEN}部署完成! 请选择运行模式:${NC}"
    echo ""
    echo "  1) simulation  - 模拟模式 (生成测试数据)"
    echo "  2) realtime    - 实时模式 (按真实时间推送)"
    echo "  3) catchup     - 追赶模式 (补推历史数据)"
    echo "  4) validate    - 验证模式 (验证生成的日志)"
    echo "  5) dashboard   - 启动仪表板 (可视化监控)"
    echo "  6) status      - 查看状态"
    echo "  0) exit        - 退出"
    echo ""
}

# 运行选择的模式
run_mode() {
    local choice=$1

    case $choice in
        1|simulation)
            info "启动模拟模式..."
            python3 -m fake_cdn simulation
            ;;
        2|realtime)
            info "启动实时模式..."
            python3 -m fake_cdn realtime
            ;;
        3|catchup)
            info "启动追赶模式..."
            read -p "开始日期 (YYYY-MM-DD): " start_date
            read -p "结束日期 (YYYY-MM-DD): " end_date
            python3 -m fake_cdn catchup --start-date "$start_date" --end-date "$end_date"
            ;;
        4|validate)
            info "启动验证模式..."
            if [ -f "output/cdn_logs.db" ]; then
                # 从 SQLite 导出验证
                python3 -c "
from fake_cdn.core.storage import get_default_storage
from fake_cdn.core.validator import Percentile95Validator
import json

storage = get_default_storage()
logs = storage.query_logs()
config = json.load(open('config.json'))
target_bw = config['target']['bandwidth_gbps']

result = Percentile95Validator.validate_logs(logs, target_bw)
Percentile95Validator.print_report(result)
"
            else
                warn "未找到数据库，请先运行模拟模式生成数据"
            fi
            ;;
        5|dashboard)
            info "启动仪表板..."
            info "访问地址: http://localhost:8050"
            python3 -m fake_cdn dashboard
            ;;
        6|status)
            show_status
            check_push_config
            ;;
        0|exit)
            info "退出"
            exit 0
            ;;
        *)
            warn "无效选项: $choice"
            return 1
            ;;
    esac
}

# 主函数
main() {
    # 解析参数
    SKIP_DEPS=false
    MODE=""

    while [[ $# -gt 0 ]]; do
        case $1 in
            -h|--help)
                show_help
                ;;
            --skip-deps)
                SKIP_DEPS=true
                shift
                ;;
            *)
                MODE="$1"
                shift
                ;;
        esac
    done

    print_banner

    # 部署步骤
    separator
    echo -e "${YELLOW}[1/5]${NC} 环境检查"
    check_python

    separator
    echo -e "${YELLOW}[2/5]${NC} 虚拟环境"
    setup_venv

    if [ "$SKIP_DEPS" = false ]; then
        separator
        echo -e "${YELLOW}[3/5]${NC} 依赖安装"
        install_deps
    else
        separator
        echo -e "${YELLOW}[3/5]${NC} 依赖安装 (跳过)"
    fi

    separator
    echo -e "${YELLOW}[4/5]${NC} 配置检查"
    check_config

    separator
    echo -e "${YELLOW}[5/5]${NC} 目录准备"
    setup_output

    # 显示状态
    show_status
    check_push_config

    # 如果有命令行参数，直接运行
    if [ -n "$MODE" ]; then
        separator
        run_mode "$MODE"
        exit 0
    fi

    # 交互式菜单
    while true; do
        show_menu
        read -p "请选择 [0-6]: " choice
        echo ""
        run_mode "$choice" || continue

        echo ""
        read -p "按回车继续..." _
    done
}

# 运行
main "$@"
