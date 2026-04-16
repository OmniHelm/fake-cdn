#!/usr/bin/env python3
"""
CDN 推送数据可视化面板
基于 Dash + Plotly 构建
使用 SQLite 存储提升性能
"""

import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path

import dash
from dash import dcc, html, dash_table
from dash.dependencies import Input, Output
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
from flask import session, redirect, request, Response

from ..core.storage import CDNLogStorage, get_default_storage


# ============================================================================
# 认证配置
# ============================================================================
def get_auth_config():
    """获取认证配置，从环境变量读取

    支持多用户:
    - DASHBOARD_USERNAME / DASHBOARD_PASSWORD: 主用户（默认 admin）
    - DASHBOARD_USERS: JSON 格式额外用户，如 {"viewer":"pass123","ops":"pass456"}
    """
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    username = os.environ.get("DASHBOARD_USERNAME", "admin")

    # 构建用户字典
    users = {}
    if password:
        users[username] = password

    # 解析额外用户
    extra_users = os.environ.get("DASHBOARD_USERS", "")
    if extra_users:
        import json
        try:
            parsed = json.loads(extra_users)
            if isinstance(parsed, dict):
                users.update(parsed)
        except json.JSONDecodeError:
            print(f"[警告] DASHBOARD_USERS 格式错误，应为 JSON 对象")

    return {
        "enabled": bool(users),
        "users": users,
    }


def verify_password(username: str, password: str) -> bool:
    """验证用户名密码（支持多用户）"""
    config = get_auth_config()
    if not config["enabled"]:
        return True
    # 遍历所有用户，使用常量时间比较防止时序攻击
    for valid_user, valid_pass in config["users"].items():
        correct_user = secrets.compare_digest(username, valid_user)
        correct_pass = secrets.compare_digest(password, valid_pass)
        if correct_user and correct_pass:
            return True
    return False

# ============================================================================
# 专业配色方案 (参考 Stripe/Linear 设计规范)
# ============================================================================
COLORS = {
    # 侧边栏
    "sidebar_bg": "#0f172a",       # Slate-900
    "sidebar_hover": "#1e293b",    # Slate-800
    "sidebar_active": "#334155",   # Slate-700
    "sidebar_text": "#94a3b8",     # Slate-400
    "sidebar_accent": "#38bdf8",   # Sky-400

    # 基础色
    "bg": "#f1f5f9",               # Slate-100
    "card": "#ffffff",
    "border": "#e2e8f0",           # Slate-200

    # 文字
    "text_primary": "#0f172a",     # Slate-900
    "text_secondary": "#475569",   # Slate-600
    "text_muted": "#94a3b8",       # Slate-400

    # 语义色
    "primary": "#0ea5e9",          # Sky-500
    "success": "#10b981",          # 绿 - 正向指标
    "warning": "#f59e0b",          # 橙 - 警告
    "danger": "#ef4444",           # 红 - 错误
    "info": "#06b6d4",             # 青 - 信息
    "purple": "#8b5cf6",           # 紫 - 辅助

    # 图表专用
    "chart_primary": "#0ea5e9",
    "chart_secondary": "#10b981",
    "chart_tertiary": "#8b5cf6",
    "chart_grid": "#f1f5f9",
}

# HTTP 状态码配色
HTTP_COLORS = {
    "2xx": "#10b981",  # 成功 - 绿
    "3xx": "#3b82f6",  # 重定向 - 蓝
    "4xx": "#f59e0b",  # 客户端错误 - 橙
    "5xx": "#ef4444",  # 服务端错误 - 红
}

# 图表全局配置
CHART_LAYOUT = {
    "font": {"family": "Inter, -apple-system, BlinkMacSystemFont, sans-serif", "color": COLORS["text_primary"]},
    "paper_bgcolor": "rgba(0,0,0,0)",
    "plot_bgcolor": "rgba(0,0,0,0)",
    "margin": {"t": 50, "b": 40, "l": 60, "r": 40},
    "hovermode": "x unified",
    "legend": {"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "right", "x": 1},
}


def get_storage() -> CDNLogStorage:
    """获取 SQLite 存储实例"""
    return get_default_storage()


def load_data_from_sqlite(
    storage: CDNLogStorage,
    start_time: int = None,
    end_time: int = None,
    domain: str = None
) -> list:
    """从 SQLite 加载数据"""
    return storage.query_logs(
        start_time=start_time,
        end_time=end_time,
        domain=domain if domain != "all" else None
    )


def process_data(records):
    """处理数据为 DataFrame"""
    if not records:
        return pd.DataFrame()

    data = []
    for i, record in enumerate(records):
        # 从 start_time 转换时间戳
        start_time_ms = record["start_time"]
        timestamp = datetime.fromtimestamp(start_time_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
        # 使用 start_time 作为 batch 标识（同一时间点的记录归为一批）
        batch = start_time_ms

        # 获取时间间隔，用于将 bit 总量转换为 bps
        interval = record.get("interval", 300)

        row = {
            "timestamp": timestamp,
            "batch": batch,
            "domain": record["domain"],
            "bw_mbps": (record["bw"] or 0) / interval / 1000000,  # bit 总量 / interval = bps -> Mbps
            "flux_gb": (record["flux"] or 0) / (1024**3),
            "bs_bw_mbps": (record["bs_bw"] or 0) / interval / 1000000,  # bit 总量 / interval = bps -> Mbps
            "bs_flux_gb": (record["bs_flux"] or 0) / (1024**3),
            "req_num": record["req_num"] or 0,
            "hit_num": record["hit_num"] or 0,
            "bs_num": record["bs_num"] or 0,
            "bs_fail_num": record["bs_fail_num"] or 0,
            "hit_flux_gb": (record["hit_flux"] or 0) / (1024**3),
            "http_2xx": record["http_code_2xx"] or 0,
            "http_3xx": record["http_code_3xx"] or 0,
            "http_4xx": record["http_code_4xx"] or 0,
            "http_5xx": record["http_code_5xx"] or 0,
            "bs_http_2xx": record["bs_http_code_2xx"] or 0,
            "bs_http_3xx": record["bs_http_code_3xx"] or 0,
            "bs_http_4xx": record["bs_http_code_4xx"] or 0,
            "bs_http_5xx": record["bs_http_code_5xx"] or 0,
        }
        row["hit_rate"] = (row["hit_num"] / row["req_num"] * 100) if row["req_num"] > 0 else 0
        row["bs_fail_rate"] = (row["bs_fail_num"] / row["bs_num"] * 100) if row["bs_num"] > 0 else 0
        data.append(row)

    df = pd.DataFrame(data)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def get_default_date_range(storage: CDNLogStorage):
    """获取默认日期范围（默认当天）"""
    min_time, max_time = storage.get_time_range()
    if min_time is None or max_time is None:
        # 无数据时返回当前时间范围
        now = datetime.now()
        return now.date(), now.date()

    # 默认显示当天
    today = datetime.now().date()
    return today, today


def create_metric_card(title, value, subtitle=None, color=None, trend=None):
    """创建单个指标卡片"""
    value_children = [value]
    if trend is not None and abs(trend) >= 0.1:
        arrow = "\u25b2" if trend > 0 else "\u25bc"
        cls = "metric-trend up" if trend > 0 else "metric-trend down"
        value_children.append(html.Span(f"{arrow} {abs(trend):.1f}%", className=cls))

    return html.Div([
        html.Div(title, className="metric-label"),
        html.Div(value_children, className="metric-value", style={"color": color} if color else {}),
        html.Div(subtitle, className="metric-subtitle") if subtitle else None,
    ], className="metric-card")


def create_summary_cards(df):
    """创建汇总卡片"""
    import numpy as np

    # 按时间点聚合后计算带宽指标
    time_agg = df.groupby("batch").agg({"bw_mbps": "sum", "timestamp": "first"})
    peak_bw = time_agg["bw_mbps"].max()  # 峰值带宽
    avg_bw = time_agg["bw_mbps"].mean()  # 平均带宽

    # 计算日平均和日95
    time_agg["date"] = time_agg["timestamp"].dt.date

    def calc_95_billing(bw_series):
        """计算95计费值：排序后取第95%位置的值"""
        sorted_bw = bw_series.sort_values(ascending=True).values
        n = len(sorted_bw)
        idx = int(n * 0.95) - 1
        if idx < 0:
            idx = 0
        if idx >= n:
            idx = n - 1
        return sorted_bw[idx]

    daily_stats = time_agg.groupby("date").agg({
        "bw_mbps": ["mean", calc_95_billing]
    })
    daily_stats.columns = ["daily_avg", "daily_p95"]
    daily_avg_bw = daily_stats["daily_avg"].mean()
    daily_p95_bw = daily_stats["daily_p95"].mean()

    total_flux = df["flux_gb"].sum()
    avg_hit_rate = df["hit_rate"].mean()

    # 计算趋势：将时间范围分为前后两半比较
    mid_batch = df["batch"].quantile(0.5)
    first_half = df[df["batch"] <= mid_batch]
    second_half = df[df["batch"] > mid_batch]

    def calc_trend(first, second, col, agg="sum"):
        if first.empty or second.empty:
            return None
        v1 = first[col].sum() if agg == "sum" else first[col].mean()
        v2 = second[col].sum() if agg == "sum" else second[col].mean()
        if v1 == 0:
            return None
        return ((v2 / v1) - 1) * 100

    bw_trend = calc_trend(first_half, second_half, "bw_mbps", "mean")
    flux_trend = calc_trend(first_half, second_half, "flux_gb")
    hit_trend = calc_trend(first_half, second_half, "hit_rate", "mean")

    return html.Div([
        create_metric_card("峰值带宽", f"{peak_bw/1000:.1f} Gbps", f"平均 {avg_bw/1000:.1f} Gbps", trend=bw_trend),
        create_metric_card("日平均带宽", f"{daily_avg_bw/1000:.1f} Gbps", "每日平均值"),
        create_metric_card("日95带宽", f"{daily_p95_bw/1000:.1f} Gbps", "95th 分位计费带宽", COLORS["primary"]),
        create_metric_card("总流量", f"{total_flux:.1f} GB", "累计传输流量", trend=flux_trend),
        create_metric_card("缓存命中率", f"{avg_hit_rate:.1f}%", "平均命中比例",
                          COLORS["success"] if avg_hit_rate >= 90 else COLORS["warning"], trend=hit_trend),
    ], className="metrics-grid")


def apply_chart_style(fig, title):
    """应用统一的图表样式"""
    fig.update_layout(
        title={"text": title, "font": {"size": 14, "color": COLORS["text_primary"]}, "x": 0, "xanchor": "left"},
        font={"family": "Inter, sans-serif", "color": COLORS["text_secondary"], "size": 12},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin={"t": 40, "b": 40, "l": 50, "r": 20},
        hovermode="x unified",
        hoverlabel={"bgcolor": "#0f172a", "font_size": 12, "font_color": "#f8fafc", "bordercolor": "#334155"},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "right", "x": 1, "font": {"size": 11}},
    )
    fig.update_xaxes(
        showgrid=True, gridcolor=COLORS["chart_grid"], gridwidth=1,
        showline=False, zeroline=False,
        tickfont={"size": 11, "color": COLORS["text_muted"]}
    )
    fig.update_yaxes(
        showgrid=True, gridcolor=COLORS["chart_grid"], gridwidth=1,
        showline=False, zeroline=False,
        tickfont={"size": 11, "color": COLORS["text_muted"]}
    )
    return fig


# 自定义 HTML 模板
INDEX_STRING = '''
<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>{%title%}</title>
        {%favicon%}
        {%css%}
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
        <link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200&display=swap" rel="stylesheet">
        <style>
            * { box-sizing: border-box; }

            body {
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                background-color: #f1f5f9;
                margin: 0;
                padding: 0;
                color: #0f172a;
                line-height: 1.5;
                height: 100vh;
                overflow: hidden;
            }

            .material-symbols-outlined {
                font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 20;
                font-size: 20px;
                vertical-align: middle;
            }

            /* ===== Shell 布局 ===== */
            .app-shell {
                display: flex;
                height: 100vh;
                overflow: hidden;
            }

            /* ===== 侧边栏 ===== */
            .sidebar {
                width: 240px;
                min-width: 240px;
                background: #0f172a;
                display: flex;
                flex-direction: column;
                overflow-y: auto;
                overflow-x: hidden;
                z-index: 10;
            }
            .sidebar-logo {
                display: flex;
                align-items: center;
                gap: 12px;
                padding: 20px 20px 24px;
            }
            .sidebar-logo-text {
                font-size: 15px;
                font-weight: 600;
                color: #f1f5f9;
                white-space: nowrap;
            }
            .sidebar-section {
                padding: 16px 20px 8px;
                font-size: 11px;
                font-weight: 600;
                color: #475569;
                text-transform: uppercase;
                letter-spacing: 0.8px;
                white-space: nowrap;
            }
            .sidebar-item {
                display: flex;
                align-items: center;
                gap: 12px;
                padding: 9px 20px;
                color: #94a3b8;
                font-size: 13px;
                font-weight: 500;
                cursor: default;
                border-left: 3px solid transparent;
                transition: all 0.15s ease;
                white-space: nowrap;
            }
            .sidebar-item:hover {
                background: #1e293b;
                color: #cbd5e1;
            }
            .sidebar-item.active {
                background: #1e293b;
                color: #f1f5f9;
                border-left-color: #38bdf8;
            }
            .sidebar-item .material-symbols-outlined {
                font-size: 20px;
                opacity: 0.7;
                flex-shrink: 0;
            }
            .sidebar-item.active .material-symbols-outlined {
                opacity: 1;
                color: #38bdf8;
            }
            .sidebar-item span:last-child {
                overflow: hidden;
                text-overflow: ellipsis;
            }
            .sidebar-footer {
                margin-top: auto;
                padding: 16px 20px;
                border-top: 1px solid #1e293b;
                display: flex;
                align-items: center;
                gap: 8px;
                font-size: 12px;
                color: #64748b;
                white-space: nowrap;
            }
            .status-dot {
                width: 8px;
                height: 8px;
                border-radius: 50%;
                flex-shrink: 0;
            }
            .status-dot.green { background: #10b981; box-shadow: 0 0 6px rgba(16,185,129,0.4); }
            .status-dot.yellow { background: #f59e0b; }
            .status-dot.red { background: #ef4444; }

            /* ===== 主区域 ===== */
            .main-area {
                flex: 1;
                display: flex;
                flex-direction: column;
                overflow: hidden;
                min-width: 0;
            }

            /* ===== 顶栏 ===== */
            .topbar {
                height: 56px;
                min-height: 56px;
                background: #ffffff;
                border-bottom: 1px solid #e2e8f0;
                display: flex;
                align-items: center;
                padding: 0 24px;
                gap: 16px;
            }
            .topbar-breadcrumb {
                font-size: 14px;
                color: #94a3b8;
                white-space: nowrap;
            }
            .topbar-breadcrumb strong {
                color: #0f172a;
                font-weight: 600;
            }
            .topbar-info {
                flex: 1;
                font-size: 12px;
                color: #94a3b8;
                text-align: center;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
            }
            .topbar-right {
                display: flex;
                align-items: center;
                gap: 16px;
                margin-left: auto;
                white-space: nowrap;
            }
            .topbar-status {
                display: flex;
                align-items: center;
                gap: 6px;
                font-size: 12px;
                color: #10b981;
                font-weight: 500;
            }
            .topbar-refresh {
                font-size: 12px;
                color: #94a3b8;
            }
            .topbar-logout {
                display: inline-flex;
                align-items: center;
                gap: 6px;
                padding: 6px 14px;
                font-size: 13px;
                color: #475569;
                text-decoration: none;
                border: 1px solid #e2e8f0;
                border-radius: 6px;
                transition: all 0.15s ease;
                font-family: inherit;
                background: transparent;
                cursor: pointer;
            }
            .topbar-logout:hover {
                background: #f8fafc;
                border-color: #cbd5e1;
            }

            /* ===== 内容区 ===== */
            .content-area {
                flex: 1;
                overflow-y: auto;
                padding: 24px;
            }

            /* ===== 时间范围按钮 ===== */
            .time-range-bar {
                display: flex;
                align-items: center;
                gap: 8px;
                margin-bottom: 16px;
            }
            .time-range-btn {
                padding: 6px 14px;
                font-size: 13px;
                font-weight: 500;
                color: #475569;
                background: #ffffff;
                border: 1px solid #e2e8f0;
                border-radius: 6px;
                cursor: pointer;
                transition: all 0.15s ease;
                font-family: inherit;
                line-height: 1.4;
            }
            .time-range-btn:hover {
                background: #f8fafc;
                border-color: #cbd5e1;
            }
            .time-range-btn.active {
                background: #0ea5e9;
                color: #ffffff;
                border-color: #0ea5e9;
            }
            .time-range-spacer { flex: 1; }

            /* ===== 筛选器 ===== */
            .filter-bar {
                background: #ffffff;
                border: 1px solid #e2e8f0;
                border-radius: 10px;
                padding: 14px 20px;
                margin-bottom: 20px;
                display: flex;
                align-items: center;
                gap: 16px;
                flex-wrap: wrap;
            }
            .filter-label {
                font-size: 13px;
                font-weight: 500;
                color: #475569;
            }

            /* ===== 指标卡片 ===== */
            .metrics-grid {
                display: grid;
                grid-template-columns: repeat(5, 1fr);
                gap: 16px;
                margin-bottom: 20px;
            }
            .metric-card {
                background: #ffffff;
                border: 1px solid #e2e8f0;
                border-radius: 10px;
                padding: 20px;
                transition: all 0.2s ease;
            }
            .metric-card:hover {
                box-shadow: 0 4px 12px rgba(0,0,0,0.06);
                transform: translateY(-1px);
            }
            .metric-label {
                font-size: 12px;
                font-weight: 500;
                color: #64748b;
                margin-bottom: 8px;
                text-transform: uppercase;
                letter-spacing: 0.3px;
            }
            .metric-value {
                font-size: 26px;
                font-weight: 700;
                color: #0f172a;
                letter-spacing: -0.5px;
            }
            .metric-subtitle {
                font-size: 12px;
                color: #94a3b8;
                margin-top: 6px;
            }
            .metric-trend {
                display: inline-flex;
                align-items: center;
                gap: 2px;
                font-size: 12px;
                font-weight: 600;
                padding: 2px 8px;
                border-radius: 4px;
                margin-left: 8px;
            }
            .metric-trend.up {
                color: #059669;
                background: #ecfdf5;
            }
            .metric-trend.down {
                color: #dc2626;
                background: #fef2f2;
            }

            /* ===== 图表容器 ===== */
            .chart-card {
                background: #ffffff;
                border: 1px solid #e2e8f0;
                border-radius: 10px;
                padding: 20px;
                margin-bottom: 20px;
                transition: box-shadow 0.2s ease;
            }
            .chart-card:hover {
                box-shadow: 0 2px 8px rgba(0,0,0,0.04);
            }
            .chart-card h3 {
                font-size: 14px;
                font-weight: 600;
                color: #0f172a;
                margin: 0 0 16px 0;
            }

            /* 双列布局 */
            .chart-row {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 20px;
                margin-bottom: 20px;
            }

            /* 表格样式覆盖 */
            .dash-table-container .dash-spreadsheet-container .dash-spreadsheet-inner td,
            .dash-table-container .dash-spreadsheet-container .dash-spreadsheet-inner th {
                font-family: 'Inter', sans-serif !important;
            }

            /* 下拉框样式 */
            .Select-control {
                border-color: #e2e8f0 !important;
                border-radius: 6px !important;
            }
            .Select-control:hover {
                border-color: #cbd5e1 !important;
            }

            /* ===== 响应式 ===== */
            @media (max-width: 1100px) {
                .sidebar {
                    width: 60px;
                    min-width: 60px;
                }
                .sidebar-logo-text,
                .sidebar-section,
                .sidebar-item span:last-child,
                .sidebar-footer span:not(.status-dot) {
                    display: none;
                }
                .sidebar-logo { padding: 20px 14px 24px; justify-content: center; }
                .sidebar-item { padding: 10px 0; justify-content: center; border-left: none; border-right: 3px solid transparent; }
                .sidebar-item.active { border-left-color: transparent; border-right-color: #38bdf8; }
                .sidebar-footer { justify-content: center; padding: 16px 10px; }
                .metrics-grid { grid-template-columns: repeat(3, 1fr); }
            }
            @media (max-width: 768px) {
                .sidebar { display: none; }
                .metrics-grid { grid-template-columns: repeat(2, 1fr); }
                .chart-row { grid-template-columns: 1fr; }
                .content-area { padding: 16px; }
                .topbar { padding: 0 16px; }
                .time-range-bar { flex-wrap: wrap; }
                .filter-bar { flex-direction: column; align-items: stretch; }
            }
        </style>
    </head>
    <body>
        {%app_entry%}
        <footer>
            {%config%}
            {%scripts%}
            {%renderer%}
        </footer>
    </body>
</html>
'''


# 刷新间隔（毫秒）
REFRESH_INTERVAL_MS = 30 * 1000  # 30秒


# 登录页面 HTML (注意: CSS 花括号需要转义为 {{ }})
LOGIN_PAGE = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>登录 - CDN Panel</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .login-container {{
            background: #ffffff;
            border-radius: 12px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            padding: 40px;
            width: 100%;
            max-width: 400px;
        }}
        .login-header {{
            text-align: center;
            margin-bottom: 32px;
        }}
        .login-header h1 {{
            font-size: 22px;
            font-weight: 700;
            color: #0f172a;
            margin-bottom: 8px;
        }}
        .login-header p {{
            font-size: 14px;
            color: #64748b;
        }}
        .form-group {{
            margin-bottom: 20px;
        }}
        .form-group label {{
            display: block;
            font-size: 13px;
            font-weight: 500;
            color: #475569;
            margin-bottom: 8px;
        }}
        .form-group input {{
            width: 100%;
            padding: 10px 14px;
            font-size: 14px;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            outline: none;
            transition: border-color 0.2s, box-shadow 0.2s;
            font-family: inherit;
        }}
        .form-group input:focus {{
            border-color: #0ea5e9;
            box-shadow: 0 0 0 3px rgba(14, 165, 233, 0.1);
        }}
        .btn-login {{
            width: 100%;
            padding: 11px;
            font-size: 14px;
            font-weight: 600;
            color: #ffffff;
            background: #0ea5e9;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            transition: background 0.2s;
            font-family: inherit;
        }}
        .btn-login:hover {{
            background: #0284c7;
        }}
        .error-msg {{
            background: #fef2f2;
            border: 1px solid #fecaca;
            color: #dc2626;
            padding: 12px 16px;
            border-radius: 8px;
            font-size: 14px;
            margin-bottom: 20px;
            display: {error_display};
        }}
    </style>
</head>
<body>
    <div class="login-container">
        <div class="login-header">
            <h1>CDN Panel</h1>
            <p>请输入凭据以访问控制台</p>
        </div>
        <div class="error-msg">{error_message}</div>
        <form method="POST" action="/login">
            <div class="form-group">
                <label for="username">用户名</label>
                <input type="text" id="username" name="username" value="{username}" required autocomplete="username">
            </div>
            <div class="form-group">
                <label for="password">密码</label>
                <input type="password" id="password" name="password" required autocomplete="current-password">
            </div>
            <button type="submit" class="btn-login">登录</button>
        </form>
    </div>
</body>
</html>
'''


def create_sidebar(auth_config):
    """创建侧边栏导航"""
    def nav_item(icon, label, active=False):
        cls = "sidebar-item active" if active else "sidebar-item"
        return html.Div([
            html.Span(icon, className="material-symbols-outlined"),
            html.Span(label),
        ], className=cls)

    return html.Div([
        # Logo
        html.Div([
            html.Span("CDN Panel", className="sidebar-logo-text"),
        ], className="sidebar-logo"),

        # 主要导航
        html.Div("主要", className="sidebar-section"),
        nav_item("dashboard", "概览"),
        nav_item("analytics", "流量分析", active=True),
        nav_item("language", "域名管理"),
        nav_item("dns", "DNS 记录"),

        # 配置
        html.Div("配置", className="sidebar-section"),
        nav_item("cached", "缓存"),
        nav_item("shield", "安全防护"),
        nav_item("lock", "SSL/TLS"),
        nav_item("local_fire_department", "WAF 防火墙"),
        nav_item("speed", "性能优化"),

        # 网络
        html.Div("网络", className="sidebar-section"),
        nav_item("tune", "流量规则"),
        nav_item("cloud_sync", "负载均衡"),
        nav_item("monitoring", "实时监控"),

        # 底部状态
        html.Div([
            html.Span(className="status-dot green"),
            html.Span("系统运行正常"),
        ], className="sidebar-footer"),
    ], className="sidebar")


def create_topbar(auth_config):
    """创建顶部信息栏"""
    right_items = [
        html.Div([
            html.Span(className="status-dot green"),
            html.Span("健康"),
        ], className="topbar-status"),
        html.Span(id="refresh-status", className="topbar-refresh"),
    ]
    if auth_config["enabled"]:
        right_items.append(
            html.A("退出登录", href="/logout", className="topbar-logout")
        )

    return html.Div([
        html.Span([
            "CDN Panel / ",
            html.Strong("流量分析"),
        ], className="topbar-breadcrumb"),
        html.Span(id="header-info", className="topbar-info"),
        html.Div(right_items, className="topbar-right"),
    ], className="topbar")


def create_time_controls(default_start, default_end, domains):
    """创建时间控制区域"""
    return html.Div([
        # 快捷时间按钮行
        html.Div([
            html.Button("最近 24 小时", id="range-24h", className="time-range-btn", n_clicks=0),
            html.Button("最近 7 天", id="range-7d", className="time-range-btn", n_clicks=0),
            html.Button("最近 30 天", id="range-30d", className="time-range-btn active", n_clicks=0),
            html.Button("自定义范围", id="range-custom", className="time-range-btn", n_clicks=0),
            html.Div(className="time-range-spacer"),
            # 域名筛选
            html.Span("域名", className="filter-label"),
            dcc.Dropdown(
                id="domain-filter",
                options=[{"label": "全部域名", "value": "all"}] +
                        [{"label": d, "value": d} for d in sorted(domains)],
                value="all",
                style={"width": "200px"},
                clearable=False
            ),
        ], className="time-range-bar"),
        # 自定义时间范围
        html.Div([
            html.Span("开始", className="filter-label"),
            dcc.Input(
                id="start-datetime",
                type="datetime-local",
                value=f"{default_start}T00:00:00",
                style={"width": "200px", "padding": "6px 10px",
                       "border": "1px solid #e2e8f0", "borderRadius": "6px", "fontSize": "13px"}
            ),
            html.Span("结束", className="filter-label"),
            dcc.Input(
                id="end-datetime",
                type="datetime-local",
                value=f"{default_end}T23:59:59",
                style={"width": "200px", "padding": "6px 10px",
                       "border": "1px solid #e2e8f0", "borderRadius": "6px", "fontSize": "13px"}
            ),
        ], className="filter-bar"),
    ])


def create_app(data_file=None):
    """创建 Dash 应用"""
    # 获取 SQLite 存储
    storage = get_storage()

    # 获取数据范围
    default_start, default_end = get_default_date_range(storage)
    min_time, max_time = storage.get_time_range()

    # 获取域名列表
    domains = storage.get_domains()

    # 计算日期范围边界
    if min_time and max_time:
        min_date = datetime.fromtimestamp(min_time / 1000).date()
        # max_date_allowed 设为未来30天，避免限制用户选择
        max_date = (datetime.now() + timedelta(days=30)).date()
    else:
        min_date = default_start
        max_date = (datetime.now() + timedelta(days=30)).date()

    # 创建 Dash 应用
    app = dash.Dash(__name__, title="CDN Panel")
    app.index_string = INDEX_STRING

    # 设置 Flask secret key 用于 session
    app.server.secret_key = os.environ.get(
        "DASHBOARD_SECRET_KEY",
        secrets.token_hex(32)  # 如果未设置则生成随机 key（重启后失效）
    )

    # 获取认证配置
    auth_config = get_auth_config()

    # 注册认证路由
    @app.server.route("/login", methods=["GET", "POST"])
    def login():
        if not auth_config["enabled"]:
            return redirect("/")

        error_message = ""
        error_display = "none"
        username = ""

        if request.method == "POST":
            submitted_user = request.form.get("username", "")
            submitted_pass = request.form.get("password", "")

            if verify_password(submitted_user, submitted_pass):
                session["authenticated"] = True
                session["username"] = submitted_user
                return redirect("/")
            else:
                error_message = "用户名或密码错误"
                error_display = "block"
                username = submitted_user

        return LOGIN_PAGE.format(
            error_message=error_message,
            error_display=error_display,
            username=username
        )

    @app.server.route("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    # 认证中间件
    @app.server.before_request
    def check_auth():
        if not auth_config["enabled"]:
            return None

        # 排除静态资源和登录页面
        path = request.path
        if path in ["/login", "/logout"] or path.startswith("/_dash") or path.startswith("/assets"):
            return None

        # 检查是否已登录
        if not session.get("authenticated"):
            return redirect("/login")

    # 布局
    app.layout = html.Div([
        # 隐藏组件
        dcc.Interval(id="refresh-interval", interval=REFRESH_INTERVAL_MS, n_intervals=0),

        # 侧边栏
        create_sidebar(auth_config),

        # 主区域
        html.Div([
            # 顶栏
            create_topbar(auth_config),

            # 内容区
            html.Div([
                # 时间控制
                create_time_controls(default_start, default_end, domains),

                # 汇总卡片
                html.Div(id="summary-cards"),

                # 第一排: 请求带宽 + 请求流量
                html.Div([
                    html.Div([
                        dcc.Graph(id="bandwidth-chart", config={"displayModeBar": False})
                    ], className="chart-card"),
                    html.Div([
                        dcc.Graph(id="flux-chart", config={"displayModeBar": False})
                    ], className="chart-card"),
                ], className="chart-row"),

                # 第二排: 请求数 + 命中率
                html.Div([
                    html.Div([
                        dcc.Graph(id="requests-chart", config={"displayModeBar": False})
                    ], className="chart-card"),
                    html.Div([
                        dcc.Graph(id="hitrate-chart", config={"displayModeBar": False})
                    ], className="chart-card"),
                ], className="chart-row"),

                # HTTP 状态码
                html.Div([
                    html.Div([
                        dcc.Graph(id="http-status-chart", config={"displayModeBar": False})
                    ], className="chart-card"),
                    html.Div([
                        dcc.Graph(id="bs-http-status-chart", config={"displayModeBar": False})
                    ], className="chart-card"),
                ], className="chart-row"),

                # 域名排行
                html.Div([
                    dcc.Graph(id="domain-ranking-chart", config={"displayModeBar": False})
                ], className="chart-card"),

                # 回源分析
                html.Div([
                    dcc.Graph(id="origin-analysis-chart", config={"displayModeBar": False})
                ], className="chart-card"),

                # 数据表格
                html.Div([
                    html.H3("详细数据"),
                    dash_table.DataTable(
                        id="data-table",
                        columns=[
                            {"name": "时间", "id": "timestamp"},
                            {"name": "域名", "id": "domain"},
                            {"name": "带宽 (Mbps)", "id": "bw_mbps", "type": "numeric", "format": {"specifier": ",.0f"}},
                            {"name": "流量 (GB)", "id": "flux_gb", "type": "numeric", "format": {"specifier": ",.2f"}},
                            {"name": "请求数", "id": "req_num", "type": "numeric", "format": {"specifier": ","}},
                            {"name": "命中率 (%)", "id": "hit_rate", "type": "numeric", "format": {"specifier": ".1f"}},
                            {"name": "回源数", "id": "bs_num", "type": "numeric", "format": {"specifier": ","}},
                            {"name": "回源失败", "id": "bs_fail_num", "type": "numeric", "format": {"specifier": ","}},
                        ],
                        data=[],
                        page_size=12,
                        style_table={"overflowX": "auto"},
                        style_cell={
                            "textAlign": "left",
                            "padding": "12px 16px",
                            "fontFamily": "Inter, sans-serif",
                            "fontSize": "13px",
                            "border": "none",
                            "borderBottom": "1px solid #f1f5f9",
                        },
                        style_header={
                            "backgroundColor": "#f8fafc",
                            "color": "#64748b",
                            "fontWeight": "600",
                            "fontSize": "12px",
                            "textTransform": "uppercase",
                            "letterSpacing": "0.5px",
                            "border": "none",
                            "borderBottom": "2px solid #e2e8f0",
                        },
                        style_data_conditional=[
                            {"if": {"row_index": "odd"}, "backgroundColor": "#fafafa"},
                            {"if": {"state": "active"}, "backgroundColor": "#eff6ff", "border": "1px solid #bfdbfe"},
                        ],
                        sort_action="native",
                        filter_action="native",
                    )
                ], className="chart-card"),

            ], className="content-area"),
        ], className="main-area"),
    ], className="app-shell")

    # 注册回调 - 主数据更新
    @app.callback(
        [
            Output("header-info", "children"),
            Output("summary-cards", "children"),
            Output("refresh-status", "children"),
            Output("bandwidth-chart", "figure"),
            Output("flux-chart", "figure"),
            Output("requests-chart", "figure"),
            Output("hitrate-chart", "figure"),
            Output("http-status-chart", "figure"),
            Output("bs-http-status-chart", "figure"),
            Output("domain-ranking-chart", "figure"),
            Output("origin-analysis-chart", "figure"),
            Output("data-table", "data"),
        ],
        [
            Input("start-datetime", "value"),
            Input("end-datetime", "value"),
            Input("domain-filter", "value"),
            Input("refresh-interval", "n_intervals")
        ]
    )
    def update_all(start_datetime, end_datetime, selected_domain, n_intervals):
        """定时刷新 + 筛选条件更新所有图表"""
        # 转换日期时间字符串为时间戳（毫秒）
        # datetime-local 格式: YYYY-MM-DDTHH:MM:SS 或 YYYY-MM-DDTHH:MM
        try:
            if start_datetime:
                dt_str = start_datetime.strip().replace("T", " ")
                try:
                    start_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    try:
                        start_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
                    except ValueError:
                        start_dt = datetime.strptime(dt_str[:10], "%Y-%m-%d")
                start_time = int(start_dt.timestamp() * 1000)
            else:
                start_time = None

            if end_datetime:
                dt_str = end_datetime.strip().replace("T", " ")
                try:
                    end_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    try:
                        end_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
                    except ValueError:
                        end_dt = datetime.strptime(dt_str[:10], "%Y-%m-%d")
                        end_dt = end_dt.replace(hour=23, minute=59, second=59)
                end_time = int(end_dt.timestamp() * 1000)
            else:
                end_time = None

            # 从 SQLite 加载数据
            records = load_data_from_sqlite(
                storage,
                start_time=start_time,
                end_time=end_time,
                domain=selected_domain
            )
            df = process_data(records)
        except Exception as e:
            print(f"[错误] 加载数据失败: {e}")
            import traceback
            traceback.print_exc()
            # 返回空状态
            return (
                "数据加载失败", html.Div(), f"错误: {e}",
                {}, {}, {}, {}, {}, {}, {}, {}, []
            )

        # 处理空数据情况
        if df.empty:
            return (
                "暂无数据", html.Div(), "无数据",
                {}, {}, {}, {}, {}, {}, {}, {}, []
            )

        # 更新头部信息
        header_info = f"数据范围: {df['timestamp'].min().strftime('%Y-%m-%d %H:%M')} - {df['timestamp'].max().strftime('%Y-%m-%d %H:%M')} · {len(df)} 条记录 · {df['domain'].nunique()} 个域名"

        # 更新汇总卡片
        summary = create_summary_cards(df)

        # 刷新状态
        refresh_time = datetime.now().strftime("%H:%M:%S")
        refresh_status = f"上次刷新: {refresh_time} · 每 {REFRESH_INTERVAL_MS // 1000} 秒自动更新"

        # 数据已在 SQL 层过滤，直接使用
        filtered_df = df

        # 聚合数据
        time_agg = filtered_df.groupby("batch").agg({
            "bw_mbps": "sum", "flux_gb": "sum", "req_num": "sum",
            "hit_num": "sum", "bs_num": "sum", "hit_rate": "mean", "timestamp": "first"
        }).reset_index()

        # 1. 请求带宽趋势
        bw_fig = go.Figure()
        bw_fig.add_trace(go.Scatter(
            x=time_agg["timestamp"], y=time_agg["bw_mbps"],
            name="带宽", fill="tozeroy",
            line={"color": COLORS["primary"], "width": 2},
            fillcolor="rgba(14, 165, 233, 0.08)"
        ))
        bw_fig = apply_chart_style(bw_fig, "请求带宽")
        bw_fig.update_yaxes(title_text="带宽 (Mbps)", title_font={"size": 11})

        # 2. 请求流量趋势
        flux_fig = go.Figure()
        flux_fig.add_trace(go.Scatter(
            x=time_agg["timestamp"], y=time_agg["flux_gb"],
            name="流量", fill="tozeroy",
            line={"color": COLORS["success"], "width": 2},
            fillcolor="rgba(16, 185, 129, 0.1)"
        ))
        flux_fig = apply_chart_style(flux_fig, "请求流量")
        flux_fig.update_yaxes(title_text="流量 (GB)", title_font={"size": 11})

        # 3. 请求数趋势
        req_fig = go.Figure()
        req_fig.add_trace(go.Scatter(
            x=time_agg["timestamp"], y=time_agg["req_num"],
            name="请求数", fill="tozeroy",
            line={"color": COLORS["purple"], "width": 2},
            fillcolor="rgba(139, 92, 246, 0.1)"
        ))
        req_fig = apply_chart_style(req_fig, "请求数")
        req_fig.update_yaxes(title_text="请求数 (个)", title_font={"size": 11})

        # 4. 命中率趋势
        hitrate_fig = go.Figure()
        hitrate_fig.add_trace(go.Scatter(
            x=time_agg["timestamp"], y=time_agg["hit_rate"],
            mode="lines+markers", name="命中率",
            line={"color": COLORS["warning"], "width": 2},
            marker={"size": 4, "color": COLORS["warning"]}
        ))
        hitrate_fig.add_hline(
            y=90, line_dash="dash", line_color=COLORS["text_muted"],
            annotation_text="目标 90%", annotation_font_size=11, annotation_font_color=COLORS["text_muted"]
        )
        hitrate_fig = apply_chart_style(hitrate_fig, "缓存命中率")
        hitrate_fig.update_yaxes(range=[80, 100])

        # 4. HTTP 状态码分布
        http_totals = {
            "2xx": filtered_df["http_2xx"].sum(),
            "3xx": filtered_df["http_3xx"].sum(),
            "4xx": filtered_df["http_4xx"].sum(),
            "5xx": filtered_df["http_5xx"].sum(),
        }
        http_fig = go.Figure(data=[go.Pie(
            labels=list(http_totals.keys()),
            values=list(http_totals.values()),
            hole=0.6,
            marker_colors=[HTTP_COLORS[k] for k in http_totals.keys()],
            textinfo="percent",
            textfont={"size": 12, "color": "#ffffff"},
            hovertemplate="<b>%{label}</b><br>%{value:,} 次<br>%{percent}<extra></extra>"
        )])
        http_fig = apply_chart_style(http_fig, "HTTP 状态码分布")
        http_fig.update_layout(showlegend=True, legend={"orientation": "v", "x": 1, "y": 0.5})

        # 5. 回源 HTTP 状态码分布
        bs_http_totals = {
            "2xx": filtered_df["bs_http_2xx"].sum(),
            "3xx": filtered_df["bs_http_3xx"].sum(),
            "4xx": filtered_df["bs_http_4xx"].sum(),
            "5xx": filtered_df["bs_http_5xx"].sum(),
        }
        bs_http_fig = go.Figure(data=[go.Pie(
            labels=list(bs_http_totals.keys()),
            values=list(bs_http_totals.values()),
            hole=0.6,
            marker_colors=[HTTP_COLORS[k] for k in bs_http_totals.keys()],
            textinfo="percent",
            textfont={"size": 12, "color": "#ffffff"},
            hovertemplate="<b>%{label}</b><br>%{value:,} 次<br>%{percent}<extra></extra>"
        )])
        bs_http_fig = apply_chart_style(bs_http_fig, "回源状态码分布")
        bs_http_fig.update_layout(showlegend=True, legend={"orientation": "v", "x": 1, "y": 0.5})

        # 6. 域名流量排行
        domain_agg = filtered_df.groupby("domain").agg({
            "flux_gb": "sum", "req_num": "sum", "hit_rate": "mean"
        }).reset_index().sort_values("flux_gb", ascending=True).tail(10)

        domain_fig = go.Figure(go.Bar(
            x=domain_agg["flux_gb"],
            y=domain_agg["domain"],
            orientation="h",
            marker_color=COLORS["primary"],
            marker_line_width=0,
            text=[f"{v:.1f} GB" for v in domain_agg["flux_gb"]],
            textposition="outside",
            textfont={"size": 11, "color": COLORS["text_secondary"]},
            hovertemplate="<b>%{y}</b><br>流量: %{x:.2f} GB<extra></extra>"
        ))
        domain_fig = apply_chart_style(domain_fig, "域名流量排行 (Top 10)")
        domain_fig.update_layout(showlegend=False, margin={"l": 140})

        # 7. 回源分析
        origin_agg = filtered_df.groupby("batch").agg({
            "bs_bw_mbps": "sum", "bs_flux_gb": "sum", "bs_fail_num": "sum", "timestamp": "first"
        }).reset_index()

        origin_fig = make_subplots(specs=[[{"secondary_y": True}]])
        origin_fig.add_trace(
            go.Scatter(
                x=origin_agg["timestamp"], y=origin_agg["bs_bw_mbps"],
                name="回源带宽", fill="tozeroy",
                line={"color": COLORS["warning"], "width": 2},
                fillcolor="rgba(245, 158, 11, 0.1)"
            ), secondary_y=False
        )
        origin_fig.add_trace(
            go.Bar(
                x=origin_agg["timestamp"], y=origin_agg["bs_fail_num"],
                name="失败数", marker_color=COLORS["danger"], opacity=0.8, marker_line_width=0
            ), secondary_y=True
        )
        origin_fig = apply_chart_style(origin_fig, "回源带宽与失败分析")
        origin_fig.update_yaxes(title_text="回源带宽 (Mbps)", secondary_y=False, title_font={"size": 11})
        origin_fig.update_yaxes(title_text="失败数", secondary_y=True, title_font={"size": 11})

        # 表格数据
        table_data = filtered_df.copy()
        table_data["timestamp"] = pd.to_datetime(table_data["timestamp"]).dt.strftime("%H:%M:%S")

        return (
            header_info,
            summary,
            refresh_status,
            bw_fig, flux_fig, req_fig, hitrate_fig,
            http_fig, bs_http_fig,
            domain_fig, origin_fig,
            table_data.to_dict("records")
        )

    # 快捷时间范围按钮回调
    @app.callback(
        [
            Output("start-datetime", "value"),
            Output("end-datetime", "value"),
            Output("range-24h", "className"),
            Output("range-7d", "className"),
            Output("range-30d", "className"),
            Output("range-custom", "className"),
        ],
        [
            Input("range-24h", "n_clicks"),
            Input("range-7d", "n_clicks"),
            Input("range-30d", "n_clicks"),
            Input("range-custom", "n_clicks"),
        ],
        prevent_initial_call=True
    )
    def update_time_range(n_24h, n_7d, n_30d, n_custom):
        ctx = dash.callback_context
        if not ctx.triggered:
            return dash.no_update, dash.no_update, "time-range-btn", "time-range-btn", "time-range-btn active", "time-range-btn"
        btn_id = ctx.triggered[0]["prop_id"].split(".")[0]

        base = "time-range-btn"
        classes = [base, base, base, base]
        btn_map = {"range-24h": 0, "range-7d": 1, "range-30d": 2, "range-custom": 3}
        classes[btn_map[btn_id]] = base + " active"

        now = datetime.now()
        end_val = now.strftime("%Y-%m-%dT%H:%M:%S")

        if btn_id == "range-24h":
            start_val = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
        elif btn_id == "range-7d":
            start_val = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
        elif btn_id == "range-30d":
            start_val = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
        else:  # custom
            return dash.no_update, dash.no_update, *classes

        return start_val, end_val, *classes

    return app


def run_dashboard(host="0.0.0.0", port=8050, debug=False, data_file=None):
    """运行仪表板"""
    app = create_app(data_file)

    # 获取存储信息
    storage = get_storage()
    record_count = storage.get_record_count()
    min_time, max_time = storage.get_time_range()

    # 获取认证状态
    auth_config = get_auth_config()

    print("\n" + "=" * 60)
    print("  CDN Panel Dashboard")
    print("=" * 60)
    print(f"  数据存储: SQLite")
    print(f"  记录数量: {record_count:,} 条")
    if min_time and max_time:
        min_dt = datetime.fromtimestamp(min_time / 1000).strftime("%Y-%m-%d %H:%M")
        max_dt = datetime.fromtimestamp(max_time / 1000).strftime("%Y-%m-%d %H:%M")
        print(f"  数据范围: {min_dt} - {max_dt}")
    print("-" * 60)
    if auth_config["enabled"]:
        print(f"  登录认证: 已启用")
        print(f"  用户列表: {', '.join(auth_config['users'].keys())}")
    else:
        print(f"  登录认证: 未启用 (设置 DASHBOARD_PASSWORD 环境变量启用)")
    print("=" * 60)
    print(f"  访问地址: http://127.0.0.1:{port}")
    print("=" * 60 + "\n")

    app.run(debug=debug, host=host, port=port)


if __name__ == "__main__":
    run_dashboard()
