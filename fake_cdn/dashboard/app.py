#!/usr/bin/env python3
"""
CDN 推送数据可视化面板
基于 Dash + Plotly 构建
使用 SQLite 存储提升性能
"""

import os
from datetime import datetime, timedelta
from pathlib import Path

import dash
from dash import dcc, html, dash_table
from dash.dependencies import Input, Output
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd

from ..core.storage import CDNLogStorage, get_default_storage

# ============================================================================
# 专业配色方案 (参考 Stripe/Linear 设计规范)
# ============================================================================
COLORS = {
    # 基础色
    "bg": "#f9fafb",
    "card": "#ffffff",
    "border": "#e5e7eb",

    # 文字
    "text_primary": "#111827",
    "text_secondary": "#6b7280",
    "text_muted": "#9ca3af",

    # 语义色
    "primary": "#3b82f6",      # 蓝 - 主要数据
    "success": "#10b981",      # 绿 - 正向指标
    "warning": "#f59e0b",      # 橙 - 警告
    "danger": "#ef4444",       # 红 - 错误
    "info": "#06b6d4",         # 青 - 信息
    "purple": "#8b5cf6",       # 紫 - 辅助

    # 图表专用
    "chart_primary": "#3b82f6",
    "chart_secondary": "#10b981",
    "chart_tertiary": "#8b5cf6",
    "chart_grid": "#f3f4f6",
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
        timestamp = datetime.fromtimestamp(record["start_time"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
        # 按区域数量分批（config 中有 5 个区域）
        batch = i // 5 + 1

        row = {
            "timestamp": timestamp,
            "batch": batch,
            "domain": record["domain"],
            "bw_mbps": record["bw"] or 0,
            "flux_gb": (record["flux"] or 0) / (1024**3),
            "bs_bw_mbps": record["bs_bw"] or 0,
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
    """获取默认日期范围（显示全部数据）"""
    min_time, max_time = storage.get_time_range()
    if min_time is None or max_time is None:
        # 无数据时返回当前时间范围
        now = datetime.now()
        return now.date(), now.date()

    # 转换为日期
    max_date = datetime.fromtimestamp(max_time / 1000).date()
    min_date = datetime.fromtimestamp(min_time / 1000).date()

    # 默认显示全部数据范围
    return min_date, max_date


def create_metric_card(title, value, subtitle=None, color=None):
    """创建单个指标卡片"""
    return html.Div([
        html.Div(title, className="metric-label"),
        html.Div(value, className="metric-value", style={"color": color} if color else {}),
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
    # 95计费：每天288个点，去掉最高5%（约14个），取第273个值
    time_agg["date"] = time_agg["timestamp"].dt.date

    def calc_95_billing(bw_series):
        """计算95计费值：排序后取第95%位置的值"""
        sorted_bw = bw_series.sort_values(ascending=True).values
        n = len(sorted_bw)
        # 取第95%位置，即去掉最高5%后的最大值
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
    daily_avg_bw = daily_stats["daily_avg"].mean()  # 日平均的均值
    daily_p95_bw = daily_stats["daily_p95"].mean()  # 日95的均值

    total_flux = df["flux_gb"].sum()
    total_requests = df["req_num"].sum()
    avg_hit_rate = df["hit_rate"].mean()
    total_bs_fail = df["bs_fail_num"].sum()
    total_bs = df["bs_num"].sum()

    return html.Div([
        create_metric_card("峰值带宽", f"{peak_bw/1000:.1f} Gbps", f"平均 {avg_bw/1000:.1f} Gbps"),
        create_metric_card("日平均带宽", f"{daily_avg_bw/1000:.1f} Gbps", "每日平均值"),
        create_metric_card("日95带宽", f"{daily_p95_bw/1000:.1f} Gbps", "每日95分位值", COLORS["primary"]),
        create_metric_card("总流量", f"{total_flux:.1f} GB", "累计传输流量"),
        create_metric_card("缓存命中率", f"{avg_hit_rate:.1f}%", "平均命中比例",
                          COLORS["success"] if avg_hit_rate >= 90 else COLORS["warning"]),
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
        <style>
            * { box-sizing: border-box; }

            body {
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                background-color: #f9fafb;
                margin: 0;
                padding: 24px;
                color: #111827;
                line-height: 1.5;
            }

            /* 头部 */
            .header {
                margin-bottom: 24px;
            }
            .header h1 {
                font-size: 24px;
                font-weight: 600;
                color: #111827;
                margin: 0 0 4px 0;
            }
            .header p {
                font-size: 14px;
                color: #6b7280;
                margin: 0;
            }

            /* 指标卡片网格 */
            .metrics-grid {
                display: grid;
                grid-template-columns: repeat(5, 1fr);
                gap: 16px;
                margin-bottom: 24px;
            }
            .metric-card {
                background: #ffffff;
                border: 1px solid #e5e7eb;
                border-radius: 8px;
                padding: 20px;
            }
            .metric-label {
                font-size: 13px;
                font-weight: 500;
                color: #6b7280;
                margin-bottom: 8px;
            }
            .metric-value {
                font-size: 28px;
                font-weight: 600;
                color: #111827;
                letter-spacing: -0.5px;
            }
            .metric-subtitle {
                font-size: 12px;
                color: #9ca3af;
                margin-top: 4px;
            }

            /* 筛选器 */
            .filter-bar {
                background: #ffffff;
                border: 1px solid #e5e7eb;
                border-radius: 8px;
                padding: 16px 20px;
                margin-bottom: 24px;
                display: flex;
                align-items: center;
                gap: 16px;
            }
            .filter-label {
                font-size: 14px;
                font-weight: 500;
                color: #374151;
            }

            /* 图表容器 */
            .chart-card {
                background: #ffffff;
                border: 1px solid #e5e7eb;
                border-radius: 8px;
                padding: 20px;
                margin-bottom: 20px;
            }
            .chart-card h3 {
                font-size: 14px;
                font-weight: 600;
                color: #111827;
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
                border-color: #e5e7eb !important;
                border-radius: 6px !important;
            }
            .Select-control:hover {
                border-color: #d1d5db !important;
            }

            /* 响应式 */
            @media (max-width: 1200px) {
                .metrics-grid { grid-template-columns: repeat(3, 1fr); }
            }
            @media (max-width: 768px) {
                .metrics-grid { grid-template-columns: repeat(2, 1fr); }
                .chart-row { grid-template-columns: 1fr; }
                body { padding: 16px; }
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
    app = dash.Dash(__name__, title="CDN Analytics")
    app.index_string = INDEX_STRING

    # 布局
    app.layout = html.Div([
        # 定时刷新组件
        dcc.Interval(
            id="refresh-interval",
            interval=REFRESH_INTERVAL_MS,
            n_intervals=0
        ),

        # 标题 (动态更新)
        html.Div([
            html.H1("CDN Analytics Dashboard"),
            html.P(id="header-info")
        ], className="header"),

        # 汇总卡片容器 (动态更新)
        html.Div(id="summary-cards"),

        # 筛选器
        html.Div([
            # 日期范围选择器
            html.Span("日期范围", className="filter-label"),
            dcc.DatePickerRange(
                id="date-range-picker",
                min_date_allowed=min_date,
                max_date_allowed=max_date,
                start_date=default_start,
                end_date=default_end,
                display_format="YYYY-MM-DD",
                style={"marginRight": "24px"}
            ),
            # 域名筛选
            html.Span("筛选域名", className="filter-label"),
            dcc.Dropdown(
                id="domain-filter",
                options=[{"label": "全部域名", "value": "all"}] +
                        [{"label": d, "value": d} for d in sorted(domains)],
                value="all",
                style={"width": "280px"},
                clearable=False
            ),
            # 刷新状态提示
            html.Span(id="refresh-status", style={"marginLeft": "auto", "fontSize": "12px", "color": "#9ca3af"}),
        ], className="filter-bar"),

        # 带宽趋势
        html.Div([
            dcc.Graph(id="bandwidth-chart", config={"displayModeBar": False})
        ], className="chart-card"),

        # 请求分布 + 命中率
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
                data=[],  # 数据由回调填充
                page_size=12,
                style_table={"overflowX": "auto"},
                style_cell={
                    "textAlign": "left",
                    "padding": "12px 16px",
                    "fontFamily": "Inter, sans-serif",
                    "fontSize": "13px",
                    "border": "none",
                    "borderBottom": "1px solid #f3f4f6",
                },
                style_header={
                    "backgroundColor": "#f9fafb",
                    "color": "#6b7280",
                    "fontWeight": "600",
                    "fontSize": "12px",
                    "textTransform": "uppercase",
                    "letterSpacing": "0.5px",
                    "border": "none",
                    "borderBottom": "1px solid #e5e7eb",
                },
                style_data_conditional=[
                    {"if": {"row_index": "odd"}, "backgroundColor": "#fafafa"}
                ],
                sort_action="native",
                filter_action="native",
            )
        ], className="chart-card"),
    ])

    # 注册回调 - 主数据更新
    @app.callback(
        [
            Output("header-info", "children"),
            Output("summary-cards", "children"),
            Output("refresh-status", "children"),
            Output("bandwidth-chart", "figure"),
            Output("requests-chart", "figure"),
            Output("hitrate-chart", "figure"),
            Output("http-status-chart", "figure"),
            Output("bs-http-status-chart", "figure"),
            Output("domain-ranking-chart", "figure"),
            Output("origin-analysis-chart", "figure"),
            Output("data-table", "data"),
        ],
        [
            Input("date-range-picker", "start_date"),
            Input("date-range-picker", "end_date"),
            Input("domain-filter", "value"),
            Input("refresh-interval", "n_intervals")
        ]
    )
    def update_all(start_date, end_date, selected_domain, n_intervals):
        """定时刷新 + 筛选条件更新所有图表"""
        # 转换日期为时间戳（毫秒）
        try:
            if start_date:
                start_dt = datetime.strptime(start_date[:10], "%Y-%m-%d")
                start_time = int(start_dt.timestamp() * 1000)
            else:
                start_time = None

            if end_date:
                end_dt = datetime.strptime(end_date[:10], "%Y-%m-%d")
                # 结束日期取当天23:59:59
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
                {}, {}, {}, {}, {}, {}, {}, []
            )

        # 处理空数据情况
        if df.empty:
            return (
                "暂无数据", html.Div(), "无数据",
                {}, {}, {}, {}, {}, {}, {}, []
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

        # 1. 带宽和流量趋势
        time_agg = filtered_df.groupby("batch").agg({
            "bw_mbps": "sum", "flux_gb": "sum", "timestamp": "first"
        }).reset_index()

        bw_fig = make_subplots(specs=[[{"secondary_y": True}]])
        bw_fig.add_trace(
            go.Scatter(
                x=time_agg["timestamp"], y=time_agg["bw_mbps"],
                name="带宽", fill="tozeroy",
                line={"color": COLORS["primary"], "width": 2},
                fillcolor="rgba(59, 130, 246, 0.1)"
            ), secondary_y=False
        )
        bw_fig.add_trace(
            go.Scatter(
                x=time_agg["timestamp"], y=time_agg["flux_gb"],
                name="流量", line={"color": COLORS["success"], "width": 2, "dash": "dot"}
            ), secondary_y=True
        )
        bw_fig = apply_chart_style(bw_fig, "带宽与流量趋势")
        bw_fig.update_yaxes(title_text="带宽 (Mbps)", secondary_y=False, title_font={"size": 11})
        bw_fig.update_yaxes(title_text="流量 (GB)", secondary_y=True, title_font={"size": 11})

        # 2. 请求数分布
        req_agg = filtered_df.groupby("batch").agg({
            "req_num": "sum", "hit_num": "sum", "bs_num": "sum", "timestamp": "first"
        }).reset_index()

        req_fig = go.Figure()
        req_fig.add_trace(go.Bar(
            x=req_agg["timestamp"], y=req_agg["hit_num"], name="命中请求",
            marker_color=COLORS["info"], marker_line_width=0
        ))
        req_fig.add_trace(go.Bar(
            x=req_agg["timestamp"], y=req_agg["bs_num"], name="回源请求",
            marker_color=COLORS["warning"], marker_line_width=0
        ))
        req_fig = apply_chart_style(req_fig, "请求分布")
        req_fig.update_layout(barmode="stack", bargap=0.3)

        # 3. 命中率趋势
        hitrate_agg = filtered_df.groupby("batch").agg({
            "hit_rate": "mean", "timestamp": "first"
        }).reset_index()

        hitrate_fig = go.Figure()
        hitrate_fig.add_trace(go.Scatter(
            x=hitrate_agg["timestamp"], y=hitrate_agg["hit_rate"],
            mode="lines+markers", name="命中率",
            line={"color": COLORS["success"], "width": 2},
            marker={"size": 4, "color": COLORS["success"]}
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
            bw_fig, req_fig, hitrate_fig,
            http_fig, bs_http_fig,
            domain_fig, origin_fig,
            table_data.to_dict("records")
        )

    return app


def run_dashboard(host="0.0.0.0", port=8050, debug=False, data_file=None):
    """运行仪表板"""
    app = create_app(data_file)

    # 获取存储信息
    storage = get_storage()
    record_count = storage.get_record_count()
    min_time, max_time = storage.get_time_range()

    print("\n" + "=" * 60)
    print("  CDN Analytics Dashboard")
    print("=" * 60)
    print(f"  数据存储: SQLite")
    print(f"  记录数量: {record_count:,} 条")
    if min_time and max_time:
        min_dt = datetime.fromtimestamp(min_time / 1000).strftime("%Y-%m-%d %H:%M")
        max_dt = datetime.fromtimestamp(max_time / 1000).strftime("%Y-%m-%d %H:%M")
        print(f"  数据范围: {min_dt} - {max_dt}")
    print("=" * 60)
    print(f"  访问地址: http://127.0.0.1:{port}")
    print("=" * 60 + "\n")

    app.run(debug=debug, host=host, port=port)


if __name__ == "__main__":
    run_dashboard()
