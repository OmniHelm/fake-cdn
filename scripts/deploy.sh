#!/bin/bash
# Fake CDN 一键部署脚本
# 用法:
#   一键安装: curl -fsSL https://raw.githubusercontent.com/OmniHelm/fake-cdn/main/scripts/deploy.sh | bash
#   本地运行: ./scripts/deploy.sh [模式]

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# 打印带颜色的消息
info() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# 仓库地址
REPO_URL="https://github.com/OmniHelm/fake-cdn.git"
# 固定安装到 /opt/fake-cdn，可通过 FAKE_CDN_DIR 环境变量覆盖
INSTALL_DIR="${FAKE_CDN_DIR:-/opt/fake-cdn}"

# 判断是否通过管道运行 (curl | bash)
if [ -z "${BASH_SOURCE[0]}" ] || [ "${BASH_SOURCE[0]}" = "bash" ]; then
    # 通过管道运行，需要先克隆项目然后执行本地脚本
    REMOTE_INSTALL=true
else
    # 本地文件运行
    REMOTE_INSTALL=false
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
fi

# 远程安装: 克隆项目并执行本地脚本
remote_install() {
    echo ""
    info "检查 git..."
    if ! command -v git &> /dev/null; then
        error "未找到 git，请先安装 git"
    fi
    success "git 已安装"

    if [ -d "$INSTALL_DIR" ]; then
        info "目录已存在，更新仓库..."
        cd "$INSTALL_DIR"
        git pull -q 2>/dev/null || warn "更新失败，使用现有代码"
    else
        info "克隆仓库到 $INSTALL_DIR ..."
        git clone -q "$REPO_URL" "$INSTALL_DIR"
        success "克隆完成"
        cd "$INSTALL_DIR"
    fi

    echo ""
    success "正在启动本地部署脚本..."
    echo ""

    # 执行本地脚本，显式重定向 stdin 到终端以支持交互
    bash ./scripts/deploy.sh "$@" < /dev/tty
}

# 如果是远程安装，克隆后执行本地脚本
if [ "$REMOTE_INSTALL" = true ]; then
    remote_install "$@"
    exit $?
fi

cd "$PROJECT_ROOT"

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
    echo "  worker        启动管理员任务队列 Worker"
    echo "  status        查看数据状态"
    echo "  full          完整模式 (dashboard + worker 后台启动)"
    echo "  stop          停止后台服务"
    echo "  config        配置 API 推送"
    echo ""
    echo "选项:"
    echo "  -h, --help    显示帮助信息"
    echo "  --skip-deps   跳过依赖安装"
    echo "  --tenant-id ID  使用配置数据库中该租户的已发布版本"
    echo "  --config-db PATH 租户配置数据库路径 (默认: output/config.db)"
    echo ""
    echo "环境变量 (真实推送时需要):"
    echo "  CDN_API_ENDPOINT  API 端点地址"
    echo "  CDN_API_VIP       API VIP 标识"
    echo ""
    echo "示例:"
    echo "  $0                    # 交互式菜单"
    echo "  $0 simulation         # 直接运行模拟模式"
    echo "  $0 dashboard          # 启动仪表板"
    echo "  $0 full                # 后台启动 dashboard + worker"
    echo "  $0 stop               # 停止后台服务"
    echo "  $0 --skip-deps status # 跳过依赖安装，查看状态"
    exit 0
}

require_tenant_id() {
    if [ -z "$TENANT_ID" ]; then
        error "realtime 模式必须通过 --tenant-id 指定租户"
    fi
}

read_realtime_end_datetime() {
    python3 -c '
import sys
from fake_cdn.core.tenant_config import TenantConfigStore

config = TenantConfigStore(sys.argv[2]).resolve_active(sys.argv[1])["config"]
print(config["time"]["end_datetime"])
' "$TENANT_ID" "$CONFIG_DB"
}

ensure_tenant_config() {
    require_tenant_id
    python3 -m fake_cdn tenant-migrate \
        --tenant-id "$TENANT_ID" \
        --config "$PROJECT_ROOT/config.json" \
        --config-db "$CONFIG_DB"
}

read_effective_dry_run() {
    if [ -n "$TENANT_ID" ]; then
        python3 -c '
import sys
from fake_cdn.core.tenant_config import TenantConfigStore

config = TenantConfigStore(sys.argv[2]).resolve_active(sys.argv[1])["config"]
print(config["mode"]["dry_run"])
' "$TENANT_ID" "$CONFIG_DB"
    else
        python3 -c "import json; print(json.load(open('config.json'))['mode']['dry_run'])"
    fi
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

    # 检查 venv 是否完整（存在 activate 脚本）
    if [ ! -f "venv/bin/activate" ]; then
        # 清理可能存在的不完整 venv 目录
        if [ -d "venv" ]; then
            warn "发现不完整的虚拟环境，正在清理..."
            rm -rf venv
        fi

        info "创建虚拟环境..."

        # 尝试创建虚拟环境
        if ! python3 -m venv venv 2>/dev/null; then
            echo ""
            warn "创建虚拟环境失败，可能缺少 python3-venv 包"
            echo ""

            # 检测系统类型并给出安装提示
            if [ -f /etc/debian_version ]; then
                PYTHON_VERSION=$(python3 --version 2>&1 | grep -oP '\d+\.\d+')
                echo -e "请运行以下命令安装:"
                echo -e "  ${YELLOW}sudo apt install python${PYTHON_VERSION}-venv${NC}"
                echo ""
            elif [ -f /etc/redhat-release ]; then
                echo -e "请运行以下命令安装:"
                echo -e "  ${YELLOW}sudo yum install python3-virtualenv${NC}"
                echo ""
            fi

            error "请安装 python3-venv 后重新运行此脚本"
        fi

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
    DRY_RUN=$(read_effective_dry_run 2>/dev/null)
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
load_managed_pids() {
    DASHBOARD_PID=""
    WORKER_PID=""
    local pid_file="$PROJECT_ROOT/.fake-cdn.pid"
    if [ ! -f "$pid_file" ]; then
        return 0
    fi

    # 兼容旧脚本只写入一个 Dashboard PID 的格式，同时拒绝执行文件内容。
    local first_line key value
    first_line=$(head -n 1 "$pid_file" 2>/dev/null || true)
    if [[ "$first_line" =~ ^[0-9]+$ ]]; then
        DASHBOARD_PID="$first_line"
        return 0
    fi
    while IFS='=' read -r key value; do
        case "$key" in
            DASHBOARD_PID|WORKER_PID)
                if [[ "$value" =~ ^[0-9]+$ ]]; then
                    if [ "$key" = "DASHBOARD_PID" ]; then
                        DASHBOARD_PID="$value"
                    else
                        WORKER_PID="$value"
                    fi
                fi
                ;;
        esac
    done < "$pid_file"
}

is_managed_process() {
    local pid="$1"
    local role="$2"
    local command_line
    if ! [[ "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" 2>/dev/null; then
        return 1
    fi
    command_line=$(ps -p "$pid" -o command= 2>/dev/null || true)
    if [ "$role" = "dashboard" ]; then
        [[ "$command_line" == *"fake_cdn dashboard"* || \
           "$command_line" == *"fake_cdn.dashboard.wsgi"* ]]
        return $?
    fi
    [[ "$command_line" == *"fake_cdn worker"* ]]
}

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

    echo ""
    echo -e "${CYAN}服务状态:${NC}"
    load_managed_pids
    if is_managed_process "${DASHBOARD_PID:-}" dashboard; then
        echo -e "  Dashboard: ${GREEN}运行中${NC} (PID: $DASHBOARD_PID)"
    else
        echo -e "  Dashboard: ${YELLOW}未运行${NC}"
    fi
    if is_managed_process "${WORKER_PID:-}" worker; then
        echo -e "  Worker: ${GREEN}运行中${NC} (PID: $WORKER_PID)"
    else
        echo -e "  Worker: ${YELLOW}未运行${NC}"
    fi
}

stop_managed_services() {
    local pid_file="$PROJECT_ROOT/.fake-cdn.pid"
    if [ ! -f "$pid_file" ]; then
        return
    fi
    load_managed_pids
    local managed_pids=()
    if is_managed_process "${WORKER_PID:-}" worker; then
        kill "$WORKER_PID" 2>/dev/null || true
        managed_pids+=("$WORKER_PID")
    elif [ -n "${WORKER_PID:-}" ]; then
        warn "跳过无效 Worker PID: $WORKER_PID"
    fi
    if is_managed_process "${DASHBOARD_PID:-}" dashboard; then
        kill "$DASHBOARD_PID" 2>/dev/null || true
        managed_pids+=("$DASHBOARD_PID")
    elif [ -n "${DASHBOARD_PID:-}" ]; then
        warn "跳过无效 Dashboard PID: $DASHBOARD_PID"
    fi
    for _ in $(seq 1 300); do
        local running=false
        for pid in "${managed_pids[@]}"; do
            if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
                running=true
            fi
        done
        if [ "$running" = false ]; then
            break
        fi
        sleep 0.1
    done
    rm -f "$pid_file"
}

port_8050_in_use() {
    if command -v lsof >/dev/null 2>&1; then
        lsof -ti:8050 >/dev/null 2>&1
        return $?
    fi
    if command -v fuser >/dev/null 2>&1; then
        fuser -n tcp 8050 >/dev/null 2>&1
        return $?
    fi
    if command -v ss >/dev/null 2>&1; then
        ss -ltn | awk 'NR > 1 && $4 ~ /(^|:)8050$/ { found = 1 } END { exit(found ? 0 : 1) }'
        return $?
    fi
    return 1
}

# 首次运行向导
first_run_wizard() {
    separator
    echo -e "${CYAN}━━━━━━━━━━━━ 初始化向导 ━━━━━━━━━━━━${NC}"
    echo ""

    # 1. 检查数据库状态
    if [ ! -f "output/cdn_logs.db" ]; then
        echo -e "${YELLOW}[数据库]${NC} 未检测到模拟数据"
        echo ""
        echo -n "是否现在生成模拟数据? (Y/n): "
        read -r gen_data
        if [[ ! "$gen_data" =~ ^[Nn]$ ]]; then
            echo ""
            run_mode simulation
            echo ""
            read -p "按回车继续..." _
        fi
    else
        echo -e "${GREEN}[数据库]${NC} 已存在模拟数据"
        show_status
    fi

    separator

    # 2. 检查 API 配置
    DRY_RUN=$(read_effective_dry_run 2>/dev/null)

    echo -e "${YELLOW}[推送配置]${NC}"
    echo ""

    if [ "$DRY_RUN" = "True" ]; then
        echo -e "  当前模式: ${GREEN}dry_run (模拟推送，不发送真实请求)${NC}"
        echo ""
        echo -n "是否配置真实 API 推送? (y/N): "
        read -r config_api
        if [[ "$config_api" =~ ^[Yy]$ ]]; then
            configure_api
        fi
    else
        echo -e "  当前模式: ${RED}真实推送${NC}"
        check_push_config
    fi

    separator
}

# 加载 .env 文件
load_env() {
    ENV_FILE="$PROJECT_ROOT/.env"
    if [ -f "$ENV_FILE" ]; then
        source "$ENV_FILE"
    fi
}

# 保存环境变量到 .env
save_env() {
    ENV_FILE="$PROJECT_ROOT/.env"
    CDN_API_ENDPOINT="${CDN_API_ENDPOINT:-}" CDN_API_VIP="${CDN_API_VIP:-}" \
        python3 - "$ENV_FILE" <<'PY'
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
pattern = re.compile(r"^\s*(?:export\s+)?(?:CDN_API_ENDPOINT|CDN_API_VIP)\s*=")
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
preserved = [
    line
    for line in lines
    if not pattern.match(line) and line.strip() != "# Fake CDN API 环境变量"
]
if preserved and preserved[-1]:
    preserved.append("")
preserved.extend(
    [
        "# Fake CDN API 环境变量",
        f"export CDN_API_ENDPOINT={shlex.quote(os.environ['CDN_API_ENDPOINT'])}",
        f"export CDN_API_VIP={shlex.quote(os.environ['CDN_API_VIP'])}",
    ]
)

path.parent.mkdir(parents=True, exist_ok=True)
fd, temporary_path = tempfile.mkstemp(prefix=".env.", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write("\n".join(preserved).rstrip() + "\n")
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, path)
except Exception:
    try:
        os.unlink(temporary_path)
    except FileNotFoundError:
        pass
    raise
PY
}

# 配置 API
configure_api() {
    echo ""
    echo -e "${CYAN}配置 API 推送${NC}"
    echo ""

    ENV_FILE="$PROJECT_ROOT/.env"

    # 加载已有配置
    load_env

    # API Endpoint
    current_endpoint="未配置"
    if [ -n "${CDN_API_ENDPOINT:-}" ]; then
        current_endpoint="已配置"
    fi
    echo -e "当前 Endpoint: ${YELLOW}$current_endpoint${NC}"
    echo -n "API Endpoint (留空保持不变): "
    read -r api_endpoint
    if [ -n "$api_endpoint" ]; then
        export CDN_API_ENDPOINT="$api_endpoint"
        success "CDN_API_ENDPOINT 已设置"
    fi

    # API VIP
    current_vip="未配置"
    if [ -n "${CDN_API_VIP:-}" ]; then
        current_vip="已配置"
    fi
    echo -e "当前 VIP: ${YELLOW}$current_vip${NC}"
    echo -n "API VIP (留空保持不变): "
    read -r api_vip
    if [ -n "$api_vip" ]; then
        export CDN_API_VIP="$api_vip"
        success "CDN_API_VIP 已设置"
    fi

    # 保存到 .env 文件
    if [ -n "$CDN_API_ENDPOINT" ] || [ -n "$CDN_API_VIP" ]; then
        save_env
        success "配置已保存到 $ENV_FILE"
    fi

    # 是否关闭 dry_run
    if [ -n "$CDN_API_ENDPOINT" ] && [ -n "$CDN_API_VIP" ]; then
        echo ""
        echo -n "是否关闭 dry_run 模式开始真实推送? (y/N): "
        read -r disable_dry
        if [[ "$disable_dry" =~ ^[Yy]$ ]]; then
            python3 -c "
import json
with open('config.json', 'r') as f:
    config = json.load(f)
config['mode']['dry_run'] = False
with open('config.json', 'w') as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
"
            success "dry_run 已关闭，将进行真实推送"
            warn "请确保 API 指向测试环境!"
        fi
    fi
}

# 显示菜单
show_menu() {
    separator
    echo -e "${GREEN}请选择运行模式:${NC}"
    echo ""
    echo "  1) simulation  - 模拟模式 (生成测试数据)"
    echo "  2) realtime    - 实时模式 (按真实时间推送)"
    echo "  3) catchup     - 追赶模式 (补推历史数据)"
    echo "  4) validate    - 验证模式 (验证生成的日志)"
    echo "  5) dashboard   - 启动仪表板 (可视化监控)"
    echo "  6) worker      - 启动管理员任务队列 Worker"
    echo "  7) status      - 查看状态"
    echo -e "  ${CYAN}8) full         - 完整模式 (dashboard + worker 后台启动)${NC}"
    echo -e "  ${RED}9) stop         - 停止后台服务${NC}"
    echo -e "  ${YELLOW}10) config      - 配置 API 推送${NC}"
    echo "  0) exit        - 退出"
    echo ""
}

# 运行选择的模式
run_mode() {
    local choice=$1

    case $choice in
        1|simulation)
            info "启动模拟模式..."
            if [ -n "$TENANT_ID" ]; then
                ensure_tenant_config
            fi
            python3 -m fake_cdn simulation "${TENANT_ARGS[@]}"
            ;;
        2|realtime)
            info "启动实时模式..."
            ensure_tenant_config
            realtime_end=$(read_realtime_end_datetime)
            python3 -u -m fake_cdn realtime "${TENANT_ARGS[@]}" \
                --end-datetime "$realtime_end"
            ;;
        3|catchup)
            info "启动追赶模式..."
            read -p "开始日期 (YYYY-MM-DD): " start_date
            read -p "结束日期 (YYYY-MM-DD): " end_date
            if [ -n "$TENANT_ID" ]; then
                ensure_tenant_config
            fi
            python3 -m fake_cdn catchup "${TENANT_ARGS[@]}" \
                --start-date "$start_date" --end-date "$end_date"
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
            load_env
            python3 -m fake_cdn dashboard \
                --config "$PROJECT_ROOT/config.json" --config-db "$CONFIG_DB"
            ;;
        6|worker)
            info "启动任务 Worker..."
            load_env
            python3 -u -m fake_cdn worker --config-db "$CONFIG_DB"
            ;;
        7|status)
            show_status
            check_push_config
            ;;
        8|full)
            info "启动完整模式 (dashboard + worker)..."
            echo ""
            load_env
            if { [ -n "${DASHBOARD_PASSWORD:-}" ] || [ -n "${DASHBOARD_USERS:-}" ] || \
                 [ -n "${DASHBOARD_TENANT_USERS:-}" ]; } && \
               [ -z "${DASHBOARD_SECRET_KEY:-}" ]; then
                error "启用 Dashboard 登录时必须设置稳定的 DASHBOARD_SECRET_KEY"
            fi
            if ! command -v gunicorn >/dev/null 2>&1; then
                error "未安装 gunicorn，请重新运行且不要使用 --skip-deps"
            fi
            PID_FILE="$PROJECT_ROOT/.fake-cdn.pid"
            stop_managed_services
            if port_8050_in_use; then
                error "端口 8050 已被非托管进程占用，请先确认该进程后再启动"
            fi
            info "迁移租户配置与任务队列表..."
            python3 -m fake_cdn tenant-migrate \
                --config "$PROJECT_ROOT/config.json" --config-db "$CONFIG_DB"

            export FAKE_CDN_CONFIG_PATH="$PROJECT_ROOT/config.json"
            export FAKE_CDN_CONFIG_DB_PATH="$CONFIG_DB"
            export FAKE_CDN_DB_PATH="${FAKE_CDN_DB_PATH:-$PROJECT_ROOT/output/cdn_logs.db}"
            export FAKE_CDN_PROJECT_ROOT="$PROJECT_ROOT"
            info "后台启动仪表板 (端口 8050)..."
            nohup gunicorn --bind 0.0.0.0:8050 --workers 2 --threads 2 \
                --timeout 120 --preload fake_cdn.dashboard.wsgi:application \
                > "$PROJECT_ROOT/output/dashboard.log" 2>&1 &
            DASHBOARD_PID=$!
            sleep 2
            if kill -0 "$DASHBOARD_PID" 2>/dev/null; then
                success "仪表板已启动 (PID: $DASHBOARD_PID)"
                echo -e "  访问地址: ${GREEN}http://localhost:8050${NC}"
                echo -e "  日志文件: output/dashboard.log"
            else
                error "仪表板启动失败，查看 output/dashboard.log"
            fi

            info "后台启动任务 Worker..."
            nohup python3 -u -m fake_cdn worker --config-db "$CONFIG_DB" \
                > "$PROJECT_ROOT/output/worker.log" 2>&1 &
            WORKER_PID=$!
            sleep 2
            if kill -0 "$WORKER_PID" 2>/dev/null; then
                success "任务 Worker 已启动 (PID: $WORKER_PID)"
                echo -e "  日志文件: output/worker.log"
            else
                kill "$DASHBOARD_PID" 2>/dev/null || true
                error "任务 Worker 启动失败，查看 output/worker.log"
            fi
            echo "DASHBOARD_PID=$DASHBOARD_PID" > "$PID_FILE"
            echo "WORKER_PID=$WORKER_PID" >> "$PID_FILE"
            echo ""
            success "所有服务已在后台启动!"
            echo ""
            echo -e "  仪表板: ${GREEN}http://localhost:8050${NC}"
            echo -e "  停止服务: ${YELLOW}$0 stop${NC}"
            echo -e "  查看日志: tail -f output/worker.log"
            ;;
        9|stop)
            info "停止服务..."
            stop_managed_services
            success "托管的 Dashboard 与 Worker 已停止"
            ;;
        10|config)
            configure_api
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
    TENANT_ID="${FAKE_CDN_TENANT_ID:-}"
    CONFIG_DB="${FAKE_CDN_CONFIG_DB_PATH:-output/config.db}"

    while [[ $# -gt 0 ]]; do
        case $1 in
            -h|--help)
                show_help
                ;;
            --skip-deps)
                SKIP_DEPS=true
                shift
                ;;
            --tenant-id)
                if [ -z "${2:-}" ]; then
                    error "--tenant-id 缺少参数"
                fi
                TENANT_ID="$2"
                shift 2
                ;;
            --config-db)
                if [ -z "${2:-}" ]; then
                    error "--config-db 缺少参数"
                fi
                CONFIG_DB="$2"
                shift 2
                ;;
            *)
                MODE="$1"
                shift
                ;;
        esac
    done

    TENANT_ARGS=()
    if [ -n "$TENANT_ID" ]; then
        TENANT_ARGS=(--tenant-id "$TENANT_ID" --config-db "$CONFIG_DB")
    fi

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

    # 如果有命令行参数，直接运行
    if [ -n "$MODE" ]; then
        separator
        run_mode "$MODE"
        exit 0
    fi

    # 首次运行向导
    first_run_wizard

    # 交互式菜单
    while true; do
        show_menu
        read -p "请选择 [0-9]: " choice
        echo ""
        run_mode "$choice" || continue

        echo ""
        read -p "按回车继续..." _
    done
}

# 运行
main "$@"
