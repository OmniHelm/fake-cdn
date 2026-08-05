# Fake CDN - CDN日志模拟系统

> 按95计费策略生成真实的模拟CDN监控数据

**⚠️ 警告: 仅用于测试/开发环境! 禁止向生产计费系统推送假数据!**

---

## 系统简介

这是一个用于生成模拟CDN监控日志的系统,核心特性:

1. **精确的95计费模拟** - 生成的数据95分位精确等于目标值
2. **真实的流量曲线** - 包含日周期、周周期、随机噪声、突发流量
3. **完整的指标推导** - 从带宽推导所有CDN指标(流量、请求数、状态码等)
4. **异常事件注入** - 模拟源站故障、缓存清理、DDoS等真实场景
5. **多维度分布** - 支持多域名、多地区
6. **可视化仪表板** - 基于 Dash + Plotly 的数据分析面板

---

## 快速开始

### 一键安装

```bash
curl -fsSL https://raw.githubusercontent.com/OmniHelm/fake-cdn/main/scripts/deploy.sh | bash
```

脚本会自动完成:
- 克隆项目到 `/opt/fake-cdn`
- 检查 Python 环境 (需要 3.8+)
- 创建虚拟环境
- 安装依赖
- 显示交互式菜单

> 固定安装到 `/opt/fake-cdn`，可通过 `FAKE_CDN_DIR` 环境变量指定其他位置

### 选择运行模式

```
请选择运行模式:

  1) simulation  - 模拟模式 (生成测试数据)
  2) realtime    - 实时模式 (按真实时间推送)
  3) catchup     - 追赶模式 (补推历史数据)
  4) validate    - 验证模式 (验证生成的日志)
  5) dashboard   - 启动仪表板 (可视化监控)
  6) worker      - 管理员任务队列 Worker
  7) status      - 查看状态
  8) full        - 完整模式 (dashboard + worker 后台启动)
  9) stop        - 停止后台服务
 10) config      - 配置 API 推送
  0) exit        - 退出
```

### 命令行直接运行

也可以跳过交互菜单，直接指定模式:

```bash
# 生成模拟数据
./scripts/deploy.sh simulation

# 后台启动管理后台和任务 Worker；具体租户、模式和时间在任务中心提交
./scripts/deploy.sh full

# 停止后台服务
./scripts/deploy.sh stop

# 查看状态
./scripts/deploy.sh status

# 跳过依赖安装
./scripts/deploy.sh --skip-deps simulation
```

---

## 配置

编辑 `config.json`:

```json
{
  "target": {
    "bandwidth_gbps": 20.0,
    "comment": "平均带宽20Gbps = 每天211TB流量"
  },
  "time": {
    "start_date": "2025-01-01",
    "duration_days": 30,
    "interval_seconds": 300
  },
  "mode": {
    "dry_run": true,
    "save_local": true
  }
}
```

### 环境变量 (真实推送时需要)

```bash
export CDN_API_ENDPOINT=<your_api_endpoint>
export CDN_API_VIP=<your_vip>
```

### 后台配置管理

配置管理以 `output/config.db` 为事实来源，按 `tenant_id` 隔离租户、不可变配置版本、
草稿、发布、回滚、审计和生成任务。`config.json` 只用于首次迁移和未指定租户时的
CLI 兼容模式，不再由后台页面直接改写。

首次迁移并检查历史日志中的待映射租户：

```bash
python -m fake_cdn tenant-migrate --config ./config.json
```

启动管理后台：

```bash
python -m fake_cdn dashboard --config ./config.json --port 8050
```

打开 `http://127.0.0.1:8050/config`。后台支持：

1. Tenant 列表、创建和租户配置入口。
2. 六步编辑器保存草稿，显式发布后才影响新的生成任务。
3. 乐观锁防并发覆盖，历史版本可回滚为新的发布版本。
4. 配置、发布、回滚和任务操作统一记录在数据库审计表。
5. CDN 页面 URL 固定携带租户，例如 `/tenants/hccl/overview`；所有 SQL 都强制
   使用 `tenant_id`，不提供默认跨租户“全部”视图。

从数据库中的已发布版本运行任务：

```bash
python -m fake_cdn simulation --tenant-id hccl --dry-run
python -m fake_cdn catchup --tenant-id hccl --start-date 2026-03-01 --end-date 2026-03-31
./scripts/deploy.sh --tenant-id hccl realtime
```

每个任务写入 `output/tenants/{tenant_id}/jobs/{job_id}/`，日志同时记录
`tenant_id`、`config_version_id` 和 `generation_job_id`，便于追溯。

### 管理员任务中心

配置保存、发布和回滚不会直接生成数据。配置中心的执行入口是管理员专用的
`/admin/jobs` 页面：管理员选择租户、任务模式和时间窗口后，系统将任务写入
SQLite 队列；独立 Worker 领取任务并执行固定的已发布版本。任务中心提供：

1. 全量任务状态、租户和模式筛选，以及执行日志和统计详情。
2. 模拟、历史补推、实时三种模式的参数校验；补推时间必须在版本窗口内并按粒度对齐。
3. 每个租户同一时间最多一个活动任务，重复提交会被幂等拒绝。
4. 排队取消、运行中停止、失败重试和 Worker 重启恢复；重试始终使用原配置版本。
5. 真实推送配置必须输入租户 ID 二次确认；任务执行时才读取 `.env` 中的 API 环境变量。

启动本地管理后台和 Worker：

```bash
python -m fake_cdn dashboard --config ./config.json --config-db output/config.db --port 8050
python -m fake_cdn worker --config-db output/config.db
```

生产环境建议使用 `scripts/deploy.sh full` 或 systemd 单元，确保 Dashboard 与 Worker
独立重启。Dashboard 通过 Gunicorn 的 `fake_cdn.dashboard.wsgi:application` 入口运行。

### systemd 安装

以下步骤只在目标主机初始化时执行一次。服务模板默认项目目录为 `/opt/fake-cdn`，
运行用户为 `fake-cdn`：

```bash
sudo useradd --system --home /opt/fake-cdn --shell /usr/sbin/nologin fake-cdn
sudo chown -R fake-cdn:fake-cdn /opt/fake-cdn
sudo install -m 0644 deploy/systemd/fake-cdn-dashboard.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/fake-cdn-worker.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/fake-cdn.target /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fake-cdn.target
```

确认 `DASHBOARD_SECRET_KEY`、认证用户和 API 环境变量已写入 `/opt/fake-cdn/.env`，
并保证 `output/` 对服务用户可写。查看状态与日志：

```bash
systemctl status fake-cdn-dashboard fake-cdn-worker
journalctl -u fake-cdn-dashboard -u fake-cdn-worker -f
```

配置保存不会自动生成数据或调用 CDN API。实际执行必须通过管理员任务中心或 CLI，
真实推送还需要二次确认。对外提供仪表板时，可使用现有的单管理员登录，不需要额外
角色系统：

```bash
export DASHBOARD_USERNAME=admin
export DASHBOARD_PASSWORD=<strong-password>
export DASHBOARD_SECRET_KEY=<stable-random-secret>
# 普通账号固定绑定 tenant_id，不具备配置管理权限
export DASHBOARD_TENANT_USERS='{"hccl_user":{"password":"<user-password>","tenant_id":"hccl"}}'
```

启用后，页面路由以及 Dash 布局、依赖和回调接口都会验证管理员登录态，配置保存
接口不能在未登录状态下直接调用。租户普通账号只能进入自身的概览、流量分析、
月度报告和域名页面；服务端会忽略客户端提交的其他 `tenant_id`，并禁止配置写入。
未设置任何密码时仅适合本地开发。

---

## 项目结构

```
fake-cdn/
├── config.json              # 首次导入 / CLI 兼容配置
├── requirements.txt         # 依赖
├── README.md
│
├── fake_cdn/                # Python 包
│   ├── __main__.py          # python -m fake_cdn 入口
│   ├── cli.py               # 命令行接口
│   ├── core/                # 核心模块
│   │   ├── generator.py     # 带宽曲线 + 指标推导
│   │   ├── pusher.py        # HTTP 推送客户端
│   │   ├── scheduler.py     # 调度器 (实时/补推)
│   │   ├── storage.py       # SQLite 存储
│   │   ├── tenant_config.py # Tenant 配置版本、审计与任务队列存储
│   │   ├── job_service.py   # 任务校验、幂等、取消和重试
│   │   ├── job_runner.py    # 固定版本执行器与心跳
│   │   └── validator.py     # 95计费验证器
│   ├── worker.py            # 独立任务 Worker
│   └── dashboard/           # 可视化仪表板
│       ├── app.py           # Dash 应用与路由权限
│       ├── job_page.py      # 管理员任务中心
│       └── wsgi.py          # Gunicorn WSGI 入口
│
├── scripts/                 # Shell 脚本
│   ├── deploy.sh            # 一键部署 (推荐)
│   └── quickstart.sh        # 快速启动
│
└── output/                  # 输出目录
    ├── config.db            # 多租户配置数据库
    ├── cdn_logs.db          # SQLite 数据库
    ├── realtime.log         # 实时推送日志
    └── dashboard.log        # 仪表板日志
```

---

## 运行模式说明

| 模式 | 命令 | 说明 |
|------|------|------|
| simulation | `./scripts/deploy.sh simulation` | 一次性生成完整月度数据 |
| realtime | `./scripts/deploy.sh realtime` | 按时间间隔实时推送 |
| catchup | `./scripts/deploy.sh catchup` | 补推历史数据 |
| validate | `./scripts/deploy.sh validate` | 验证日志是否符合目标 |
| dashboard | `./scripts/deploy.sh dashboard` | 启动可视化仪表板 |
| worker | `./scripts/deploy.sh worker` | 前台启动管理员任务 Worker |
| full | `./scripts/deploy.sh full` | 后台启动 Dashboard + Worker |
| stop | `./scripts/deploy.sh stop` | 停止后台服务 |
| status | `./scripts/deploy.sh status` | 查看数据和服务状态 |

---

## 流量计算原理

**目标**: 平均带宽 20Gbps = 每天 211TB 流量

```
1 Gbps 全天跑满 = 86400秒 × 1Gbps / 8bits = 10.54 TB/天

20 Gbps × 10.54 = 210.8 TB/天
```

**真实流量特征**:
- 平均带宽: 20 Gbps
- 95分位带宽: 约 28 Gbps (比平均高40%)
- 峰值带宽: 约 30 Gbps

---

## 核心算法

### 带宽曲线生成

```python
bandwidth(t) = baseline × daily_pattern(t) × weekly_pattern(t) × noise(t) + burst(t)
```

- `baseline`: 基准带宽(目标平均值)
- `daily_pattern`: 日周期(凌晨低谷0.6x, 晚高峰1.3x)
- `weekly_pattern`: 周周期(周末0.85x)
- `noise`: 随机噪声(±8%)
- `burst`: 突发流量(可配置)

### 异常注入

- **凌晨运维**: 5xx增加
- **源站故障**: 回源失败率飙升
- **缓存清理**: 命中率骤降
- **DDoS攻击**: 4xx激增

---

## 验证报告示例

```
============================================================
带宽验证报告
============================================================

【验证结果】 ✓ 通过
  目标平均带宽: 20.00 Gbps
  实际平均带宽: 19.99 Gbps
  偏差: 0.05%
  每天流量: 210.82 TB
  95分位: 28.16 Gbps

【整体统计】
  数据点数: 8640
  最小带宽: 11.32 Gbps
  最大带宽: 29.88 Gbps
  95分位(P95): 28.16 Gbps
  99分位(P99): 29.68 Gbps
```

---

## 注意事项

### 关闭 Dry-Run

默认 `dry_run=true`,不会真实推送。

如果要真实推送 (仅限测试环境!):

1. 编辑 `config.json`: `"dry_run": false`
2. 设置环境变量: `CDN_API_ENDPOINT` 和 `CDN_API_VIP`
3. 运行时会提示确认
4. 确保 API endpoint 是测试环境!

### 时区问题

系统使用本地时区生成时间戳。如果部署到不同时区的服务器，生成的数据时间会有差异。

---

## 技术栈

- Python 3.8+
- SQLite (数据存储)
- requests (HTTP客户端)
- dash + plotly (可视化)

---

## License

MIT
