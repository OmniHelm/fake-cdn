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

### 1. 安装

```bash
# 克隆项目
git clone <repo-url>
cd fake-cdn

# 方式一: 开发模式安装 (推荐)
pip install -e .

# 方式二: 仅安装依赖
pip install -r requirements.txt

# 安装仪表板依赖 (可选)
pip install -e ".[dashboard]"
```

### 2. 配置

编辑 `config.json`:

```json
{
  "target": {
    "bandwidth_gbps": 15.0,
    "comment": "平均带宽15Gbps = 每天158TB流量"
  },
  "time": {
    "start_date": "2025-01-01",
    "duration_days": 30,
    "interval_seconds": 300
  }
}
```

### 3. 运行

```bash
# 模拟生成30天数据
python -m fake_cdn simulation

# 实时推送 (持续运行)
python -m fake_cdn realtime

# 历史补推
python -m fake_cdn catchup --start-date 2025-01-01 --end-date 2025-01-31

# 验证日志
python -m fake_cdn validate --log-file output/logs.jsonl

# 启动仪表板
python -m fake_cdn dashboard
```

或使用一键部署脚本:

```bash
./scripts/deploy.sh
```

---

## 项目结构

```
fake-cdn/
├── config.json              # 配置文件
├── pyproject.toml           # 项目配置 (PEP 517/518)
├── requirements.txt         # 依赖
├── README.md
│
├── fake_cdn/                # Python 包
│   ├── __init__.py
│   ├── __main__.py          # python -m fake_cdn 入口
│   ├── cli.py               # 命令行接口
│   ├── core/                # 核心模块
│   │   ├── generator.py     # 带宽曲线 + 指标推导
│   │   ├── pusher.py        # HTTP 推送客户端
│   │   ├── scheduler.py     # 调度器 (实时/补推)
│   │   └── validator.py     # 95计费验证器
│   └── dashboard/           # 可视化仪表板
│       └── app.py           # Dash 应用
│
├── scripts/                 # Shell 脚本
│   ├── deploy.sh            # 一键部署
│   └── quickstart.sh        # 快速启动
│
├── tests/                   # 测试
├── docs/                    # 文档
└── output/                  # 输出目录
    ├── logs.jsonl           # 日志文件
    ├── stats.json           # 统计信息
    └── bandwidth_curve.csv  # 带宽曲线
```

---

## 运行模式

| 模式 | 命令 | 说明 |
|------|------|------|
| simulation | `python -m fake_cdn simulation` | 一次性生成完整月度数据 |
| realtime | `python -m fake_cdn realtime` | 按时间间隔实时推送 |
| catchup | `python -m fake_cdn catchup --start-date ... --end-date ...` | 补推历史数据 |
| validate | `python -m fake_cdn validate --log-file ...` | 验证日志是否符合目标 |
| dashboard | `python -m fake_cdn dashboard` | 启动可视化仪表板 |

---

## 流量计算原理

**目标**: 平均带宽 15Gbps = 每天 158TB 流量

```
1 Mbps 全天跑满 = 86400秒 × 1Mbps / 8bits / 1024 = 10.54 GB/天

15 Gbps = 15000 Mbps
每天流量 = 15000 × 10.54 = 158100 GB = 158.1 TB
```

**真实流量特征**:
- 平均带宽: 15 Gbps
- 95分位带宽: 约 21 Gbps (比平均高40%)
- 峰值带宽: 约 22 Gbps

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

### 指标推导

```
流量 = 带宽 × 时间
请求数 = 流量 / 平均对象大小
缓存命中率 → 回源流量/请求数
状态码 → 按真实分布生成
```

### 异常注入

- **凌晨运维**: 5xx增加
- **源站故障**: 回源失败率飙升
- **缓存清理**: 命中率骤降
- **DDoS攻击**: 4xx激增

---

## 配置详解

### 目标配置

```json
{
  "target": {
    "bandwidth_gbps": 15.0
  }
}
```

### 时间配置

```json
{
  "time": {
    "start_date": "2025-01-01",
    "duration_days": 30,
    "interval_seconds": 300
  }
}
```

### 维度配置

```json
{
  "dimensions": {
    "tenant_id": "hccl",
    "domains": ["example.com", "cdn.example.com"],
    "regions": [
      {"country": "cn", "region": "mainland_china", "weight": 0.6}
    ]
  }
}
```

### 真实性配置

```json
{
  "realism": {
    "cache_hit_rate": [0.85, 0.95],
    "avg_object_size_kb": [200, 2048],
    "origin_fail_rate": [0.001, 0.01],
    "burst_probability": 0.0,
    "anomaly_probability": 0.001
  }
}
```

### 运行模式

```json
{
  "mode": {
    "dry_run": true,
    "save_local": true,
    "output_dir": "./output"
  }
}
```

- `dry_run`: true 时不真实推送到 API

---

## 命令行参数

```bash
# 查看帮助
python -m fake_cdn --help

# 指定配置文件
python -m fake_cdn simulation --config my_config.json

# 强制 dry-run
python -m fake_cdn simulation --dry-run

# 实时模式只执行一次
python -m fake_cdn realtime --once

# 补推历史数据
python -m fake_cdn catchup --start-date 2025-01-01 --end-date 2025-01-31

# 验证日志文件
python -m fake_cdn validate --log-file output/logs.jsonl

# 启动仪表板 (指定端口)
python -m fake_cdn dashboard --port 8080
```

---

## 验证报告示例

```
============================================================
带宽验证报告
============================================================

【验证结果】 ✓ 通过
  目标平均带宽: 15.00 Gbps
  实际平均带宽: 14.99 Gbps
  偏差: 0.05%
  每天流量: 158.02 TB
  95分位: 21.16 Gbps

【整体统计】
  数据点数: 8640
  最小带宽: 8.32 Gbps
  最大带宽: 21.88 Gbps
  95分位(P95): 21.16 Gbps
  99分位(P99): 21.68 Gbps
```

---

## 注意事项

### 关闭 Dry-Run

默认 `dry_run=true`,不会真实推送。

如果要真实推送 (仅限测试环境!):

1. 编辑 `config.json`: `"dry_run": false`
2. 运行时会提示确认
3. 确保 API endpoint 是测试环境!

### 性能

- 30天 × 288个点/天 = 8640个点
- 每个点 × 5个地区 = 43200条日志
- 推送速度: 约100条/秒

---

## 技术栈

- Python 3.8+
- requests (HTTP客户端)
- dash + plotly (可视化,可选)
- pandas (数据处理,可选)
