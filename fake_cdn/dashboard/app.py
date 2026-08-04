#!/usr/bin/env python3
"""
CDN 推送数据可视化面板
基于 Dash + Plotly 构建
使用 SQLite 存储提升性能
"""

import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Asia/Shanghai")
except ImportError:
    # Python 3.8 fallback
    LOCAL_TZ = timezone(timedelta(hours=8))

import dash
from dash import dcc, html, dash_table
from dash.dependencies import Input, Output
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
from flask import session, redirect, request, Response

from ..core.config_manager import ConfigManager
from ..core.storage import CDNLogStorage, get_default_storage
from .config_page import (
    create_config_audit_page,
    create_config_page,
    register_config_callbacks,
)


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


def _build_where(start_time, end_time, domain, project):
    """构造所有 analytics SQL 共用的 WHERE 子句，保证三页过滤口径一致"""
    where = ["interval > 0"]
    params: list = []
    if start_time:
        where.append("start_time >= ?")
        params.append(start_time)
    if end_time:
        where.append("start_time <= ?")
        params.append(end_time)
    if domain and domain != "all":
        where.append("domain = ?")
        params.append(domain)
    if project and project != "all":
        where.append("project = ?")
        params.append(project)
    return " AND ".join(where), params


def _load_aggregate_meta(storage, start_time=None, end_time=None, domain=None, project=None):
    """单条 SQL 返回时段+过滤条件下的全部聚合指标，供概览与分析页共享"""
    where_sql, params = _build_where(start_time, end_time, domain, project)
    with storage._get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS total_records,
                COUNT(DISTINCT domain) AS domain_count,
                MIN(start_time) AS min_time,
                MAX(start_time) AS max_time,
                SUM(bw) AS total_bw,
                SUM(flux) AS total_flux,
                SUM(req_num) AS total_requests,
                SUM(hit_num) AS total_hits,
                SUM(bs_num) AS total_bs,
                SUM(bs_fail_num) AS total_bs_fail,
                SUM(http_code_2xx) AS c2, SUM(http_code_3xx) AS c3,
                SUM(http_code_4xx) AS c4, SUM(http_code_5xx) AS c5,
                SUM(bs_http_code_2xx) AS bs2, SUM(bs_http_code_3xx) AS bs3,
                SUM(bs_http_code_4xx) AS bs4, SUM(bs_http_code_5xx) AS bs5
            FROM cdn_logs
            WHERE {where_sql}
            """,
            params,
        ).fetchone()
    return dict(row) if row else {}


def _load_peak_bps(storage, start_time=None, end_time=None, domain=None, project=None):
    """峰值带宽（bps）：按 start_time 分组求瞬时总带宽，取最大"""
    where_sql, params = _build_where(start_time, end_time, domain, project)
    with storage._get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT MAX(total_bps) AS peak_bps FROM (
                SELECT SUM(bw * 1.0 / interval) AS total_bps
                FROM cdn_logs WHERE {where_sql}
                GROUP BY start_time
            )
            """,
            params,
        ).fetchone()
    return (row["peak_bps"] or 0) if row else 0


def _load_daily_stats(storage, start_time=None, end_time=None, domain=None, project=None):
    """按自然日（Asia/Shanghai）聚合：日流量/日请求/日命中率/日峰值带宽"""
    where_sql, params = _build_where(start_time, end_time, domain, project)
    # SQLite 的 date() 支持 unixepoch 转换；+8 hours 对齐 Asia/Shanghai
    with storage._get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT d, SUM(flux) AS flux, SUM(req) AS req,
                   SUM(hit) AS hit, SUM(bs_num) AS bs_num, SUM(bs_fail) AS bs_fail,
                   MAX(bps) AS peak_bps
            FROM (
                SELECT date(start_time/1000, 'unixepoch', '+8 hours') AS d,
                       start_time,
                       SUM(flux) AS flux,
                       SUM(req_num) AS req,
                       SUM(hit_num) AS hit,
                       SUM(bs_num) AS bs_num,
                       SUM(bs_fail_num) AS bs_fail,
                       SUM(bw * 1.0 / interval) AS bps
                FROM cdn_logs WHERE {where_sql}
                GROUP BY start_time
            )
            GROUP BY d
            ORDER BY d
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def load_analytics_data(storage, start_time=None, end_time=None, domain=None, project=None):
    """SQL 层聚合：一次调用拿到 analytics 页所需的全部数据

    只加载已聚合的小结果集，而不是全部原始记录。对百万级数据至关重要。
    返回：
        time_rows: 每个 start_time 一行，已聚合
        http: 单行 dict，含 http/bs_http 各状态码总数、总记录数等元数据
        domains: Top 10 域名流量排行
        table: 最近 500 条明细
    """
    where_sql, params = _build_where(start_time, end_time, domain, project)
    # meta 单独走共享函数（供概览页同步使用，保证计算口径一致）
    meta = _load_aggregate_meta(storage, start_time, end_time, domain, project)

    with storage._get_conn() as conn:
        # 1. 时间点聚合（按 start_time 分组）
        time_rows = conn.execute(
            f"""
            SELECT start_time,
                   SUM(bw * 1.0 / interval) AS bps,
                   SUM(bs_bw * 1.0 / interval) AS bs_bps,
                   SUM(flux) AS flux,
                   SUM(bs_flux) AS bs_flux,
                   SUM(req_num) AS req_num,
                   SUM(hit_num) AS hit_num,
                   SUM(bs_num) AS bs_num,
                   SUM(bs_fail_num) AS bs_fail_num
            FROM cdn_logs
            WHERE {where_sql}
            GROUP BY start_time
            ORDER BY start_time
            """,
            params,
        ).fetchall()

        # 3. 域名流量排行 Top 10
        domain_rows = conn.execute(
            f"""
            SELECT domain, SUM(flux) AS flux, SUM(req_num) AS req_num
            FROM cdn_logs
            WHERE {where_sql}
            GROUP BY domain
            ORDER BY flux DESC
            LIMIT 10
            """,
            params,
        ).fetchall()

        # 4. 最近 500 条明细
        table_rows = conn.execute(
            f"""
            SELECT start_time, domain, bw, interval, flux, req_num,
                   hit_num, bs_num, bs_fail_num
            FROM cdn_logs
            WHERE {where_sql}
            ORDER BY start_time DESC
            LIMIT 500
            """,
            params,
        ).fetchall()

    return {
        "time_rows": [dict(r) for r in time_rows],
        "meta": meta,
        "domains": [dict(r) for r in domain_rows],
        "table": [dict(r) for r in table_rows],
    }


def process_data(records):
    """处理数据为 DataFrame (向量化实现)"""
    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)

    # 缺失列填 0 并数值化
    numeric_cols = [
        "start_time", "interval", "bw", "flux", "bs_bw", "bs_flux",
        "req_num", "hit_num", "bs_num", "bs_fail_num", "hit_flux",
        "http_code_2xx", "http_code_3xx", "http_code_4xx", "http_code_5xx",
        "bs_http_code_2xx", "bs_http_code_3xx", "bs_http_code_4xx", "bs_http_code_5xx",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        else:
            df[col] = 0

    interval = df["interval"].replace(0, 300)
    df["timestamp"] = pd.to_datetime(df["start_time"], unit="ms")
    df["batch"] = df["start_time"]
    df["bw_mbps"] = df["bw"] / interval / 1_000_000
    df["flux_gb"] = df["flux"] / (1024 ** 3)
    df["bs_bw_mbps"] = df["bs_bw"] / interval / 1_000_000
    df["bs_flux_gb"] = df["bs_flux"] / (1024 ** 3)
    df["hit_flux_gb"] = df["hit_flux"] / (1024 ** 3)
    df["http_2xx"] = df["http_code_2xx"]
    df["http_3xx"] = df["http_code_3xx"]
    df["http_4xx"] = df["http_code_4xx"]
    df["http_5xx"] = df["http_code_5xx"]
    df["bs_http_2xx"] = df["bs_http_code_2xx"]
    df["bs_http_3xx"] = df["bs_http_code_3xx"]
    df["bs_http_4xx"] = df["bs_http_code_4xx"]
    df["bs_http_5xx"] = df["bs_http_code_5xx"]

    # 命中率与回源失败率，按位向量化，避免除零
    df["hit_rate"] = (df["hit_num"] / df["req_num"].where(df["req_num"] > 0) * 100).fillna(0)
    df["bs_fail_rate"] = (df["bs_fail_num"] / df["bs_num"].where(df["bs_num"] > 0) * 100).fillna(0)

    return df


def _resolve_end_time_ms(storage):
    """统一的"当前"基准时间：若数据最新时间晚于 1 天前则取现在；否则取 max_time

    让概览（日/周/月）和分析页（24h/7d/30d）使用完全相同的时间窗口锚点，
    保证"同一时间段"下三页数字可校验对齐。
    """
    now_ms = int(datetime.now(tz=LOCAL_TZ).timestamp() * 1000)
    _, max_time = storage.get_time_range()
    if max_time and (now_ms - max_time) > 86400 * 1000:
        return max_time
    return now_ms


def get_default_date_range(storage: CDNLogStorage):
    """获取默认日期范围：以统一基准时间向前推 7 天，对齐概览默认"周" """
    min_time, max_time = storage.get_time_range()
    if min_time is None or max_time is None:
        now = datetime.now(tz=LOCAL_TZ)
        return now.date(), now.date()

    end_ms = _resolve_end_time_ms(storage)
    end_dt = datetime.fromtimestamp(end_ms / 1000, tz=LOCAL_TZ)
    start_dt = end_dt - timedelta(days=7)
    return start_dt.date(), end_dt.date()


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


def create_summary_cards_from_time_df(time_df, meta):
    """基于 SQL 聚合后的 time_df 构建汇总卡片（省去扫全量记录）"""
    peak_bw = time_df["bw_mbps"].max()
    avg_bw = time_df["bw_mbps"].mean()

    time_df = time_df.copy()
    time_df["date"] = time_df["timestamp"].dt.date

    def calc_95_billing(bw_series):
        sorted_bw = bw_series.sort_values(ascending=True).values
        n = len(sorted_bw)
        idx = max(0, min(n - 1, int(n * 0.95) - 1))
        return sorted_bw[idx]

    daily_stats = time_df.groupby("date").agg({"bw_mbps": ["mean", calc_95_billing]})
    daily_stats.columns = ["daily_avg", "daily_p95"]
    daily_avg_bw = daily_stats["daily_avg"].mean()
    daily_p95_bw = daily_stats["daily_p95"].mean()

    total_flux = time_df["flux_gb"].sum()
    total_hits = time_df["hit_num"].sum()
    total_req = time_df["req_num"].sum()
    avg_hit_rate = (total_hits / total_req * 100) if total_req > 0 else 0

    # 趋势：按时间分前后两半比较
    mid = time_df["start_time"].quantile(0.5)
    first = time_df[time_df["start_time"] <= mid]
    second = time_df[time_df["start_time"] > mid]

    def trend(f, s, col, agg="sum"):
        if f.empty or s.empty:
            return None
        v1 = f[col].sum() if agg == "sum" else f[col].mean()
        v2 = s[col].sum() if agg == "sum" else s[col].mean()
        if v1 == 0:
            return None
        return ((v2 / v1) - 1) * 100

    def hit_trend(f, s):
        if f.empty or s.empty:
            return None
        r1 = f["hit_num"].sum() / f["req_num"].sum() if f["req_num"].sum() > 0 else 0
        r2 = s["hit_num"].sum() / s["req_num"].sum() if s["req_num"].sum() > 0 else 0
        if r1 == 0:
            return None
        return ((r2 / r1) - 1) * 100

    bw_trend = trend(first, second, "bw_mbps", "mean")
    flux_trend = trend(first, second, "flux_gb")
    h_trend = hit_trend(first, second)

    return html.Div([
        create_metric_card("峰值带宽", f"{peak_bw/1000:.1f} Gbps", f"平均 {avg_bw/1000:.1f} Gbps", trend=bw_trend),
        create_metric_card("日平均带宽", f"{daily_avg_bw/1000:.1f} Gbps", "每日平均值"),
        create_metric_card("日95带宽", f"{daily_p95_bw/1000:.1f} Gbps", "95th 分位计费带宽", COLORS["primary"]),
        create_metric_card("总流量", f"{total_flux:.1f} GB", "累计传输流量", trend=flux_trend),
        create_metric_card("缓存命中率", f"{avg_hit_rate:.1f}%", "平均命中比例",
                          COLORS["success"] if avg_hit_rate >= 90 else COLORS["warning"], trend=h_trend),
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

            /* ===== 页面通用 ===== */
            .page-header {
                display: flex;
                align-items: center;
                justify-content: space-between;
                margin-bottom: 20px;
            }
            .page-header h2 {
                font-size: 22px;
                font-weight: 700;
                margin: 0 0 4px 0;
                color: #0f172a;
            }
            .page-header p {
                font-size: 13px;
                color: #64748b;
                margin: 0;
            }

            .status-badge {
                display: inline-flex;
                align-items: center;
                gap: 4px;
                padding: 2px 10px;
                font-size: 12px;
                font-weight: 500;
                border-radius: 4px;
            }
            .status-badge.active { color: #059669; background: #ecfdf5; }
            .status-badge.pending { color: #d97706; background: #fef3c7; }
            .status-badge.warning { color: #dc2626; background: #fef2f2; }
            .status-badge.info { color: #0369a1; background: #e0f2fe; }

            .btn-primary {
                padding: 7px 14px;
                font-size: 13px;
                font-weight: 500;
                color: #fff;
                background: #0ea5e9;
                border: none;
                border-radius: 6px;
                cursor: pointer;
                font-family: inherit;
            }
            .btn-primary:hover { background: #0284c7; }

            .btn-ghost {
                padding: 6px 12px;
                font-size: 13px;
                color: #475569;
                background: #fff;
                border: 1px solid #e2e8f0;
                border-radius: 6px;
                cursor: pointer;
                font-family: inherit;
            }
            .btn-ghost:hover { background: #f8fafc; }

            /* 切换开关 */
            .toggle-row {
                display: flex;
                align-items: center;
                justify-content: space-between;
                padding: 14px 0;
                border-bottom: 1px solid #f1f5f9;
            }
            .toggle-row:last-child { border-bottom: none; }
            .toggle-info .toggle-title {
                font-size: 14px;
                font-weight: 500;
                color: #0f172a;
            }
            .toggle-info .toggle-desc {
                font-size: 12px;
                color: #64748b;
                margin-top: 2px;
            }
            .toggle-switch {
                width: 36px;
                height: 20px;
                background: #cbd5e1;
                border-radius: 10px;
                position: relative;
                flex-shrink: 0;
            }
            .toggle-switch.on { background: #0ea5e9; }
            .toggle-switch::after {
                content: '';
                width: 16px;
                height: 16px;
                background: #fff;
                border-radius: 50%;
                position: absolute;
                top: 2px;
                left: 2px;
                transition: left 0.2s;
            }
            .toggle-switch.on::after { left: 18px; }

            /* 日志流 */
            .log-stream {
                font-family: 'SF Mono', Monaco, Consolas, monospace;
                font-size: 12px;
                max-height: 480px;
                overflow-y: auto;
            }
            .log-line {
                padding: 6px 0;
                border-bottom: 1px solid #f8fafc;
                color: #475569;
                display: flex;
                gap: 12px;
                align-items: center;
            }
            .log-time { color: #94a3b8; min-width: 70px; }
            .log-ip { color: #334155; min-width: 120px; }
            .log-method { color: #0369a1; font-weight: 600; min-width: 50px; }
            .log-path { color: #475569; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
            .log-status-2xx { color: #059669; font-weight: 600; min-width: 40px; }
            .log-status-3xx { color: #0369a1; font-weight: 600; min-width: 40px; }
            .log-status-4xx { color: #d97706; font-weight: 600; min-width: 40px; }
            .log-status-5xx { color: #dc2626; font-weight: 600; min-width: 40px; }
            .log-latency { color: #94a3b8; min-width: 60px; text-align: right; }

            /* 节点网格 */
            .node-grid {
                display: grid;
                grid-template-columns: repeat(3, 1fr);
                gap: 12px;
            }
            .node-card {
                border: 1px solid #e2e8f0;
                border-radius: 8px;
                padding: 14px;
                display: flex;
                align-items: center;
                gap: 12px;
            }
            .node-region {
                font-size: 14px;
                font-weight: 600;
                color: #0f172a;
            }
            .node-meta {
                font-size: 12px;
                color: #64748b;
                margin-top: 2px;
            }

            /* 时间线 */
            .timeline-item {
                display: flex;
                gap: 12px;
                padding: 10px 0;
                border-bottom: 1px solid #f1f5f9;
                font-size: 13px;
                align-items: flex-start;
            }
            .timeline-item:last-child { border-bottom: none; }
            .timeline-time {
                color: #94a3b8;
                min-width: 90px;
                font-size: 12px;
            }
            .timeline-icon {
                width: 6px;
                height: 6px;
                border-radius: 50%;
                margin-top: 7px;
                flex-shrink: 0;
            }
            .timeline-icon.info { background: #0ea5e9; }
            .timeline-icon.success { background: #10b981; }
            .timeline-icon.warning { background: #f59e0b; }
            .timeline-desc { color: #334155; }

            /* QPS 大数字 */
            .qps-number {
                font-size: 56px;
                font-weight: 700;
                color: #0ea5e9;
                letter-spacing: -2px;
                font-variant-numeric: tabular-nums;
                line-height: 1;
            }
            .qps-label {
                font-size: 13px;
                color: #64748b;
                margin-top: 6px;
            }

            /* 侧边栏链接 */
            a.sidebar-item {
                text-decoration: none;
                cursor: pointer;
            }

            /* 路由容器：让内部 sidebar 能撑满高度 */
            #sidebar-container { display: flex; flex-shrink: 0; }

            /* 代码/URL 样式 */
            .mono {
                font-family: 'SF Mono', Monaco, Consolas, monospace;
                font-size: 12px;
                color: #334155;
            }

            /* ===== 打印样式（导出 PDF 专用） ===== */
            @media print {
                body, .app-shell, .main-area { height: auto !important; overflow: visible !important; }
                .sidebar, #sidebar-container,
                .topbar, #topbar-container,
                #report-toolbar,
                .time-range-bar,
                .filter-bar {
                    display: none !important;
                }
                .content-area { padding: 0 !important; overflow: visible !important; }
                .chart-card, .metric-card {
                    page-break-inside: avoid;
                    box-shadow: none !important;
                }
                .chart-row { page-break-inside: avoid; }
                .metrics-grid { page-break-inside: avoid; }
                .page-header { page-break-after: avoid; }
                a, button { color: inherit !important; }
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
REFRESH_INTERVAL_MS = 60 * 1000  # 60秒


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


# 路由路径 → 菜单标题
PAGE_TITLES = {
    "/": "概览",
    "/overview": "概览",
    "/analytics": "流量分析",
    "/report": "月度报告",
    "/config": "配置管理",
    "/config-audit": "审计日志",
    "/domains": "域名管理",
    "/dns": "DNS 记录",
    "/cache": "缓存",
    "/security": "安全防护",
    "/ssl": "SSL/TLS",
    "/waf": "WAF 防火墙",
    "/speed": "性能优化",
    "/rules": "流量规则",
    "/loadbalancer": "负载均衡",
}


def create_sidebar(current_path="/overview"):
    """创建侧边栏导航，current_path 决定 active 项"""
    # 归一化 /
    if current_path in ("/", ""):
        current_path = "/overview"

    def nav_item(icon, label, href):
        active = (current_path == href)
        cls = "sidebar-item active" if active else "sidebar-item"
        return dcc.Link([
            html.Span(icon, className="material-symbols-outlined"),
            html.Span(label),
        ], href=href, className=cls, refresh=False)

    return html.Div([
        # Logo
        html.Div([
            html.Span("CDN Panel", className="sidebar-logo-text"),
        ], className="sidebar-logo"),

        # 主要导航
        html.Div("主要", className="sidebar-section"),
        nav_item("dashboard", "概览", "/overview"),
        nav_item("analytics", "流量分析", "/analytics"),
        nav_item("summarize", "月度报告", "/report"),
        nav_item("language", "域名管理", "/domains"),
        nav_item("dns", "DNS 记录", "/dns"),

        # 配置中心
        html.Div("配置中心", className="sidebar-section"),
        nav_item("assignment", "配置管理", "/config"),
        nav_item("history", "审计日志", "/config-audit"),

        # CDN 功能配置
        html.Div("CDN 功能", className="sidebar-section"),
        nav_item("cached", "缓存", "/cache"),
        nav_item("shield", "安全防护", "/security"),
        nav_item("lock", "SSL/TLS", "/ssl"),
        nav_item("local_fire_department", "WAF 防火墙", "/waf"),
        nav_item("speed", "性能优化", "/speed"),

        # 网络
        html.Div("网络", className="sidebar-section"),
        nav_item("tune", "流量规则", "/rules"),
        nav_item("cloud_sync", "负载均衡", "/loadbalancer"),

        # 底部状态
        html.Div([
            html.Span(className="status-dot green"),
            html.Span("系统运行正常"),
        ], className="sidebar-footer"),
    ], className="sidebar")


def create_topbar(auth_config, current_path="/overview"):
    """创建顶部信息栏"""
    page_title = PAGE_TITLES.get(current_path, "概览")

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
            html.Strong(page_title, id="breadcrumb-page"),
        ], className="topbar-breadcrumb"),
        html.Span(id="header-info", className="topbar-info"),
        html.Div(right_items, className="topbar-right"),
    ], className="topbar")


# ============================================================================
# 通用 UI 辅助
# ============================================================================
def create_page_header(title, description, action_label=None):
    """统一页面标题区"""
    right = html.Button(action_label, className="btn-primary") if action_label else None
    return html.Div([
        html.Div([
            html.H2(title),
            html.P(description),
        ]),
        right,
    ], className="page-header")


def create_status_badge(text, variant="active"):
    """状态徽章"""
    return html.Span(text, className=f"status-badge {variant}")


def create_toggle_row(title, desc, on=True):
    """切换开关行（纯展示）"""
    return html.Div([
        html.Div([
            html.Div(title, className="toggle-title"),
            html.Div(desc, className="toggle-desc"),
        ], className="toggle-info"),
        html.Div(className=f"toggle-switch {'on' if on else ''}"),
    ], className="toggle-row")


def create_time_controls(default_start_iso, default_end_iso, domains, projects=None):
    """创建时间控制区域。

    default_start_iso / default_end_iso 为精确到秒的 ISO 字符串
    (YYYY-MM-DDTHH:MM:SS)，与概览页"周"范围使用同一基准以保证两页数据
    可对齐。
    """
    projects = projects or []
    return html.Div([
        # 快捷时间按钮行
        html.Div([
            html.Button("最近 24 小时", id="range-24h", className="time-range-btn", n_clicks=0),
            html.Button("最近 7 天", id="range-7d", className="time-range-btn active", n_clicks=0),
            html.Button("最近 30 天", id="range-30d", className="time-range-btn", n_clicks=0),
            html.Button("自定义范围", id="range-custom", className="time-range-btn", n_clicks=0),
            html.Div(className="time-range-spacer"),
            # 项目筛选
            html.Span("项目", className="filter-label"),
            dcc.Dropdown(
                id="project-filter",
                options=[{"label": "全部项目", "value": "all"}] +
                        [{"label": p, "value": p} for p in projects],
                value="all",
                style={"width": "180px"},
                clearable=False
            ),
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
                value=default_start_iso,
                style={"width": "200px", "padding": "6px 10px",
                       "border": "1px solid #e2e8f0", "borderRadius": "6px", "fontSize": "13px"}
            ),
            html.Span("结束", className="filter-label"),
            dcc.Input(
                id="end-datetime",
                type="datetime-local",
                value=default_end_iso,
                style={"width": "200px", "padding": "6px 10px",
                       "border": "1px solid #e2e8f0", "borderRadius": "6px", "fontSize": "13px"}
            ),
        ], className="filter-bar"),
    ])


# ============================================================================
# 表格通用样式
# ============================================================================
TABLE_STYLE_CELL = {
    "textAlign": "left",
    "padding": "12px 16px",
    "fontFamily": "Inter, sans-serif",
    "fontSize": "13px",
    "border": "none",
    "borderBottom": "1px solid #f1f5f9",
}
TABLE_STYLE_HEADER = {
    "backgroundColor": "#f8fafc",
    "color": "#64748b",
    "fontWeight": "600",
    "fontSize": "12px",
    "textTransform": "uppercase",
    "letterSpacing": "0.5px",
    "border": "none",
    "borderBottom": "2px solid #e2e8f0",
    "padding": "10px 16px",
}


def _simple_table(columns, data, page_size=15):
    """构造统一样式的简单表格"""
    return dash_table.DataTable(
        columns=columns,
        data=data,
        page_size=page_size,
        style_table={"overflowX": "auto"},
        style_cell=TABLE_STYLE_CELL,
        style_header=TABLE_STYLE_HEADER,
        style_data_conditional=[
            {"if": {"row_index": "odd"}, "backgroundColor": "#fafafa"},
        ],
        sort_action="native",
    )


# ============================================================================
# 页面: 概览
# ============================================================================
OVERVIEW_RANGE_DAYS = {"day": 1, "week": 7, "month": 30}
OVERVIEW_RANGE_LABELS = {"day": "最近 24 小时", "week": "最近 7 天", "month": "最近 30 天"}


def _compute_overview_metrics(storage, range_key="week"):
    """按日/周/月范围计算概览指标；与分析页共用 _load_aggregate_meta / _load_peak_bps
    保证相同时间段下两页数字完全一致。"""
    days = OVERVIEW_RANGE_DAYS.get(range_key, 7)
    end_ms = _resolve_end_time_ms(storage)
    start_ms = end_ms - days * 86400 * 1000

    meta = _load_aggregate_meta(storage, start_time=start_ms, end_time=end_ms)
    total_flux_bytes = meta.get("total_flux") or 0
    total_requests = meta.get("total_requests") or 0
    active_domains = meta.get("domain_count") or 0
    total_bs = meta.get("total_bs") or 0
    total_bs_fail = meta.get("total_bs_fail") or 0

    peak_bps = _load_peak_bps(storage, start_time=start_ms, end_time=end_ms)

    availability = (1 - total_bs_fail / total_bs) * 100 if total_bs > 0 else None

    return {
        "range_key": range_key,
        "range_label": OVERVIEW_RANGE_LABELS[range_key],
        "start_ms": start_ms,
        "end_ms": end_ms,
        "peak_gbps": peak_bps / 1_000_000_000,
        "total_tb": total_flux_bytes / (1024 ** 4),
        "total_req_m": total_requests / 1_000_000,
        "active_domains": active_domains,
        "availability": availability,
    }


def _humanize_relative(now_dt, ts_ms):
    """返回相对时间描述：刚刚/N 分钟前/N 小时前/N 天前"""
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=LOCAL_TZ)
    sec = int((now_dt - dt).total_seconds())
    if sec < 60:
        return "刚刚"
    if sec < 3600:
        return f"{sec // 60} 分钟前"
    if sec < 86400:
        return f"{sec // 3600} 小时前"
    return f"{sec // 86400} 天前"


# 节点分摊比例 & 节点配置（亚太五地）
_NODE_CONFIG = [
    ("香港",        0.30, 10),
    ("日本 · 东京", 0.25,  8),
    ("新加坡",      0.20,  6),
    ("台湾 · 台北", 0.15,  4),
    ("韩国 · 首尔", 0.10,  3),
]


def _compute_node_cards(storage, start_ms, end_ms):
    """按查询范围计算各节点的实时带宽和健康状态（分摊模型）"""
    meta = _load_aggregate_meta(storage, start_time=start_ms, end_time=end_ms)
    total_bw_bits = meta.get("total_bw") or 0
    total_req = meta.get("total_requests") or 0
    c5 = meta.get("c5") or 0
    err_ratio = (c5 / total_req) if total_req > 0 else 0

    duration_s = max(1, (end_ms - start_ms) / 1000)
    avg_bps = total_bw_bits / duration_s
    avg_gbps = avg_bps / 1_000_000_000

    def status_color(ratio):
        if ratio > 0.02:
            return "red"
        if ratio > 0.005:
            return "yellow"
        return "green"

    cards = []
    for i, (region, share, nodes) in enumerate(_NODE_CONFIG):
        node_gbps = avg_gbps * share
        # 最后一个固定显示维护中，保留视觉多样性
        if i == len(_NODE_CONFIG) - 1:
            color = "yellow"
            meta = f"{nodes} 节点 · 维护中"
        else:
            color = status_color(err_ratio)
            meta = f"{nodes} 节点 · {node_gbps:.2f} Gbps"
        cards.append(html.Div([
            html.Span(className=f"status-dot {color}"),
            html.Div([
                html.Div(region, className="node-region"),
                html.Div(meta, className="node-meta"),
            ]),
        ], className="node-card"))
    return html.Div(cards, className="node-grid")


def _compute_activity_timeline(storage, start_ms, end_ms):
    """基于 SQL 查询生成最近活动时间线（8 条）"""
    now_dt = datetime.now(tz=LOCAL_TZ)
    activities = []

    meta = _load_aggregate_meta(storage, start_time=start_ms, end_time=end_ms)
    total_req = meta.get("total_requests") or 0
    total_hits = meta.get("total_hits") or 0
    domain_count = meta.get("domain_count") or 0

    try:
        with storage._get_conn() as conn:
            # 1. 峰值带宽时刻
            peak_row = conn.execute(
                "SELECT start_time, SUM(bw * 1.0 / interval) AS bps FROM cdn_logs "
                "WHERE interval > 0 AND start_time >= ? AND start_time <= ? "
                "GROUP BY start_time ORDER BY bps DESC LIMIT 1",
                (start_ms, end_ms),
            ).fetchone()
            # 2. 回源失败最多的域名
            fail_row = conn.execute(
                "SELECT domain, SUM(bs_fail_num) AS f FROM cdn_logs "
                "WHERE start_time >= ? AND start_time <= ? "
                "GROUP BY domain ORDER BY f DESC LIMIT 1",
                (start_ms, end_ms),
            ).fetchone()
            # 3. Top 请求域名
            top_req_row = conn.execute(
                "SELECT domain, SUM(req_num) AS r FROM cdn_logs "
                "WHERE start_time >= ? AND start_time <= ? "
                "GROUP BY domain ORDER BY r DESC LIMIT 1",
                (start_ms, end_ms),
            ).fetchone()
            # 4. 项目数
            proj_row = conn.execute(
                "SELECT COUNT(DISTINCT project) AS c FROM cdn_logs "
                "WHERE start_time >= ? AND start_time <= ?",
                (start_ms, end_ms),
            ).fetchone()
    except Exception:
        peak_row = fail_row = top_req_row = proj_row = None

    _, max_time = storage.get_time_range()

    if peak_row and peak_row["bps"]:
        peak_gbps = peak_row["bps"] / 1_000_000_000
        activities.append((
            _humanize_relative(now_dt, peak_row["start_time"]),
            "warning",
            f"突发流量峰值 {peak_gbps:.2f} Gbps 触发告警",
        ))

    if fail_row and fail_row["f"]:
        activities.append((
            "近 24h",
            "warning",
            f"{fail_row['domain']} 回源失败 {int(fail_row['f']):,} 次",
        ))

    if total_req > 0:
        rate = total_hits / total_req * 100
        activities.append(("近 24h", "info", f"平均缓存命中率 {rate:.2f}%"))

    if top_req_row and top_req_row["r"]:
        activities.append((
            "近 24h",
            "info",
            f"请求 Top: {top_req_row['domain']} ({top_req_row['r']/1e6:.2f} M)",
        ))

    if domain_count:
        activities.append(("近 24h", "info", f"活跃域名 {domain_count} 个 · 总请求 {total_req/1e6:.2f} M"))

    if proj_row and proj_row["c"]:
        activities.append(("刚刚", "success", f"活跃项目 {proj_row['c']} 个 · 数据持续推送"))

    if max_time:
        ts_str = datetime.fromtimestamp(max_time / 1000, tz=LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
        activities.append((_humanize_relative(now_dt, max_time), "success", f"最新数据到达 {ts_str}"))

    activities.append(("6 小时前", "success", "SSL 证书自动续期完成"))
    activities.append(("昨天", "success", "全球 31 个节点健康检查通过"))

    items = [
        html.Div([
            html.Span(t, className="timeline-time"),
            html.Span(className=f"timeline-icon {var}"),
            html.Span(desc, className="timeline-desc"),
        ], className="timeline-item")
        for t, var, desc in activities[:8]
    ]
    return html.Div(items)


def _build_overview_cards(metrics):
    """根据 _compute_overview_metrics 的返回值构造卡片 grid"""
    label = metrics["range_label"]
    if metrics["availability"] is not None:
        avail_text = f"{metrics['availability']:.2f}%"
        avail_color = COLORS["success"] if metrics["availability"] >= 99.9 else COLORS["warning"]
    else:
        avail_text = "—"
        avail_color = COLORS["text_muted"]

    return html.Div([
        create_metric_card("峰值带宽", f"{metrics['peak_gbps']:.2f} Gbps", f"{label}峰值"),
        create_metric_card("总流量", f"{metrics['total_tb']:,.2f} TB", f"{label}累计"),
        create_metric_card("总请求", f"{metrics['total_req_m']:,.2f} M", f"{label}累计"),
        create_metric_card("活跃域名", f"{metrics['active_domains']}", f"{label}出现"),
        create_metric_card("可用率", avail_text, f"{label}回源成功率", avail_color),
    ], className="metrics-grid")


def create_overview_page(storage):
    """概览页 (带 日/周/月 筛选 + SQL 层实时聚合)"""
    default_range = "week"
    metrics = _compute_overview_metrics(storage, default_range)
    cards = _build_overview_cards(metrics)

    range_bar = html.Div([
        html.Button("日", id="overview-range-day",
                    className="time-range-btn" + (" active" if default_range == "day" else ""),
                    n_clicks=0),
        html.Button("周", id="overview-range-week",
                    className="time-range-btn" + (" active" if default_range == "week" else ""),
                    n_clicks=0),
        html.Button("月", id="overview-range-month",
                    className="time-range-btn" + (" active" if default_range == "month" else ""),
                    n_clicks=0),
        html.Div(className="time-range-spacer"),
    ], className="time-range-bar")

    node_cards = _compute_node_cards(storage, metrics["start_ms"], metrics["end_ms"])
    timeline = _compute_activity_timeline(storage, metrics["start_ms"], metrics["end_ms"])

    return html.Div([
        create_page_header("概览", "CDN 全局运行状态与关键指标"),
        range_bar,
        html.Div(cards, id="overview-cards"),
        html.Div([
            html.Div([
                html.H3("全球节点状态"),
                html.Div(node_cards, id="overview-nodes"),
            ], className="chart-card"),
            html.Div([
                html.H3("最近活动"),
                html.Div(timeline, id="overview-timeline"),
            ], className="chart-card"),
        ], className="chart-row"),
    ])


# ============================================================================
# 页面: 流量分析（现有功能）
# ============================================================================
def create_analytics_page(default_start_iso, default_end_iso, domains, projects=None):
    """流量分析页（保留现有功能）"""
    return html.Div([
        dcc.Interval(id="refresh-interval", interval=REFRESH_INTERVAL_MS, n_intervals=0),
        create_page_header("流量分析", "带宽、请求、缓存与回源详细指标"),
        create_time_controls(default_start_iso, default_end_iso, domains, projects),
        html.Div(id="summary-cards"),
        html.Div([
            html.Div([dcc.Graph(id="bandwidth-chart", config={"displayModeBar": False})], className="chart-card"),
            html.Div([dcc.Graph(id="flux-chart", config={"displayModeBar": False})], className="chart-card"),
        ], className="chart-row"),
        html.Div([
            html.Div([dcc.Graph(id="requests-chart", config={"displayModeBar": False})], className="chart-card"),
            html.Div([dcc.Graph(id="hitrate-chart", config={"displayModeBar": False})], className="chart-card"),
        ], className="chart-row"),
        html.Div([
            html.Div([dcc.Graph(id="http-status-chart", config={"displayModeBar": False})], className="chart-card"),
            html.Div([dcc.Graph(id="bs-http-status-chart", config={"displayModeBar": False})], className="chart-card"),
        ], className="chart-row"),
        html.Div([
            dcc.Graph(id="domain-ranking-chart", config={"displayModeBar": False})
        ], className="chart-card"),
        html.Div([
            dcc.Graph(id="origin-analysis-chart", config={"displayModeBar": False})
        ], className="chart-card"),
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
                style_cell=TABLE_STYLE_CELL,
                style_header=TABLE_STYLE_HEADER,
                style_data_conditional=[
                    {"if": {"row_index": "odd"}, "backgroundColor": "#fafafa"},
                    {"if": {"state": "active"}, "backgroundColor": "#eff6ff", "border": "1px solid #bfdbfe"},
                ],
                sort_action="native",
                filter_action="native",
            )
        ], className="chart-card"),
    ])


# ============================================================================
# 页面: 域名管理
# ============================================================================
def _build_domain_rows(pairs):
    """根据 (domain, project) 列表生成域名管理表格行（静态 mock 属性，稳定种子）"""
    import random
    cache_modes = ["标准", "激进缓存", "兼容模式"]
    statuses = [("已激活", "active")] * 8 + [("待验证", "pending"), ("告警", "warning")]
    rows = []
    for p in pairs:
        domain = p["domain"]
        project = p["project"]
        # 用域名作为种子，保证每次刷新同一域名属性稳定
        r = random.Random(hash(domain) & 0xFFFFFFFF)
        st_text, _ = r.choice(statuses)
        origin_ip = f"{r.randint(10, 200)}.{r.randint(0, 255)}.{r.randint(0, 255)}.{r.randint(1, 254)}"
        created = f"2025-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}"
        rows.append({
            "project": project,
            "domain": domain,
            "status": st_text,
            "ssl": "已启用" if st_text == "已激活" else "配置中",
            "origin": origin_ip,
            "cache": r.choice(cache_modes),
            "created": created,
        })
    return rows


def create_domains_page(pairs, projects):
    """域名管理页（带项目筛选）"""
    rows = _build_domain_rows(pairs)

    return html.Div([
        create_page_header(
            "域名管理",
            f"管理已接入的 {len(rows)} 个域名",
            action_label="+ 添加域名",
        ),
        html.Div([
            # 顶部项目筛选
            html.Div([
                html.Span("项目", className="filter-label"),
                dcc.Dropdown(
                    id="domain-project-filter",
                    options=[{"label": "全部项目", "value": "all"}] +
                            [{"label": p, "value": p} for p in projects],
                    value="all",
                    style={"width": "240px"},
                    clearable=False,
                ),
            ], style={"display": "flex", "alignItems": "center", "gap": "12px",
                      "marginBottom": "16px"}),
            dash_table.DataTable(
                id="domains-table",
                columns=[
                    {"name": "项目", "id": "project"},
                    {"name": "域名", "id": "domain"},
                    {"name": "状态", "id": "status"},
                    {"name": "SSL", "id": "ssl"},
                    {"name": "源站 IP", "id": "origin"},
                    {"name": "缓存模式", "id": "cache"},
                    {"name": "创建时间", "id": "created"},
                ],
                data=rows,
                page_size=20,
                style_table={"overflowX": "auto"},
                style_cell=TABLE_STYLE_CELL,
                style_header=TABLE_STYLE_HEADER,
                style_data_conditional=[
                    {"if": {"row_index": "odd"}, "backgroundColor": "#fafafa"},
                ],
                sort_action="native",
                filter_action="native",
            ),
        ], className="chart-card"),
    ])


# ============================================================================
# 页面: DNS 记录
# ============================================================================
def create_dns_page(domains):
    """DNS 记录页"""
    import random
    random.seed(7)
    record_types = ["A", "A", "A", "CNAME", "CNAME", "AAAA", "MX", "TXT", "NS"]
    rows = []
    base_domain = sorted(domains)[0] if domains else "example.com"
    # 主域名的典型 DNS 记录
    rows.extend([
        {"type": "A", "name": "@", "value": "47.243.87.148", "ttl": "自动", "proxy": "已代理"},
        {"type": "A", "name": "www", "value": "47.243.87.148", "ttl": "自动", "proxy": "已代理"},
        {"type": "AAAA", "name": "@", "value": "2400:3200::1", "ttl": "自动", "proxy": "已代理"},
        {"type": "CNAME", "name": "cdn", "value": f"{base_domain}.cdn.fakecdn.net", "ttl": "自动", "proxy": "已代理"},
        {"type": "CNAME", "name": "api", "value": f"api-origin.{base_domain}", "ttl": "300", "proxy": "仅 DNS"},
        {"type": "CNAME", "name": "mail", "value": f"mail.{base_domain}", "ttl": "3600", "proxy": "仅 DNS"},
        {"type": "MX", "name": "@", "value": f"10 mail.{base_domain}", "ttl": "3600", "proxy": "仅 DNS"},
        {"type": "TXT", "name": "@", "value": "v=spf1 include:_spf.google.com ~all", "ttl": "3600", "proxy": "仅 DNS"},
        {"type": "TXT", "name": "_dmarc", "value": "v=DMARC1; p=reject; rua=mailto:dmarc@...", "ttl": "3600", "proxy": "仅 DNS"},
        {"type": "NS", "name": "@", "value": "ns1.fakecdn.net", "ttl": "86400", "proxy": "仅 DNS"},
        {"type": "NS", "name": "@", "value": "ns2.fakecdn.net", "ttl": "86400", "proxy": "仅 DNS"},
    ])
    # 为其他几个域名加简单记录
    for d in sorted(domains)[1:8] if domains else []:
        rows.append({"type": "A", "name": d, "value": f"203.0.113.{random.randint(1, 250)}", "ttl": "自动", "proxy": "已代理"})

    table = _simple_table(
        columns=[
            {"name": "类型", "id": "type"},
            {"name": "名称", "id": "name"},
            {"name": "值", "id": "value"},
            {"name": "TTL", "id": "ttl"},
            {"name": "代理状态", "id": "proxy"},
        ],
        data=rows,
        page_size=15,
    )

    return html.Div([
        create_page_header("DNS 记录", "管理域名的 DNS 解析记录", action_label="+ 添加记录"),
        html.Div([table], className="chart-card"),
    ])


# ============================================================================
# 页面: 缓存
# ============================================================================
def create_cache_page():
    """缓存页"""
    cards = html.Div([
        create_metric_card("缓存命中率", "92.8%", "最近 24 小时", COLORS["success"]),
        create_metric_card("缓存对象数", "8.4 M", "当前活跃对象"),
        create_metric_card("节省源站流量", "1.78 TB", "最近 7 天"),
    ], className="metrics-grid", style={"gridTemplateColumns": "repeat(3, 1fr)"})

    purge_card = html.Div([
        html.H3("URL 刷新"),
        html.P("输入 URL 立即清除 CDN 节点缓存，支持通配符 *", style={"fontSize": "13px", "color": "#64748b", "margin": "0 0 12px"}),
        html.Div([
            dcc.Input(
                type="text",
                placeholder="https://example.com/path/*",
                style={"flex": "1", "padding": "8px 12px", "border": "1px solid #e2e8f0",
                       "borderRadius": "6px", "fontSize": "13px", "fontFamily": "inherit"},
            ),
            html.Button("提交刷新", className="btn-primary"),
        ], style={"display": "flex", "gap": "8px"}),
    ], className="chart-card")

    rules = [
        {"pattern": "*.css", "action": "缓存 30 天", "edge_ttl": "30d", "browser_ttl": "4h"},
        {"pattern": "*.js", "action": "缓存 7 天", "edge_ttl": "7d", "browser_ttl": "4h"},
        {"pattern": "*.jpg|*.png|*.webp", "action": "缓存 30 天", "edge_ttl": "30d", "browser_ttl": "1d"},
        {"pattern": "/api/*", "action": "不缓存", "edge_ttl": "-", "browser_ttl": "-"},
        {"pattern": "/admin/*", "action": "绕过缓存", "edge_ttl": "-", "browser_ttl": "no-cache"},
    ]
    rules_table = _simple_table(
        columns=[
            {"name": "匹配模式", "id": "pattern"},
            {"name": "动作", "id": "action"},
            {"name": "边缘 TTL", "id": "edge_ttl"},
            {"name": "浏览器 TTL", "id": "browser_ttl"},
        ],
        data=rules,
        page_size=10,
    )

    prefetch = [
        {"url": "https://appcircle.io/static/bundle.js", "status": "已完成", "progress": "100%", "time": "12:30"},
        {"url": "https://tigerbeetle.com/images/logo.png", "status": "进行中", "progress": "64%", "time": "12:35"},
        {"url": "https://qdrant.tech/*.css", "status": "排队中", "progress": "0%", "time": "12:40"},
    ]
    prefetch_table = _simple_table(
        columns=[
            {"name": "URL", "id": "url"},
            {"name": "状态", "id": "status"},
            {"name": "进度", "id": "progress"},
            {"name": "时间", "id": "time"},
        ],
        data=prefetch,
        page_size=10,
    )

    return html.Div([
        create_page_header("缓存", "缓存命中率、刷新与规则配置"),
        cards,
        purge_card,
        html.Div([html.H3("缓存规则"), rules_table], className="chart-card"),
        html.Div([html.H3("预取任务"), prefetch_table], className="chart-card"),
    ])


# ============================================================================
# 页面: 安全防护
# ============================================================================
def create_security_page():
    """安全防护页"""
    cards = html.Div([
        create_metric_card("24h 已拦截", "12,847", "恶意请求", COLORS["danger"]),
        create_metric_card("可疑请求", "3,291", "标记为可疑"),
        create_metric_card("黑名单 IP", "178", "当前生效"),
        create_metric_card("威胁分数", "中", "整体评估", COLORS["warning"]),
    ], className="metrics-grid", style={"gridTemplateColumns": "repeat(4, 1fr)"})

    events = [
        {"time": "12:45:23", "ip": "203.0.113.42", "type": "SQL 注入", "action": "已拦截", "domain": "api.appcircle.io"},
        {"time": "12:44:11", "ip": "198.51.100.88", "type": "XSS 攻击", "action": "已拦截", "domain": "www.tigerbeetle.com"},
        {"time": "12:42:07", "ip": "192.0.2.154", "type": "暴力破解", "action": "已挑战", "domain": "login.qdrant.tech"},
        {"time": "12:40:55", "ip": "203.0.113.12", "type": "CC 攻击", "action": "已限流", "domain": "api.nube.sh"},
        {"time": "12:39:41", "ip": "198.51.100.3", "type": "路径遍历", "action": "已拦截", "domain": "admin.yxvm.com"},
        {"time": "12:37:28", "ip": "203.0.113.77", "type": "恶意爬虫", "action": "已挑战", "domain": "www.sharon.io"},
        {"time": "12:36:14", "ip": "192.0.2.201", "type": "SQL 注入", "action": "已拦截", "domain": "api.gomami.io"},
        {"time": "12:34:02", "ip": "198.51.100.64", "type": "文件包含", "action": "已拦截", "domain": "www.oks.llc"},
        {"time": "12:31:49", "ip": "203.0.113.93", "type": "CC 攻击", "action": "已限流", "domain": "www.as979.net"},
        {"time": "12:30:22", "ip": "192.0.2.18", "type": "XSS 攻击", "action": "已拦截", "domain": "api.enuoidc.com"},
    ]
    events_table = _simple_table(
        columns=[
            {"name": "时间", "id": "time"},
            {"name": "来源 IP", "id": "ip"},
            {"name": "威胁类型", "id": "type"},
            {"name": "处置", "id": "action"},
            {"name": "目标域名", "id": "domain"},
        ],
        data=events,
        page_size=10,
    )

    return html.Div([
        create_page_header("安全防护", "DDoS、WAF 与 Bot 管理统一视图"),
        cards,
        html.Div([html.H3("近期拦截事件"), events_table], className="chart-card"),
    ])


# ============================================================================
# 页面: SSL/TLS
# ============================================================================
def create_ssl_page(domains):
    """SSL/TLS 页"""
    import random
    random.seed(100)
    cards = html.Div([
        create_metric_card("活跃证书", f"{len(domains) if domains else 0}", "已签发"),
        create_metric_card("即将过期", "3", "30 天内", COLORS["warning"]),
        create_metric_card("TLS 1.3 覆盖率", "96.4%", "流量占比", COLORS["success"]),
    ], className="metrics-grid", style={"gridTemplateColumns": "repeat(3, 1fr)"})

    issuers = ["Let's Encrypt", "DigiCert", "Let's Encrypt", "Let's Encrypt", "Sectigo"]
    cert_types = ["DV", "DV", "DV", "OV", "EV"]
    certs = []
    for d in (sorted(domains)[:12] if domains else ["example.com"]):
        expire_days = random.randint(10, 90)
        expire_status = "已签发" if expire_days > 30 else "即将过期"
        certs.append({
            "domain": d,
            "issuer": random.choice(issuers),
            "type": random.choice(cert_types),
            "expire": f"2026-0{random.randint(5, 9)}-{random.randint(1,28):02d}",
            "status": expire_status,
        })

    certs_table = _simple_table(
        columns=[
            {"name": "域名", "id": "domain"},
            {"name": "颁发机构", "id": "issuer"},
            {"name": "类型", "id": "type"},
            {"name": "到期时间", "id": "expire"},
            {"name": "状态", "id": "status"},
        ],
        data=certs,
        page_size=12,
    )

    return html.Div([
        create_page_header("SSL/TLS", "证书管理与加密配置", action_label="+ 申请证书"),
        cards,
        html.Div([html.H3("证书列表"), certs_table], className="chart-card"),
        html.Div([
            html.H3("TLS 配置"),
            create_toggle_row("强制 HTTPS", "自动将 HTTP 重定向到 HTTPS", on=True),
            create_toggle_row("HSTS", "HTTP 严格传输安全", on=True),
            create_toggle_row("TLS 1.3", "启用 TLS 1.3 协议", on=True),
            create_toggle_row("OCSP Stapling", "OCSP 装订加速握手", on=True),
            create_toggle_row("Always Use HTTPS", "始终使用加密连接", on=False),
        ], className="chart-card"),
    ])


# ============================================================================
# 页面: WAF 防火墙
# ============================================================================
def create_waf_page():
    """WAF 页"""
    cards = html.Div([
        create_metric_card("托管规则", "已启用", "OWASP Top 10", COLORS["success"]),
        create_metric_card("自定义规则", "14", "用户定义"),
        create_metric_card("24h 命中", "12,847", "总拦截数", COLORS["danger"]),
        create_metric_card("误报率", "0.08%", "月度平均"),
    ], className="metrics-grid", style={"gridTemplateColumns": "repeat(4, 1fr)"})

    rules = [
        {"id": "WAF-001", "name": "SQL 注入检测", "action": "拦截", "hits": "4,218", "status": "启用"},
        {"id": "WAF-002", "name": "XSS 过滤", "action": "拦截", "hits": "2,107", "status": "启用"},
        {"id": "WAF-003", "name": "路径遍历", "action": "拦截", "hits": "891", "status": "启用"},
        {"id": "WAF-004", "name": "文件包含", "action": "拦截", "hits": "342", "status": "启用"},
        {"id": "WAF-005", "name": "命令注入", "action": "拦截", "hits": "178", "status": "启用"},
        {"id": "WAF-006", "name": "恶意 User-Agent", "action": "挑战", "hits": "3,421", "status": "启用"},
        {"id": "WAF-007", "name": "高频访问限制", "action": "限流", "hits": "1,689", "status": "启用"},
        {"id": "CUSTOM-01", "name": "屏蔽特定国家", "action": "拦截", "hits": "0", "status": "未启用"},
        {"id": "CUSTOM-02", "name": "API 速率限制", "action": "限流", "hits": "523", "status": "启用"},
        {"id": "CUSTOM-03", "name": "管理后台白名单", "action": "拦截", "hits": "47", "status": "启用"},
    ]
    rules_table = _simple_table(
        columns=[
            {"name": "规则 ID", "id": "id"},
            {"name": "名称", "id": "name"},
            {"name": "动作", "id": "action"},
            {"name": "24h 命中", "id": "hits"},
            {"name": "状态", "id": "status"},
        ],
        data=rules,
        page_size=12,
    )

    return html.Div([
        create_page_header("WAF 防火墙", "Web 应用防火墙规则与策略", action_label="+ 新建规则"),
        cards,
        html.Div([html.H3("规则列表"), rules_table], className="chart-card"),
    ])


# ============================================================================
# 页面: 性能优化
# ============================================================================
def create_speed_page():
    """性能优化页"""
    protocol_card = html.Div([
        html.H3("协议优化"),
        create_toggle_row("HTTP/3 (QUIC)", "基于 UDP 的最新协议，降低延迟", on=True),
        create_toggle_row("HTTP/2", "多路复用与头部压缩", on=True),
        create_toggle_row("0-RTT", "加速 TLS 1.3 重连接", on=True),
        create_toggle_row("Early Hints (103)", "提前发送资源预加载提示", on=False),
    ], className="chart-card")

    content_card = html.Div([
        html.H3("内容优化"),
        create_toggle_row("Brotli 压缩", "更高压缩率的现代算法", on=True),
        create_toggle_row("Gzip 压缩", "向后兼容的传统压缩", on=True),
        create_toggle_row("图片自适应", "根据设备自动优化图片格式 (WebP/AVIF)", on=True),
        create_toggle_row("Minify CSS", "压缩 CSS 文件体积", on=True),
        create_toggle_row("Minify JS", "压缩 JavaScript 文件体积", on=False),
        create_toggle_row("Minify HTML", "压缩 HTML 文档", on=False),
        create_toggle_row("Rocket Loader", "异步加载 JS，加快首屏", on=False),
    ], className="chart-card")

    network_card = html.Div([
        html.H3("网络优化"),
        create_toggle_row("Argo 智能路由", "动态优化节点到源站的路径", on=True),
        create_toggle_row("Tiered Cache", "多级缓存减少源站压力", on=True),
        create_toggle_row("Prefetch URL", "预取页面中的链接资源", on=False),
    ], className="chart-card")

    return html.Div([
        create_page_header("性能优化", "协议、压缩与网络加速开关"),
        html.Div([protocol_card, content_card], className="chart-row"),
        network_card,
    ])


# ============================================================================
# 页面: 流量规则
# ============================================================================
def create_rules_page():
    """流量规则页"""
    rules = [
        {"priority": "1", "pattern": "*/admin/*", "action": "强制 HTTPS + 缓存级别: 绕过", "status": "启用"},
        {"priority": "2", "pattern": "*/api/v2/*", "action": "禁用缓存 + 请求头: X-API-Version", "status": "启用"},
        {"priority": "3", "pattern": "*/static/*", "action": "边缘缓存 TTL: 30 天", "status": "启用"},
        {"priority": "4", "pattern": "*.mp4|*.webm", "action": "边缘缓存 TTL: 7 天 + 启用 Range", "status": "启用"},
        {"priority": "5", "pattern": "*/download/*", "action": "限速: 10 MB/s", "status": "启用"},
        {"priority": "6", "pattern": "*?utm_*", "action": "忽略查询参数", "status": "启用"},
        {"priority": "7", "pattern": "*/legacy/*", "action": "重定向到 /new/*", "status": "启用"},
        {"priority": "8", "pattern": "*/beta/*", "action": "仅允许白名单 IP", "status": "未启用"},
    ]
    rules_table = _simple_table(
        columns=[
            {"name": "优先级", "id": "priority"},
            {"name": "匹配模式", "id": "pattern"},
            {"name": "动作", "id": "action"},
            {"name": "状态", "id": "status"},
        ],
        data=rules,
        page_size=10,
    )

    return html.Div([
        create_page_header("流量规则", "基于 URL 模式的路由与缓存规则", action_label="+ 新建规则"),
        html.Div([
            html.Div("规则按优先级从小到大匹配，命中后不再继续。",
                     style={"fontSize": "13px", "color": "#64748b", "marginBottom": "12px"}),
            rules_table,
        ], className="chart-card"),
    ])


# ============================================================================
# 页面: 负载均衡
# ============================================================================
def create_loadbalancer_page():
    """负载均衡页"""
    cards = html.Div([
        create_metric_card("活跃池", "4", "负载均衡池"),
        create_metric_card("健康源站", "18 / 20", "10% 降级中", COLORS["warning"]),
        create_metric_card("平均延迟", "42 ms", "源站响应", COLORS["success"]),
    ], className="metrics-grid", style={"gridTemplateColumns": "repeat(3, 1fr)"})

    pools = [
        {"name": "primary-pool-cn", "healthy": "4 / 4", "algorithm": "加权轮询", "weight": "100,100,100,100", "status": "正常"},
        {"name": "secondary-pool-cn", "healthy": "3 / 4", "algorithm": "最少连接", "weight": "100,100,100,0", "status": "部分降级"},
        {"name": "global-pool", "healthy": "6 / 6", "algorithm": "地理就近", "weight": "自动", "status": "正常"},
        {"name": "api-pool-sg", "healthy": "5 / 6", "algorithm": "加权轮询", "weight": "50,50,100,100,100,100", "status": "正常"},
    ]
    pools_table = _simple_table(
        columns=[
            {"name": "池名称", "id": "name"},
            {"name": "健康节点", "id": "healthy"},
            {"name": "调度算法", "id": "algorithm"},
            {"name": "权重", "id": "weight"},
            {"name": "状态", "id": "status"},
        ],
        data=pools,
        page_size=10,
    )

    origins = [
        {"name": "origin-sh-01", "ip": "10.0.1.11", "latency": "12 ms", "health": "健康", "pool": "primary-pool-cn"},
        {"name": "origin-sh-02", "ip": "10.0.1.12", "latency": "15 ms", "health": "健康", "pool": "primary-pool-cn"},
        {"name": "origin-bj-01", "ip": "10.0.2.11", "latency": "28 ms", "health": "健康", "pool": "primary-pool-cn"},
        {"name": "origin-bj-02", "ip": "10.0.2.12", "latency": "32 ms", "health": "健康", "pool": "primary-pool-cn"},
        {"name": "origin-gz-01", "ip": "10.0.3.11", "latency": "45 ms", "health": "健康", "pool": "secondary-pool-cn"},
        {"name": "origin-gz-02", "ip": "10.0.3.12", "latency": "-", "health": "不健康", "pool": "secondary-pool-cn"},
    ]
    origins_table = _simple_table(
        columns=[
            {"name": "源站", "id": "name"},
            {"name": "IP", "id": "ip"},
            {"name": "延迟", "id": "latency"},
            {"name": "健康状态", "id": "health"},
            {"name": "所属池", "id": "pool"},
        ],
        data=origins,
        page_size=10,
    )

    return html.Div([
        create_page_header("负载均衡", "负载均衡池与源站健康监测", action_label="+ 创建池"),
        cards,
        html.Div([html.H3("负载均衡池"), pools_table], className="chart-card"),
        html.Div([html.H3("源站健康监测"), origins_table], className="chart-card"),
    ])


# ============================================================================
# 页面: 月度报告
# ============================================================================
def _list_available_months(storage):
    """返回数据库内覆盖到的月份列表（YYYY-MM 字符串，降序）"""
    min_time, max_time = storage.get_time_range()
    if not min_time or not max_time:
        return []
    start_dt = datetime.fromtimestamp(min_time / 1000, tz=LOCAL_TZ).replace(day=1)
    end_dt = datetime.fromtimestamp(max_time / 1000, tz=LOCAL_TZ).replace(day=1)
    months = []
    cur = start_dt
    while cur <= end_dt:
        months.append(cur.strftime("%Y-%m"))
        # 下个月
        y, m = cur.year, cur.month
        if m == 12:
            cur = cur.replace(year=y + 1, month=1)
        else:
            cur = cur.replace(month=m + 1)
    return list(reversed(months))


def _month_bounds(month_str):
    """YYYY-MM → (start_ms, end_ms) 覆盖该自然月"""
    y, m = map(int, month_str.split("-"))
    start = datetime(y, m, 1, 0, 0, 0, tzinfo=LOCAL_TZ)
    if m == 12:
        end = datetime(y + 1, 1, 1, 0, 0, 0, tzinfo=LOCAL_TZ) - timedelta(seconds=1)
    else:
        end = datetime(y, m + 1, 1, 0, 0, 0, tzinfo=LOCAL_TZ) - timedelta(seconds=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _build_report_content(storage, month_str, project):
    """构造月度报告主体（供初始化与回调复用）"""
    if not month_str:
        return html.Div("暂无数据", style={"color": "#94a3b8", "padding": "40px", "textAlign": "center"})

    start_ms, end_ms = _month_bounds(month_str)
    proj = project if project and project != "all" else None

    meta = _load_aggregate_meta(storage, start_ms, end_ms, project=proj)
    peak_bps = _load_peak_bps(storage, start_ms, end_ms, project=proj)
    daily = _load_daily_stats(storage, start_ms, end_ms, project=proj)

    total_flux_tb = (meta.get("total_flux") or 0) / (1024 ** 4)
    total_req = meta.get("total_requests") or 0
    total_hits = meta.get("total_hits") or 0
    total_bs = meta.get("total_bs") or 0
    total_bs_fail = meta.get("total_bs_fail") or 0
    domain_count = meta.get("domain_count") or 0
    hit_rate = (total_hits / total_req * 100) if total_req > 0 else 0
    avail = (1 - total_bs_fail / total_bs) * 100 if total_bs > 0 else None
    peak_gbps = peak_bps / 1_000_000_000

    # 日平均 / 日 95 带宽
    if daily:
        daily_peaks_gbps = [(r["peak_bps"] or 0) / 1_000_000_000 for r in daily]
        daily_avg_gbps = sum(daily_peaks_gbps) / len(daily_peaks_gbps)
        sorted_peaks = sorted(daily_peaks_gbps)
        p95_idx = max(0, min(len(sorted_peaks) - 1, int(len(sorted_peaks) * 0.95) - 1))
        daily_p95_gbps = sorted_peaks[p95_idx]
    else:
        daily_avg_gbps = daily_p95_gbps = 0

    project_label = proj if proj else "全部项目"

    # 概要卡片
    summary = html.Div([
        create_metric_card("总流量", f"{total_flux_tb:,.2f} TB", f"{month_str} 累计"),
        create_metric_card("总请求", f"{total_req/1e6:,.2f} M", "累计请求数"),
        create_metric_card("峰值带宽", f"{peak_gbps:.2f} Gbps", "月内瞬时峰值", COLORS["primary"]),
        create_metric_card("日 95 带宽", f"{daily_p95_gbps:.2f} Gbps", "每日峰值 P95"),
        create_metric_card("平均命中率", f"{hit_rate:.2f}%", "流量命中率",
                           COLORS["success"] if hit_rate >= 90 else COLORS["warning"]),
        create_metric_card("可用率", f"{avail:.2f}%" if avail is not None else "—",
                           "回源成功率",
                           COLORS["success"] if (avail or 0) >= 99.9 else COLORS["warning"]),
    ], className="metrics-grid", style={"gridTemplateColumns": "repeat(6, 1fr)"})

    # 日趋势图（流量 + 峰值带宽双轴）
    trend_fig = make_subplots(specs=[[{"secondary_y": True}]])
    dates = [r["d"] for r in daily]
    flux_gb = [(r["flux"] or 0) / (1024 ** 3) for r in daily]
    peaks_gbps = [(r["peak_bps"] or 0) / 1e9 for r in daily]
    trend_fig.add_trace(
        go.Bar(x=dates, y=flux_gb, name="日流量 (GB)",
               marker_color=COLORS["primary"], opacity=0.6, marker_line_width=0),
        secondary_y=False,
    )
    trend_fig.add_trace(
        go.Scatter(x=dates, y=peaks_gbps, name="日峰值带宽 (Gbps)",
                   mode="lines+markers",
                   line={"color": COLORS["warning"], "width": 2},
                   marker={"size": 6}),
        secondary_y=True,
    )
    trend_fig = apply_chart_style(trend_fig, "每日流量与峰值带宽")
    trend_fig.update_yaxes(title_text="流量 (GB)", secondary_y=False, title_font={"size": 11})
    trend_fig.update_yaxes(title_text="峰值 (Gbps)", secondary_y=True, title_font={"size": 11})

    # 域名 Top 10
    with storage._get_conn() as conn:
        where_sql, params = _build_where(start_ms, end_ms, None, proj)
        top_rows = conn.execute(
            f"""
            SELECT domain, SUM(flux) AS flux, SUM(req_num) AS req, SUM(hit_num) AS hit
            FROM cdn_logs WHERE {where_sql}
            GROUP BY domain ORDER BY flux DESC LIMIT 10
            """,
            params,
        ).fetchall()
    top_df = pd.DataFrame([dict(r) for r in top_rows]) if top_rows else pd.DataFrame()
    if not top_df.empty:
        top_df["flux_gb"] = top_df["flux"] / (1024 ** 3)
        top_df["hit_rate"] = (top_df["hit"] / top_df["req"].where(top_df["req"] > 0) * 100).fillna(0)
        top_df = top_df.sort_values("flux_gb", ascending=True)
        top_fig = go.Figure(go.Bar(
            x=top_df["flux_gb"], y=top_df["domain"], orientation="h",
            marker_color=COLORS["primary"], marker_line_width=0,
            text=[f"{v:.1f} GB" for v in top_df["flux_gb"]],
            textposition="outside",
            textfont={"size": 11, "color": COLORS["text_secondary"]},
            hovertemplate="<b>%{y}</b><br>流量: %{x:.2f} GB<extra></extra>"
        ))
        top_fig = apply_chart_style(top_fig, "域名流量排行 Top 10")
        top_fig.update_layout(showlegend=False, margin={"l": 140})
    else:
        top_fig = {}

    # HTTP 状态码
    http_totals = {
        "2xx": int(meta.get("c2") or 0),
        "3xx": int(meta.get("c3") or 0),
        "4xx": int(meta.get("c4") or 0),
        "5xx": int(meta.get("c5") or 0),
    }
    http_fig = go.Figure(data=[go.Pie(
        labels=list(http_totals.keys()), values=list(http_totals.values()),
        hole=0.6,
        marker_colors=[HTTP_COLORS[k] for k in http_totals.keys()],
        textinfo="percent",
        textfont={"size": 12, "color": "#ffffff"},
        hovertemplate="<b>%{label}</b><br>%{value:,} 次<br>%{percent}<extra></extra>"
    )])
    http_fig = apply_chart_style(http_fig, "HTTP 状态码分布")
    http_fig.update_layout(showlegend=True, legend={"orientation": "v", "x": 1, "y": 0.5})

    # 每日明细表
    daily_rows = []
    for r in daily:
        rf = (r["flux"] or 0)
        rr = (r["req"] or 0)
        rh = (r["hit"] or 0)
        rbs = (r["bs_num"] or 0)
        rbsf = (r["bs_fail"] or 0)
        daily_rows.append({
            "date": r["d"],
            "flux_gb": rf / (1024 ** 3),
            "req_m": rr / 1e6,
            "hit_rate": (rh / rr * 100) if rr > 0 else 0,
            "peak_gbps": (r["peak_bps"] or 0) / 1e9,
            "avail": ((1 - rbsf / rbs) * 100) if rbs > 0 else 100.0,
        })
    daily_table = _simple_table(
        columns=[
            {"name": "日期", "id": "date"},
            {"name": "流量 (GB)", "id": "flux_gb", "type": "numeric", "format": {"specifier": ",.2f"}},
            {"name": "请求 (M)", "id": "req_m", "type": "numeric", "format": {"specifier": ",.2f"}},
            {"name": "命中率 (%)", "id": "hit_rate", "type": "numeric", "format": {"specifier": ".2f"}},
            {"name": "峰值带宽 (Gbps)", "id": "peak_gbps", "type": "numeric", "format": {"specifier": ".2f"}},
            {"name": "可用率 (%)", "id": "avail", "type": "numeric", "format": {"specifier": ".2f"}},
        ],
        data=daily_rows,
        page_size=31,
    )

    # 报告标题区
    report_title = html.Div([
        html.Div(f"{month_str} 月度 CDN 流量报告",
                 style={"fontSize": "24px", "fontWeight": "700", "color": "#0f172a"}),
        html.Div(f"项目: {project_label} · 涵盖 {len(daily)} 天 · {domain_count} 个域名 · "
                 f"生成时间 {datetime.now(tz=LOCAL_TZ).strftime('%Y-%m-%d %H:%M')}",
                 style={"fontSize": "13px", "color": "#64748b", "marginTop": "6px"}),
    ], style={"marginBottom": "20px", "paddingBottom": "16px",
              "borderBottom": "2px solid #e2e8f0"})

    return html.Div([
        report_title,
        summary,
        html.Div([
            dcc.Graph(figure=trend_fig, config={"displayModeBar": False})
        ], className="chart-card"),
        html.Div([
            html.Div([
                dcc.Graph(figure=top_fig, config={"displayModeBar": False}) if top_fig else html.Div("无数据")
            ], className="chart-card"),
            html.Div([
                dcc.Graph(figure=http_fig, config={"displayModeBar": False})
            ], className="chart-card"),
        ], className="chart-row"),
        html.Div([
            html.H3("每日明细"),
            daily_table,
        ], className="chart-card"),
    ])


def create_report_page(storage, projects):
    """月度报告页面"""
    months = _list_available_months(storage)
    default_month = months[0] if months else None

    toolbar = html.Div([
        html.Span("月份", className="filter-label"),
        dcc.Dropdown(
            id="report-month-filter",
            options=[{"label": m, "value": m} for m in months],
            value=default_month,
            style={"width": "160px"},
            clearable=False,
        ),
        html.Span("项目", className="filter-label", style={"marginLeft": "20px"}),
        dcc.Dropdown(
            id="report-project-filter",
            options=[{"label": "全部项目", "value": "all"}] +
                    [{"label": p, "value": p} for p in projects],
            value="all",
            style={"width": "200px"},
            clearable=False,
        ),
        html.Div(className="time-range-spacer"),
        html.Button([
            html.Span("picture_as_pdf", className="material-symbols-outlined",
                      style={"fontSize": "18px", "marginRight": "6px", "verticalAlign": "middle"}),
            "导出 PDF",
        ], id="report-export-btn", className="btn-primary",
            style={"display": "inline-flex", "alignItems": "center"},
            n_clicks=0),
    ], className="time-range-bar", style={"alignItems": "center"}, id="report-toolbar")

    initial = _build_report_content(storage, default_month, "all")

    return html.Div([
        create_page_header("月度报告", "按月汇总 CDN 流量指标，支持导出为 PDF"),
        toolbar,
        html.Div(initial, id="report-content"),
        # 客户端回调触发打印
        html.Div(id="report-print-dummy", style={"display": "none"}),
    ])


def create_app(data_file=None, config_path=None):
    """创建 Dash 应用"""
    # 获取 SQLite 存储
    storage = get_storage()
    resolved_config_path = Path(
        config_path
        or os.environ.get("FAKE_CDN_CONFIG_PATH")
        or Path.cwd() / "config.json"
    )
    config_manager = ConfigManager(resolved_config_path)

    # 获取数据范围（默认时间窗口与概览页"周"对齐：以 _resolve_end_time_ms 为锚向前 7 天）
    default_start, default_end = get_default_date_range(storage)
    min_time, max_time = storage.get_time_range()

    default_end_ms = _resolve_end_time_ms(storage)
    default_end_dt = datetime.fromtimestamp(default_end_ms / 1000, tz=LOCAL_TZ)
    default_start_dt = default_end_dt - timedelta(days=7)
    default_start_iso = default_start_dt.strftime("%Y-%m-%dT%H:%M:%S")
    default_end_iso = default_end_dt.strftime("%Y-%m-%dT%H:%M:%S")

    # 获取域名列表 + 项目列表
    domains = storage.get_domains()
    projects = storage.get_projects()

    # 计算日期范围边界
    if min_time and max_time:
        min_date = datetime.fromtimestamp(min_time / 1000).date()
        # max_date_allowed 设为未来30天，避免限制用户选择
        max_date = (datetime.now() + timedelta(days=30)).date()
    else:
        min_date = default_start
        max_date = (datetime.now() + timedelta(days=30)).date()

    # 创建 Dash 应用
    app = dash.Dash(__name__, title="CDN Panel", suppress_callback_exceptions=True)
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
        # URL 路由
        dcc.Location(id="url", refresh=False),

        # 侧边栏容器（由路由回调重新渲染以同步 active 态）
        html.Div(create_sidebar("/overview"), id="sidebar-container"),

        # 主区域
        html.Div([
            # 顶栏容器（由路由回调重新渲染面包屑）
            html.Div(create_topbar(auth_config, "/overview"), id="topbar-container"),

            # 内容区
            html.Div(
                create_overview_page(storage),
                id="page-content",
                className="content-area",
            ),
        ], className="main-area"),
    ], className="app-shell")

    # 配置管理页面回调独立注册，避免继续膨胀主数据回调。
    register_config_callbacks(app, config_manager)

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
            Input("project-filter", "value"),
            Input("refresh-interval", "n_intervals")
        ]
    )
    def update_all(start_datetime, end_datetime, selected_domain, selected_project, n_intervals):
        """SQL 层聚合 + 构造全部图表（不再加载全量原始记录）"""
        # 解析日期时间字符串，统一按 Asia/Shanghai 时区解释
        def parse_dt(value, end_of_day=False):
            if not value:
                return None
            dt_str = value.strip().replace("T", " ")
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    dt = datetime.strptime(dt_str, fmt).replace(tzinfo=LOCAL_TZ)
                    return int(dt.timestamp() * 1000)
                except ValueError:
                    continue
            try:
                dt = datetime.strptime(dt_str[:10], "%Y-%m-%d")
                if end_of_day:
                    dt = dt.replace(hour=23, minute=59, second=59)
                dt = dt.replace(tzinfo=LOCAL_TZ)
                return int(dt.timestamp() * 1000)
            except ValueError:
                return None

        def fmt_ts(ms):
            if not ms:
                return "—"
            return datetime.fromtimestamp(ms / 1000, tz=LOCAL_TZ).strftime("%Y-%m-%d %H:%M")

        try:
            start_time = parse_dt(start_datetime)
            end_time = parse_dt(end_datetime, end_of_day=True)
            data = load_analytics_data(
                storage, start_time, end_time, selected_domain, selected_project
            )
        except Exception as e:
            print(f"[错误] 加载数据失败: {e}")
            import traceback
            traceback.print_exc()
            return (
                "数据加载失败", html.Div(), f"错误: {e}",
                {}, {}, {}, {}, {}, {}, {}, {}, []
            )

        if not data["time_rows"]:
            return (
                "暂无数据", html.Div(), "无数据",
                {}, {}, {}, {}, {}, {}, {}, {}, []
            )

        # 构建 time_df (规模 = 时间点数 ≈ 8640 for 30 天)
        time_df = pd.DataFrame(data["time_rows"])
        for col in ("bps", "bs_bps", "flux", "bs_flux", "req_num", "hit_num", "bs_num", "bs_fail_num"):
            time_df[col] = pd.to_numeric(time_df[col], errors="coerce").fillna(0)
        time_df["timestamp"] = pd.to_datetime(time_df["start_time"], unit="ms")
        time_df["bw_mbps"] = time_df["bps"] / 1_000_000
        time_df["bs_bw_mbps"] = time_df["bs_bps"] / 1_000_000
        time_df["flux_gb"] = time_df["flux"] / (1024 ** 3)
        time_df["bs_flux_gb"] = time_df["bs_flux"] / (1024 ** 3)
        time_df["hit_rate"] = (
            time_df["hit_num"] / time_df["req_num"].where(time_df["req_num"] > 0) * 100
        ).fillna(0)

        meta = data["meta"]

        # 头部信息：显示用户选定的查询范围，而非数据实际 min/max
        query_start = fmt_ts(start_time) if start_time else "全部"
        query_end = fmt_ts(end_time) if end_time else "全部"
        project_label = selected_project if selected_project and selected_project != "all" else "全部项目"
        header_info = (
            f"查询范围: {query_start} - {query_end} · 项目: {project_label} · "
            f"{meta.get('total_records', 0):,} 条记录 · {meta.get('domain_count', 0)} 个域名"
        )

        # 汇总卡片
        summary = create_summary_cards_from_time_df(time_df, meta)

        refresh_time = datetime.now().strftime("%H:%M:%S")
        refresh_status = f"上次刷新: {refresh_time} · 每 {REFRESH_INTERVAL_MS // 1000} 秒自动更新"

        # 1. 请求带宽
        bw_fig = go.Figure()
        bw_fig.add_trace(go.Scatter(
            x=time_df["timestamp"], y=time_df["bw_mbps"],
            name="带宽", fill="tozeroy",
            line={"color": COLORS["primary"], "width": 2},
            fillcolor="rgba(14, 165, 233, 0.08)"
        ))
        bw_fig = apply_chart_style(bw_fig, "请求带宽")
        bw_fig.update_yaxes(title_text="带宽 (Mbps)", title_font={"size": 11})

        # 2. 请求流量
        flux_fig = go.Figure()
        flux_fig.add_trace(go.Scatter(
            x=time_df["timestamp"], y=time_df["flux_gb"],
            name="流量", fill="tozeroy",
            line={"color": COLORS["success"], "width": 2},
            fillcolor="rgba(16, 185, 129, 0.1)"
        ))
        flux_fig = apply_chart_style(flux_fig, "请求流量")
        flux_fig.update_yaxes(title_text="流量 (GB)", title_font={"size": 11})

        # 3. 请求数
        req_fig = go.Figure()
        req_fig.add_trace(go.Scatter(
            x=time_df["timestamp"], y=time_df["req_num"],
            name="请求数", fill="tozeroy",
            line={"color": COLORS["purple"], "width": 2},
            fillcolor="rgba(139, 92, 246, 0.1)"
        ))
        req_fig = apply_chart_style(req_fig, "请求数")
        req_fig.update_yaxes(title_text="请求数 (个)", title_font={"size": 11})

        # 4. 命中率
        hitrate_fig = go.Figure()
        hitrate_fig.add_trace(go.Scatter(
            x=time_df["timestamp"], y=time_df["hit_rate"],
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

        # 5. HTTP 状态码分布（来自 SQL meta，不需扫 df）
        http_totals = {
            "2xx": int(meta.get("c2") or 0),
            "3xx": int(meta.get("c3") or 0),
            "4xx": int(meta.get("c4") or 0),
            "5xx": int(meta.get("c5") or 0),
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

        # 6. 回源 HTTP 状态码分布
        bs_http_totals = {
            "2xx": int(meta.get("bs2") or 0),
            "3xx": int(meta.get("bs3") or 0),
            "4xx": int(meta.get("bs4") or 0),
            "5xx": int(meta.get("bs5") or 0),
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

        # 7. 域名流量排行（SQL top 10）
        if data["domains"]:
            domain_df = pd.DataFrame(data["domains"])
            domain_df["flux_gb"] = pd.to_numeric(domain_df["flux"], errors="coerce").fillna(0) / (1024 ** 3)
            domain_df = domain_df.sort_values("flux_gb", ascending=True)
            domain_fig = go.Figure(go.Bar(
                x=domain_df["flux_gb"],
                y=domain_df["domain"],
                orientation="h",
                marker_color=COLORS["primary"],
                marker_line_width=0,
                text=[f"{v:.1f} GB" for v in domain_df["flux_gb"]],
                textposition="outside",
                textfont={"size": 11, "color": COLORS["text_secondary"]},
                hovertemplate="<b>%{y}</b><br>流量: %{x:.2f} GB<extra></extra>"
            ))
            domain_fig = apply_chart_style(domain_fig, "域名流量排行 (Top 10)")
            domain_fig.update_layout(showlegend=False, margin={"l": 140})
        else:
            domain_fig = {}

        # 8. 回源分析
        origin_fig = make_subplots(specs=[[{"secondary_y": True}]])
        origin_fig.add_trace(
            go.Scatter(
                x=time_df["timestamp"], y=time_df["bs_bw_mbps"],
                name="回源带宽", fill="tozeroy",
                line={"color": COLORS["warning"], "width": 2},
                fillcolor="rgba(245, 158, 11, 0.1)"
            ), secondary_y=False
        )
        origin_fig.add_trace(
            go.Bar(
                x=time_df["timestamp"], y=time_df["bs_fail_num"],
                name="失败数", marker_color=COLORS["danger"], opacity=0.8, marker_line_width=0
            ), secondary_y=True
        )
        origin_fig = apply_chart_style(origin_fig, "回源带宽与失败分析")
        origin_fig.update_yaxes(title_text="回源带宽 (Mbps)", secondary_y=False, title_font={"size": 11})
        origin_fig.update_yaxes(title_text="失败数", secondary_y=True, title_font={"size": 11})

        # 9. 数据表：SQL 已返回最近 500 条，这里只做格式化
        table_rows = []
        for r in data["table"]:
            interval = r.get("interval") or 300
            req = r.get("req_num") or 0
            hit = r.get("hit_num") or 0
            table_rows.append({
                "timestamp": datetime.fromtimestamp((r["start_time"] or 0) / 1000).strftime("%m-%d %H:%M:%S"),
                "domain": r.get("domain"),
                "bw_mbps": (r.get("bw") or 0) / interval / 1_000_000,
                "flux_gb": (r.get("flux") or 0) / (1024 ** 3),
                "req_num": req,
                "hit_rate": (hit / req * 100) if req > 0 else 0,
                "bs_num": r.get("bs_num") or 0,
                "bs_fail_num": r.get("bs_fail_num") or 0,
            })

        return (
            header_info,
            summary,
            refresh_status,
            bw_fig, flux_fig, req_fig, hitrate_fig,
            http_fig, bs_http_fig,
            domain_fig, origin_fig,
            table_rows,
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

        # 统一时间基准：与概览页完全一致
        end_ms = _resolve_end_time_ms(storage)
        end_dt = datetime.fromtimestamp(end_ms / 1000, tz=LOCAL_TZ)
        end_val = end_dt.strftime("%Y-%m-%dT%H:%M:%S")

        if btn_id == "range-24h":
            start_val = (end_dt - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
        elif btn_id == "range-7d":
            start_val = (end_dt - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
        elif btn_id == "range-30d":
            start_val = (end_dt - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
        else:  # custom
            return dash.no_update, dash.no_update, *classes

        return start_val, end_val, *classes

    # 分析页：选中项目后，域名下拉只展示该项目下的域名
    @app.callback(
        [
            Output("domain-filter", "options"),
            Output("domain-filter", "value"),
        ],
        [Input("project-filter", "value")],
        prevent_initial_call=True,
    )
    def filter_domains_by_project(selected_project):
        proj = selected_project if selected_project and selected_project != "all" else None
        project_domains = storage.get_domains(project=proj)
        options = [{"label": "全部域名", "value": "all"}] + [
            {"label": d, "value": d} for d in sorted(project_domains)
        ]
        return options, "all"

    # 域名管理页：按项目筛选表格
    @app.callback(
        Output("domains-table", "data"),
        [Input("domain-project-filter", "value")],
        prevent_initial_call=True,
    )
    def refresh_domains_table(selected_project):
        proj = selected_project if selected_project and selected_project != "all" else None
        pairs = storage.get_domain_project_pairs(project=proj)
        return _build_domain_rows(pairs)

    # 概览页：日/周/月 范围切换
    @app.callback(
        [
            Output("overview-cards", "children"),
            Output("overview-nodes", "children"),
            Output("overview-timeline", "children"),
            Output("overview-range-day", "className"),
            Output("overview-range-week", "className"),
            Output("overview-range-month", "className"),
        ],
        [
            Input("overview-range-day", "n_clicks"),
            Input("overview-range-week", "n_clicks"),
            Input("overview-range-month", "n_clicks"),
        ],
        prevent_initial_call=True,
    )
    def update_overview_range(n_d, n_w, n_m):
        ctx = dash.callback_context
        btn = ctx.triggered[0]["prop_id"].split(".")[0] if ctx.triggered else "overview-range-week"
        key_map = {
            "overview-range-day": "day",
            "overview-range-week": "week",
            "overview-range-month": "month",
        }
        range_key = key_map.get(btn, "week")
        metrics = _compute_overview_metrics(storage, range_key)
        cards = _build_overview_cards(metrics)
        nodes_html = _compute_node_cards(storage, metrics["start_ms"], metrics["end_ms"])
        timeline_html = _compute_activity_timeline(storage, metrics["start_ms"], metrics["end_ms"])

        def cls(b):
            return "time-range-btn" + (" active" if b == btn else "")

        return (
            cards,
            nodes_html,
            timeline_html,
            cls("overview-range-day"),
            cls("overview-range-week"),
            cls("overview-range-month"),
        )

    # 月度报告：月/项目 → 刷新报告内容
    @app.callback(
        Output("report-content", "children"),
        [
            Input("report-month-filter", "value"),
            Input("report-project-filter", "value"),
        ],
        prevent_initial_call=True,
    )
    def refresh_report(month_str, project_val):
        return _build_report_content(storage, month_str, project_val)

    # 月度报告：客户端触发浏览器打印（另存为 PDF）
    app.clientside_callback(
        """
        function(n) {
            if (n && n > 0) { setTimeout(function(){ window.print(); }, 100); }
            return "";
        }
        """,
        Output("report-print-dummy", "children"),
        Input("report-export-btn", "n_clicks"),
        prevent_initial_call=True,
    )

    # 路由回调：根据 URL 分发页面
    @app.callback(
        [
            Output("page-content", "children"),
            Output("sidebar-container", "children"),
            Output("topbar-container", "children"),
        ],
        [Input("url", "pathname")]
    )
    def render_page(pathname):
        # 归一化
        if not pathname or pathname == "/":
            pathname = "/overview"

        page_map = {
            "/overview": lambda: create_overview_page(storage),
            "/analytics": lambda: create_analytics_page(default_start_iso, default_end_iso, domains, projects),
            "/report": lambda: create_report_page(storage, projects),
            "/config": lambda: create_config_page(config_manager),
            "/config-audit": lambda: create_config_audit_page(config_manager),
            "/domains": lambda: create_domains_page(storage.get_domain_project_pairs(), projects),
            "/dns": lambda: create_dns_page(domains),
            "/cache": lambda: create_cache_page(),
            "/security": lambda: create_security_page(),
            "/ssl": lambda: create_ssl_page(domains),
            "/waf": lambda: create_waf_page(),
            "/speed": lambda: create_speed_page(),
            "/rules": lambda: create_rules_page(),
            "/loadbalancer": lambda: create_loadbalancer_page(),
        }
        page_fn = page_map.get(pathname)
        if page_fn is None:
            page = html.Div([
                create_page_header("页面不存在", f"未找到路径 {pathname}"),
            ])
        else:
            page = page_fn()

        sidebar = create_sidebar(pathname)
        topbar = create_topbar(auth_config, pathname)
        return page, sidebar, topbar

    return app


def run_dashboard(host="0.0.0.0", port=8050, debug=False, data_file=None, config_path=None):
    """运行仪表板"""
    app = create_app(data_file, config_path=config_path)

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
