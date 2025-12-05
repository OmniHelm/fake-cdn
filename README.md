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
- 克隆项目到 `~/fake-cdn`
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
  6) status      - 查看状态
  7) full        - 完整模式 (realtime + dashboard 后台启动)
  8) stop        - 停止后台服务
  0) exit        - 退出
```

### 命令行直接运行

也可以跳过交互菜单，直接指定模式:

```bash
# 生成模拟数据
./scripts/deploy.sh simulation

# 后台启动 realtime + dashboard
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

---

## 项目结构

```
fake-cdn/
├── config.json              # 配置文件
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
│   │   └── validator.py     # 95计费验证器
│   └── dashboard/           # 可视化仪表板
│       └── app.py           # Dash 应用
│
├── scripts/                 # Shell 脚本
│   ├── deploy.sh            # 一键部署 (推荐)
│   └── quickstart.sh        # 快速启动
│
└── output/                  # 输出目录
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
| full | `./scripts/deploy.sh full` | 后台启动 realtime + dashboard |
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
