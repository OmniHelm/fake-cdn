"""配置管理页面及其 Dash 回调。"""

from __future__ import annotations

import base64
import csv
import io
from copy import deepcopy
from typing import Dict, Iterable, List, Sequence

import dash
from dash import ALL, Input, Output, State, dash_table, dcc, html
from dash.exceptions import PreventUpdate
from flask import session

from fake_cdn.core.config_manager import (
    ConfigConflictError,
    ConfigManagerError,
    ConfigValidationError,
)
from fake_cdn.core.generator import DEFAULT_PROFILES
from fake_cdn.core.tenant_config import TenantConfigStore

STEPS = [
    "基础信息",
    "流量目标",
    "时间窗口",
    "域名与地区",
    "真实性参数",
    "校验发布",
]

PROFILE_OPTIONS = [
    {"label": "企业 B2B", "value": "enterprise_b2b"},
    {"label": "均衡平台", "value": "balanced_platform"},
    {"label": "游戏内容", "value": "game_content"},
]

WEEKDAYS = [
    ("mon", "周一"),
    ("tue", "周二"),
    ("wed", "周三"),
    ("thu", "周四"),
    ("fri", "周五"),
    ("sat", "周六"),
    ("sun", "周日"),
]

BOOLEAN_PATHS = {"mode.dry_run", "mode.save_local"}
INTEGER_PATHS = {
    "target.total_flux.base",
    "target.random_seed",
    "time.interval_seconds",
    "api.timeout",
    "api.retry",
    "api.batch_size",
}
FLOAT_PATHS = {
    "target.total_flux.value",
    "realism.day_noise_ratio",
    "realism.slot_noise_ratio",
    "realism.month_edge_boost",
    "realism.anomaly_probability",
    "realism.cache_hit_rate.min",
    "realism.cache_hit_rate.max",
    "realism.avg_object_size_kb.min",
    "realism.avg_object_size_kb.max",
    "realism.origin_fail_rate.min",
    "realism.origin_fail_rate.max",
    *(f"realism.weekday_factors.{key}" for key, _ in WEEKDAYS),
}
DATETIME_PATHS = {"time.start_datetime", "time.end_datetime"}


def _icon(name: str, class_name: str = ""):
    classes = "material-symbols-outlined"
    if class_name:
        classes += f" {class_name}"
    return html.Span(name, className=classes, **{"aria-hidden": "true"})


def _field_id(path: str) -> Dict[str, str]:
    return {"type": "config-field", "path": path}


def _nested(config: Dict, path: str, default=None):
    value = config
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def _set_nested(config: Dict, path: str, value) -> None:
    parts = path.split(".")
    target = config
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value


def _datetime_local(value: str) -> str:
    return str(value or "").replace(" ", "T")


def _input(path: str, value, *, input_type="text", **kwargs):
    return dcc.Input(
        id=_field_id(path),
        value=value,
        type=input_type,
        debounce=True,
        className="config-input",
        **kwargs,
    )


def _dropdown(path: str, value, options: Sequence[Dict], *, clearable=False):
    return dcc.Dropdown(
        id=_field_id(path),
        value=value,
        options=list(options),
        clearable=clearable,
        className="config-dropdown",
    )


def _check(path: str, checked: bool, label: str):
    return dcc.Checklist(
        id=_field_id(path),
        options=[{"label": label, "value": "enabled"}],
        value=["enabled"] if checked else [],
        className="config-check",
    )


def _field(label: str, control, hint: str = ""):
    children = [html.Span(label, className="config-field-label"), control]
    if hint:
        children.append(html.Small(hint))
    return html.Label(children, className="config-field")


def _step_panel(index: int, children: Iterable, active_step: int):
    class_name = "config-step-panel active" if index == active_step else "config-step-panel"
    return html.Section(list(children), id=f"config-panel-{index}", className=class_name)


def _percentage_rows(items: Sequence[Dict], name_key: str) -> List[Dict]:
    weights = [max(0.0, float(item.get("weight", 0))) for item in items]
    total = sum(weights)
    if not items:
        return []
    if total <= 0:
        percentages = [0.0 for _ in items]
    else:
        percentages = [round(weight / total * 100, 2) for weight in weights]
        percentages[-1] = round(100 - sum(percentages[:-1]), 2)

    rows = []
    for item, percentage in zip(items, percentages):
        rows.append(
            {
                name_key: item.get(name_key) or item.get("name", ""),
                "weight": percentage,
                "profile": item.get("profile", "balanced_platform"),
                "share": f"{percentage:.2f}%",
            }
        )
    return rows


def _domain_table(rows: Sequence[Dict]):
    return dash_table.DataTable(
        id="config-domain-table",
        data=list(rows),
        columns=[
            {"name": "域名", "id": "domain", "editable": True},
            {"name": "权重 (%)", "id": "weight", "type": "numeric", "editable": True},
            {"name": "业务画像", "id": "profile", "presentation": "dropdown", "editable": True},
            {"name": "预计占比", "id": "share", "editable": False},
        ],
        dropdown={"profile": {"options": PROFILE_OPTIONS}},
        editable=True,
        row_deletable=True,
        page_action="native",
        page_size=5,
        filter_action="native",
        filter_options={"case": "insensitive"},
        filter_query="",
        sort_action="native",
        style_table={"overflowX": "auto"},
        style_header={
            "backgroundColor": "#fbfcfe",
            "border": "none",
            "borderBottom": "1px solid #e2e8f0",
            "color": "#64748b",
            "fontWeight": 600,
            "padding": "13px 16px",
            "textAlign": "left",
        },
        style_cell={
            "backgroundColor": "#ffffff",
            "border": "none",
            "borderBottom": "1px solid #e7edf3",
            "color": "#334155",
            "fontFamily": "Inter, sans-serif",
            "fontSize": "12px",
            "padding": "13px 16px",
            "textAlign": "left",
            "minWidth": "105px",
            "maxWidth": "280px",
        },
        style_cell_conditional=[
            {"if": {"column_id": "domain"}, "minWidth": "180px", "fontWeight": 500},
            {"if": {"column_id": "weight"}, "width": "110px"},
            {"if": {"column_id": "profile"}, "minWidth": "190px"},
            {"if": {"column_id": "share"}, "width": "110px"},
        ],
        css=[
            {"selector": ".dash-filter", "rule": "display: none;"},
            {"selector": ".show-hide", "rule": "display: none;"},
        ],
    )


def _region_table(rows: Sequence[Dict]):
    return dash_table.DataTable(
        id="config-region-table",
        data=list(rows),
        columns=[
            {"name": "国家代码", "id": "country", "editable": True},
            {"name": "地区标识", "id": "region", "editable": True},
            {"name": "权重 (%)", "id": "weight", "type": "numeric", "editable": True},
        ],
        editable=True,
        row_deletable=True,
        style_header={
            "backgroundColor": "#fbfcfe",
            "border": "none",
            "borderBottom": "1px solid #e2e8f0",
            "color": "#64748b",
            "fontWeight": 600,
            "padding": "12px 16px",
            "textAlign": "left",
        },
        style_cell={
            "backgroundColor": "#ffffff",
            "border": "none",
            "borderBottom": "1px solid #e7edf3",
            "color": "#334155",
            "fontFamily": "Inter, sans-serif",
            "fontSize": "12px",
            "padding": "12px 16px",
            "textAlign": "left",
        },
    )


def _simple_step_heading(title: str, description: str):
    return html.Div([html.H3(title), html.P(description)], className="config-section-heading")


def _editor_summary(summary: Dict, domain_rows: Sequence[Dict], region_rows: Sequence[Dict]):
    total_weight = sum(float(row.get("weight") or 0) for row in domain_rows)
    region_weight = sum(float(row.get("weight") or 0) for row in region_rows)
    total_class = (
        "config-summary-value success"
        if abs(total_weight - 100) <= 0.01
        else "config-summary-value danger"
    )
    region_class = (
        "config-summary-value success"
        if abs(region_weight - 100) <= 0.01
        else "config-summary-value danger"
    )
    return [
        html.H3("校验摘要"),
        html.Div(
            [html.Span("域名数量"), html.Strong(f"{len(domain_rows):,}")],
            className="config-summary-item",
        ),
        html.Div(
            [html.Span("权重总计"), html.Strong(f"{total_weight:.2f}%", className=total_class)],
            className="config-summary-item",
        ),
        html.Div(
            [html.Span("地区数量"), html.Strong(f"{len(region_rows):,}")],
            className="config-summary-item",
        ),
        html.Div(
            [html.Span("地区权重"), html.Strong(f"{region_weight:.2f}%", className=region_class)],
            className="config-summary-item",
        ),
        html.Div(
            [html.Span("预计生成记录数"), html.Strong(f"{summary['estimated_record_count']:,}")],
            className="config-summary-item",
        ),
        html.P(
            "基于当前配置的预估值，保存配置不会立即生成或推送数据。",
            className="config-summary-note",
        ),
    ]


def _review_content(config: Dict, summary: Dict):
    deployment_mode = config.get("deployment", {}).get("mode", "preview")
    mode_text = (
        "预览 · 强制 Dry-Run" if deployment_mode == "preview" else "推送配置 · 执行时仍需 CLI 确认"
    )
    rows = [
        ("目标总流量", f"{summary['target_total_flux_pb']:.4f} PB"),
        ("时间点", f"{summary['slot_count']:,} 个"),
        ("域名 / 地区", f"{summary['domain_count']} / {summary['region_count']}"),
        ("预计记录数", f"{summary['estimated_record_count']:,} 条"),
        ("等效平均带宽", f"{summary['equivalent_average_gbps']:.2f} Gbps"),
        ("安全模式", mode_text),
    ]
    return html.Div(
        [
            html.Div(
                [
                    _icon("verified"),
                    html.Div(
                        [
                            html.Strong("配置校验通过"),
                            html.Span("字段、时间窗口和权重均满足保存条件。"),
                        ]
                    ),
                ],
                className="config-success-banner",
            ),
            html.Div(
                [html.Div([html.Span(label), html.Strong(value)]) for label, value in rows],
                className="config-review-grid",
            ),
        ]
    )


def create_config_page(manager: TenantConfigStore, tenant_id: str):
    """创建六步配置编辑器，默认打开与确认原型一致的第 4 步。"""
    try:
        snapshot = manager.load(tenant_id)
    except ConfigManagerError as exc:
        return html.Div(
            [
                html.Div(
                    [_icon("error"), html.Div([html.H3("配置加载失败"), html.P(str(exc))])],
                    className="config-load-error",
                )
            ],
            className="config-page",
        )

    config = snapshot["config"]
    active_step = 3
    domain_rows = _percentage_rows(
        [{"domain": item["name"], **item} for item in config["dimensions"]["domains"]],
        "domain",
    )
    region_rows = _percentage_rows(config["dimensions"]["regions"], "region")
    for row, source in zip(region_rows, config["dimensions"]["regions"]):
        row["country"] = source.get("country", "cn")
        row.pop("profile", None)
        row.pop("share", None)

    stepper_items = []
    for index, label in enumerate(STEPS):
        state = "complete" if index < active_step else "active" if index == active_step else ""
        stepper_items.append(
            html.Div(
                [
                    html.Button(
                        [
                            html.Span(
                                [
                                    html.Span(str(index + 1), className="step-index"),
                                    _icon("check", "step-check"),
                                ],
                                className="config-step-number",
                            ),
                            html.Span(label, className="config-step-label"),
                        ],
                        id=f"config-step-button-{index}",
                        className="config-step-button",
                        n_clicks=0,
                    ),
                    html.Span(className="config-step-line") if index < len(STEPS) - 1 else None,
                ],
                id=f"config-step-item-{index}",
                className=f"config-step-item {state}".strip(),
            )
        )

    step_one = _step_panel(
        0,
        [
            _simple_step_heading("基础信息", "租户 ID 是数据隔离边界，不可在配置中修改。"),
            html.Div(
                [
                    _field(
                        "配置名称",
                        _input(
                            "deployment.name", _nested(config, "deployment.name", "月度流量配置")
                        ),
                    ),
                    _field(
                        "部署平台",
                        _input(
                            "deployment.platform", _nested(config, "deployment.platform", "CUG")
                        ),
                    ),
                    _field(
                        "租户 ID",
                        _input(
                            "dimensions.tenant_id",
                            tenant_id,
                            disabled=True,
                        ),
                    ),
                    _field(
                        "项目",
                        _input("dimensions.project", _nested(config, "dimensions.project", "默认")),
                    ),
                ],
                className="config-form-grid",
            ),
        ],
        active_step,
    )

    step_two = _step_panel(
        1,
        [
            _simple_step_heading("流量目标", "设置总流量、计量进制和默认业务画像。"),
            html.Div(
                [
                    _field(
                        "目标总流量",
                        _input(
                            "target.total_flux.value",
                            _nested(config, "target.total_flux.value"),
                            input_type="number",
                            min=0.000001,
                            step="any",
                        ),
                    ),
                    _field(
                        "流量单位",
                        _dropdown(
                            "target.total_flux.unit",
                            _nested(config, "target.total_flux.unit", "PB"),
                            [{"label": unit, "value": unit} for unit in ("GB", "TB", "PB")],
                        ),
                    ),
                    _field(
                        "计量进制",
                        _dropdown(
                            "target.total_flux.base",
                            _nested(config, "target.total_flux.base", 1000),
                            [
                                {"label": "十进制 1000", "value": 1000},
                                {"label": "二进制 1024", "value": 1024},
                            ],
                        ),
                    ),
                    _field(
                        "默认业务画像",
                        _dropdown(
                            "target.profile",
                            _nested(config, "target.profile", "balanced_platform"),
                            PROFILE_OPTIONS,
                        ),
                    ),
                    _field(
                        "随机种子",
                        _input(
                            "target.random_seed",
                            _nested(config, "target.random_seed", 20260311),
                            input_type="number",
                            step=1,
                        ),
                        "相同配置与种子可复现同一批数据",
                    ),
                ],
                className="config-form-grid",
            ),
        ],
        active_step,
    )

    step_three = _step_panel(
        2,
        [
            _simple_step_heading("时间窗口", "设置计划覆盖的时间范围、时区和生成粒度。"),
            html.Div(
                [
                    _field(
                        "开始时间",
                        _input(
                            "time.start_datetime",
                            _datetime_local(_nested(config, "time.start_datetime")),
                            input_type="datetime-local",
                        ),
                    ),
                    _field(
                        "结束时间",
                        _input(
                            "time.end_datetime",
                            _datetime_local(_nested(config, "time.end_datetime")),
                            input_type="datetime-local",
                        ),
                    ),
                    _field(
                        "时区",
                        _dropdown(
                            "time.timezone",
                            _nested(config, "time.timezone", "Asia/Shanghai"),
                            [
                                {"label": value, "value": value}
                                for value in ("Asia/Shanghai", "Asia/Singapore", "UTC")
                            ],
                        ),
                    ),
                    _field(
                        "时间间隔（秒）",
                        _dropdown(
                            "time.interval_seconds",
                            _nested(config, "time.interval_seconds", 300),
                            [
                                {"label": f"{value} 秒", "value": value}
                                for value in (60, 300, 600, 900, 1800, 3600)
                            ],
                        ),
                    ),
                ],
                className="config-form-grid",
            ),
        ],
        active_step,
    )

    step_four = _step_panel(
        3,
        [
            html.P(
                "配置参与流量模拟的域名及其权重分配，系统将基于权重生成访问计划。",
                className="config-intro",
            ),
            html.Div(
                [
                    html.Section(
                        [
                            html.Div(
                                [
                                    html.Div(
                                        [
                                            html.Button(
                                                [_icon("content_paste"), html.Span("批量粘贴")],
                                                id="config-open-paste",
                                                className="config-button secondary",
                                                n_clicks=0,
                                            ),
                                            dcc.Upload(
                                                html.Button(
                                                    [_icon("upload"), html.Span("导入 CSV")],
                                                    className="config-button secondary",
                                                ),
                                                id="config-upload-csv",
                                                accept=".csv,text/csv",
                                                multiple=False,
                                            ),
                                            html.Button(
                                                [_icon("sync"), html.Span("平均分配权重")],
                                                id="config-average-weights",
                                                className="config-button secondary",
                                                n_clicks=0,
                                            ),
                                        ],
                                        className="config-toolbar-actions",
                                    ),
                                    html.Label(
                                        [
                                            dcc.Input(
                                                id="config-domain-search",
                                                placeholder="搜索域名",
                                                debounce=True,
                                            ),
                                            _icon("search"),
                                        ],
                                        className="config-search",
                                    ),
                                ],
                                className="config-table-toolbar",
                            ),
                            _domain_table(domain_rows),
                            html.Button(
                                [_icon("add"), "添加域名"],
                                id="config-add-domain",
                                className="config-add-row",
                                n_clicks=0,
                            ),
                            html.Div(id="config-domain-stats", className="config-table-total"),
                            html.Div(
                                [
                                    _icon("warning"),
                                    html.Span(
                                        "修改域名或权重将重新生成访问计划，可能导致历史计划失效，请谨慎调整。"
                                    ),
                                ],
                                className="config-warning",
                            ),
                        ],
                        className="config-table-card",
                    ),
                    html.Aside(id="config-editor-summary", className="config-summary-card"),
                ],
                className="config-editor-grid",
            ),
            html.Details(
                [
                    html.Summary(
                        [
                            html.Span(
                                [
                                    html.Strong("地区分配"),
                                    html.Em(f"（已配置 {len(region_rows)} 个地区）"),
                                ]
                            ),
                            _icon("expand_more"),
                        ]
                    ),
                    _region_table(region_rows),
                    html.Button(
                        [_icon("add"), "添加地区"],
                        id="config-add-region",
                        className="config-add-row",
                        n_clicks=0,
                    ),
                ],
                className="config-region-card",
                open=True,
            ),
        ],
        active_step,
    )

    weekday_fields = [
        _field(
            label,
            _input(
                f"realism.weekday_factors.{key}",
                _nested(config, f"realism.weekday_factors.{key}"),
                input_type="number",
                min=0,
                step=0.01,
            ),
        )
        for key, label in WEEKDAYS
    ]
    step_five = _step_panel(
        4,
        [
            _simple_step_heading("真实性参数", "调整工作日规律、噪声、缓存和异常事件概率。"),
            html.H4("星期倍率", className="config-subheading"),
            html.Div(weekday_fields, className="config-form-grid weekday"),
            html.H4("波动与异常", className="config-subheading"),
            html.Div(
                [
                    _field(
                        "日级噪声",
                        _input(
                            "realism.day_noise_ratio",
                            _nested(config, "realism.day_noise_ratio"),
                            input_type="number",
                            min=0,
                            max=1,
                            step=0.001,
                        ),
                    ),
                    _field(
                        "时间片噪声",
                        _input(
                            "realism.slot_noise_ratio",
                            _nested(config, "realism.slot_noise_ratio"),
                            input_type="number",
                            min=0,
                            max=1,
                            step=0.001,
                        ),
                    ),
                    _field(
                        "月初月末增益",
                        _input(
                            "realism.month_edge_boost",
                            _nested(config, "realism.month_edge_boost"),
                            input_type="number",
                            min=0,
                            max=1,
                            step=0.001,
                        ),
                    ),
                    _field(
                        "异常概率",
                        _input(
                            "realism.anomaly_probability",
                            _nested(config, "realism.anomaly_probability"),
                            input_type="number",
                            min=0,
                            max=1,
                            step=0.0001,
                        ),
                    ),
                ],
                className="config-form-grid",
            ),
            html.H4("指标范围", className="config-subheading"),
            html.Div(
                [
                    _field(
                        "缓存命中率下限",
                        _input(
                            "realism.cache_hit_rate.min",
                            _nested(config, "realism.cache_hit_rate.min"),
                            input_type="number",
                            min=0,
                            max=1,
                            step=0.001,
                        ),
                    ),
                    _field(
                        "缓存命中率上限",
                        _input(
                            "realism.cache_hit_rate.max",
                            _nested(config, "realism.cache_hit_rate.max"),
                            input_type="number",
                            min=0,
                            max=1,
                            step=0.001,
                        ),
                    ),
                    _field(
                        "对象大小下限（KB）",
                        _input(
                            "realism.avg_object_size_kb.min",
                            _nested(config, "realism.avg_object_size_kb.min"),
                            input_type="number",
                            min=1,
                            step=1,
                        ),
                    ),
                    _field(
                        "对象大小上限（KB）",
                        _input(
                            "realism.avg_object_size_kb.max",
                            _nested(config, "realism.avg_object_size_kb.max"),
                            input_type="number",
                            min=1,
                            step=1,
                        ),
                    ),
                    _field(
                        "回源失败率下限",
                        _input(
                            "realism.origin_fail_rate.min",
                            _nested(config, "realism.origin_fail_rate.min"),
                            input_type="number",
                            min=0,
                            max=1,
                            step=0.0001,
                        ),
                    ),
                    _field(
                        "回源失败率上限",
                        _input(
                            "realism.origin_fail_rate.max",
                            _nested(config, "realism.origin_fail_rate.max"),
                            input_type="number",
                            min=0,
                            max=1,
                            step=0.0001,
                        ),
                    ),
                ],
                className="config-form-grid",
            ),
        ],
        active_step,
    )

    step_six = _step_panel(
        5,
        [
            _simple_step_heading("校验发布", "先保存不可变草稿，再明确发布为租户生效版本。"),
            html.Div(
                [
                    _field(
                        "部署模式",
                        _dropdown(
                            "deployment.mode",
                            _nested(config, "deployment.mode", "preview"),
                            [
                                {"label": "预览（不推送）", "value": "preview"},
                                {"label": "推送配置（执行时需再次确认）", "value": "push"},
                            ],
                        ),
                    ),
                    _field(
                        "输出目录",
                        _input("mode.output_dir", _nested(config, "mode.output_dir", "./output")),
                    ),
                    _field(
                        "安全选项",
                        html.Div(
                            [
                                _check(
                                    "mode.dry_run",
                                    bool(_nested(config, "mode.dry_run", True)),
                                    "启用 Dry-Run",
                                ),
                                _check(
                                    "mode.save_local",
                                    bool(_nested(config, "mode.save_local", True)),
                                    "保存本地数据",
                                ),
                            ],
                            className="config-check-group",
                        ),
                    ),
                ],
                className="config-form-grid compact",
            ),
            html.Div(id="config-review-content", className="config-review-card"),
            html.Div(id="config-inline-validation", role="status"),
        ],
        active_step,
    )

    return html.Div(
        [
            dcc.Store(id="config-active-step", data=active_step),
            dcc.Store(id="config-tenant-id", data=tenant_id),
            dcc.Store(id="config-revision", data=snapshot["revision"]),
            dcc.Store(id="config-base", data=config),
            html.Div(
                [
                    html.H2(f"{snapshot['display_name']} · 流量配置"),
                    html.Span(
                        f"{tenant_id} · v{snapshot['version_no']} · {snapshot['status']} · "
                        f"{snapshot['checksum'][:12]}",
                        className="config-path",
                    ),
                ],
                className="config-page-heading",
            ),
            html.Div(
                stepper_items, className="config-stepper", role="list", **{"aria-label": "配置步骤"}
            ),
            html.Div(
                [step_one, step_two, step_three, step_four, step_five, step_six],
                className="config-panels",
            ),
            html.Div(
                [
                    html.Div(id="config-save-alert", role="status", className="config-save-alert"),
                    html.Div(
                        [
                            html.Button(
                                "上一步",
                                id="config-prev",
                                className="config-button secondary large",
                                n_clicks=0,
                            ),
                            html.Button(
                                "保存草稿",
                                id="config-save",
                                className="config-button secondary large save",
                                n_clicks=0,
                            ),
                            html.Button(
                                "下一步：真实性参数",
                                id="config-next",
                                className="config-button primary large",
                                n_clicks=0,
                            ),
                        ],
                        className="config-footer-actions",
                    ),
                ],
                className="config-action-bar",
            ),
            html.Div(
                [
                    html.Section(
                        [
                            html.Div(
                                [
                                    html.Div(
                                        [
                                            html.H3("批量粘贴域名", id="config-paste-title"),
                                            html.P("每行格式：域名, 权重, 业务画像"),
                                        ]
                                    ),
                                    html.Button(
                                        _icon("close"),
                                        id="config-close-paste",
                                        className="config-icon-button",
                                        **{"aria-label": "关闭批量粘贴弹窗"},
                                    ),
                                ],
                                className="config-modal-title",
                            ),
                            dcc.Textarea(
                                id="config-paste-text",
                                value="novu.co,10,balanced_platform\nappwrite.io,8,enterprise_b2b",
                            ),
                            html.Div(
                                [
                                    html.Button(
                                        "取消",
                                        id="config-cancel-paste",
                                        className="config-button secondary",
                                    ),
                                    html.Button(
                                        "添加域名",
                                        id="config-apply-paste",
                                        className="config-button primary",
                                    ),
                                ],
                                className="config-modal-actions",
                            ),
                        ],
                        className="config-modal",
                        role="dialog",
                        **{"aria-modal": "true", "aria-labelledby": "config-paste-title"},
                    ),
                ],
                id="config-paste-backdrop",
                className="config-modal-backdrop hidden",
            ),
            html.Div(id="config-domain-action-message", className="config-domain-message"),
        ],
        className="config-page",
    )


def _coerce_field(path: str, value):
    if path in BOOLEAN_PATHS:
        return "enabled" in (value or [])
    if path in INTEGER_PATHS:
        return int(value)
    if path in FLOAT_PATHS:
        return float(value)
    if path in DATETIME_PATHS:
        return str(value or "").replace("T", " ")
    return value


def _build_candidate(
    base: Dict,
    field_ids: Sequence[Dict],
    field_values: Sequence,
    domain_rows: Sequence[Dict],
    region_rows: Sequence[Dict],
) -> Dict:
    candidate = deepcopy(base)
    for field_id, value in zip(field_ids, field_values):
        path = field_id["path"]
        try:
            converted = _coerce_field(path, value)
        except (TypeError, ValueError) as exc:
            raise ConfigValidationError(f"{path} 的值无效") from exc
        _set_nested(candidate, path, converted)

    domains = []
    for row in domain_rows or []:
        domain = str(row.get("domain") or "").strip().lower()
        profile = str(row.get("profile") or candidate["target"]["profile"])
        if profile not in DEFAULT_PROFILES:
            raise ConfigValidationError(f"不支持的业务画像: {profile}")
        domains.append(
            {"name": domain, "weight": float(row.get("weight") or 0), "profile": profile}
        )
    if abs(sum(item["weight"] for item in domains) - 100) > 0.01:
        raise ConfigValidationError("域名权重总计必须等于 100%")
    candidate.setdefault("dimensions", {})["domains"] = domains

    regions = []
    for row in region_rows or []:
        regions.append(
            {
                "country": str(row.get("country") or "cn").strip().lower(),
                "region": str(row.get("region") or "mainland_china").strip(),
                "weight": float(row.get("weight") or 0),
            }
        )
    if abs(sum(item["weight"] for item in regions) - 100) > 0.01:
        raise ConfigValidationError("地区权重总计必须等于 100%")
    candidate["dimensions"]["regions"] = regions
    return candidate


def _parse_domain_lines(text: str) -> List[Dict]:
    rows = []
    reader = csv.reader(io.StringIO(text or ""))
    for index, parts in enumerate(reader):
        if not parts or not str(parts[0]).strip():
            continue
        domain = str(parts[0]).strip().lower()
        if index == 0 and domain in {"domain", "域名"}:
            continue
        try:
            weight = float(parts[1]) if len(parts) > 1 and str(parts[1]).strip() else 0.0
        except ValueError as exc:
            raise ConfigValidationError(f"{domain} 的权重不是数字") from exc
        profile = (
            str(parts[2]).strip()
            if len(parts) > 2 and str(parts[2]).strip()
            else "balanced_platform"
        )
        rows.append(
            {"domain": domain, "weight": weight, "profile": profile, "share": f"{weight:.2f}%"}
        )
    return rows


def register_config_callbacks(app: dash.Dash, manager: TenantConfigStore) -> None:
    step_inputs = [Input(f"config-step-button-{index}", "n_clicks") for index in range(len(STEPS))]

    @app.callback(
        [
            Output("config-active-step", "data"),
            *[Output(f"config-panel-{index}", "className") for index in range(len(STEPS))],
            *[Output(f"config-step-item-{index}", "className") for index in range(len(STEPS))],
            Output("config-prev", "disabled"),
            Output("config-next", "children"),
        ],
        [*step_inputs, Input("config-prev", "n_clicks"), Input("config-next", "n_clicks")],
        [State("config-active-step", "data"), State(_field_id("deployment.mode"), "value")],
        prevent_initial_call=True,
    )
    def navigate_steps(*args):
        current_step = int(args[-2] or 0)
        deployment_mode = args[-1] or "preview"
        triggered = dash.callback_context.triggered_id
        if triggered == "config-prev":
            current_step = max(0, current_step - 1)
        elif triggered == "config-next":
            current_step = min(len(STEPS) - 1, current_step + 1)
        elif isinstance(triggered, str) and triggered.startswith("config-step-button-"):
            current_step = int(triggered.rsplit("-", 1)[1])

        panel_classes = [
            "config-step-panel active" if index == current_step else "config-step-panel"
            for index in range(len(STEPS))
        ]
        step_classes = []
        for index in range(len(STEPS)):
            state = (
                "complete" if index < current_step else "active" if index == current_step else ""
            )
            step_classes.append(f"config-step-item {state}".strip())

        if current_step == len(STEPS) - 1:
            next_label = "发布到预览环境" if deployment_mode == "preview" else "保存推送配置"
        else:
            next_label = f"下一步：{STEPS[current_step + 1]}"
        return current_step, *panel_classes, *step_classes, current_step == 0, next_label

    @app.callback(
        Output("config-domain-table", "filter_query"),
        Input("config-domain-search", "value"),
    )
    def filter_domain_table(search_value):
        if not search_value:
            return ""
        escaped = str(search_value).replace("\\", "\\\\").replace('"', '\\"')
        return f'{{domain}} contains "{escaped}"'

    @app.callback(
        [
            Output("config-domain-table", "data"),
            Output("config-paste-backdrop", "className"),
            Output("config-paste-text", "value"),
            Output("config-domain-action-message", "children"),
            Output("config-domain-action-message", "className"),
            Output("config-upload-csv", "contents"),
        ],
        [
            Input("config-open-paste", "n_clicks"),
            Input("config-close-paste", "n_clicks"),
            Input("config-cancel-paste", "n_clicks"),
            Input("config-apply-paste", "n_clicks"),
            Input("config-add-domain", "n_clicks"),
            Input("config-average-weights", "n_clicks"),
            Input("config-upload-csv", "contents"),
        ],
        [
            State("config-domain-table", "data"),
            State("config-paste-text", "value"),
            State("config-upload-csv", "filename"),
        ],
        prevent_initial_call=True,
    )
    def update_domains(
        _open,
        _close,
        _cancel,
        _apply,
        _add,
        _average,
        upload_contents,
        current_rows,
        paste_text,
        upload_name,
    ):
        triggered = dash.callback_context.triggered_id
        rows = list(current_rows or [])
        hidden = "config-modal-backdrop hidden"
        message = ""
        message_class = "config-domain-message"
        next_paste = dash.no_update

        if triggered == "config-open-paste":
            return (
                dash.no_update,
                "config-modal-backdrop",
                dash.no_update,
                "",
                message_class,
                dash.no_update,
            )
        if triggered in {"config-close-paste", "config-cancel-paste"}:
            return dash.no_update, hidden, dash.no_update, "", message_class, dash.no_update

        try:
            if triggered == "config-add-domain":
                existing = {row.get("domain") for row in rows}
                index = len(rows) + 1
                domain = f"new-domain-{index}.com"
                while domain in existing:
                    index += 1
                    domain = f"new-domain-{index}.com"
                rows.append(
                    {
                        "domain": domain,
                        "weight": 0,
                        "profile": "balanced_platform",
                        "share": "0.00%",
                    }
                )
                message = "已添加一行，请继续填写域名和权重。"
            elif triggered == "config-average-weights":
                if not rows:
                    raise ConfigValidationError("至少需要一个域名")
                average = round(100 / len(rows), 2)
                for index, row in enumerate(rows):
                    value = (
                        round(100 - average * (len(rows) - 1), 2)
                        if index == len(rows) - 1
                        else average
                    )
                    row["weight"] = value
                    row["share"] = f"{value:.2f}%"
                message = "已平均分配域名权重。"
            elif triggered == "config-apply-paste":
                parsed = _parse_domain_lines(paste_text)
                if not parsed:
                    raise ConfigValidationError("没有可添加的域名")
                rows.extend(parsed)
                next_paste = ""
                message = f"已批量添加 {len(parsed)} 个域名。"
            elif triggered == "config-upload-csv":
                if not upload_contents:
                    raise PreventUpdate
                _, encoded = upload_contents.split(",", 1)
                decoded = base64.b64decode(encoded, validate=True).decode("utf-8-sig")
                parsed = _parse_domain_lines(decoded)
                if not parsed:
                    raise ConfigValidationError("CSV 中没有可导入的域名")
                rows.extend(parsed)
                message = f"已从 {upload_name or 'CSV'} 导入 {len(parsed)} 个域名。"
            else:
                raise PreventUpdate
            message_class = "config-domain-message success"
        except (ConfigValidationError, UnicodeDecodeError, ValueError) as exc:
            return (
                dash.no_update,
                hidden,
                dash.no_update,
                str(exc),
                "config-domain-message danger",
                None,
            )

        return rows, hidden, next_paste, message, message_class, None

    @app.callback(
        Output("config-region-table", "data"),
        Input("config-add-region", "n_clicks"),
        State("config-region-table", "data"),
        prevent_initial_call=True,
    )
    def add_region(_clicks, rows):
        rows = list(rows or [])
        rows.append({"country": "cn", "region": f"region_{len(rows) + 1}", "weight": 0})
        return rows

    @app.callback(
        [
            Output("config-editor-summary", "children"),
            Output("config-domain-stats", "children"),
            Output("config-review-content", "children"),
            Output("config-inline-validation", "children"),
            Output("config-inline-validation", "className"),
        ],
        [
            Input(_field_id(ALL), "value"),
            Input("config-domain-table", "data"),
            Input("config-region-table", "data"),
        ],
        [State(_field_id(ALL), "id"), State("config-base", "data")],
    )
    def update_config_preview(field_values, domain_rows, region_rows, field_ids, base):
        domain_rows = domain_rows or []
        region_rows = region_rows or []
        total_weight = sum(float(row.get("weight") or 0) for row in domain_rows)
        table_stats = [
            html.Span(f"共 {len(domain_rows)} 个域名"),
            html.Span(
                [
                    "权重总计：",
                    html.Strong(
                        f"{total_weight:.2f}%",
                        className="success" if abs(total_weight - 100) <= 0.01 else "danger",
                    ),
                ]
            ),
            html.Span(f"预计占比合计：{total_weight:.2f}%"),
        ]
        try:
            candidate = _build_candidate(base, field_ids, field_values, domain_rows, region_rows)
            normalized = manager.validate(candidate)
            summary = manager.summarize(normalized)
            return (
                _editor_summary(summary, domain_rows, region_rows),
                table_stats,
                _review_content(normalized, summary),
                [_icon("check_circle"), html.Span("配置校验通过，可以安全保存。")],
                "config-inline-validation success",
            )
        except (ConfigManagerError, TypeError, ValueError) as exc:
            fallback_summary = {
                "estimated_record_count": 0,
            }
            return (
                _editor_summary(fallback_summary, domain_rows, region_rows),
                table_stats,
                html.Div([_icon("error"), html.Span(str(exc))], className="config-review-error"),
                [_icon("error"), html.Span(str(exc))],
                "config-inline-validation danger",
            )

    @app.callback(
        [
            Output("config-save-alert", "children"),
            Output("config-save-alert", "className"),
            Output("config-revision", "data"),
            Output("config-base", "data"),
        ],
        [Input("config-save", "n_clicks"), Input("config-next", "n_clicks")],
        [
            State("config-active-step", "data"),
            State("config-revision", "data"),
            State("config-base", "data"),
            State(_field_id(ALL), "id"),
            State(_field_id(ALL), "value"),
            State("config-domain-table", "data"),
            State("config-region-table", "data"),
            State("config-tenant-id", "data"),
        ],
        prevent_initial_call=True,
    )
    def save_config(
        _save_clicks,
        _next_clicks,
        active_step,
        revision,
        base,
        field_ids,
        field_values,
        domain_rows,
        region_rows,
        tenant_id,
    ):
        triggered = dash.callback_context.triggered_id
        if triggered == "config-next" and int(active_step or 0) != len(STEPS) - 1:
            raise PreventUpdate

        try:
            candidate = _build_candidate(base, field_ids, field_values, domain_rows, region_rows)
            actor = session.get("username", "dashboard")
            draft = manager.save_draft(
                tenant_id,
                candidate,
                expected_revision=revision,
                actor=actor,
            )
            if triggered == "config-next":
                saved = manager.publish(
                    tenant_id,
                    draft["id"],
                    expected_revision=draft["revision"],
                    actor=actor,
                )
                detail = f"已发布 v{saved['version_no']}，新的生成任务将使用此版本。"
            else:
                saved = draft
                detail = f"已保存草稿 v{saved['version_no']}，当前生效版本未改变。"
            return (
                [_icon("check_circle"), html.Div([html.Strong("配置保存成功"), html.Span(detail)])],
                "config-save-alert visible success",
                saved["revision"],
                saved["config"],
            )
        except ConfigConflictError as exc:
            return (
                [_icon("sync_problem"), html.Span(str(exc))],
                "config-save-alert visible danger",
                dash.no_update,
                dash.no_update,
            )
        except (ConfigManagerError, TypeError, ValueError) as exc:
            return (
                [_icon("error"), html.Span(str(exc))],
                "config-save-alert visible danger",
                dash.no_update,
                dash.no_update,
            )


def create_config_audit_page(manager: TenantConfigStore, tenant_id: str = None):
    try:
        records = manager.read_audit(tenant_id=tenant_id, limit=100)
    except ConfigManagerError as exc:
        records = []
        error = str(exc)
    else:
        error = ""

    rows = []
    for record in records:
        rows.append(
            {
                "timestamp": str(record.get("timestamp", ""))
                .replace("T", " ")
                .replace("+00:00", " UTC"),
                "actor": record.get("actor", "—"),
                "action": record.get("action", "—"),
                "tenant": record.get("tenant_id", tenant_id or "—"),
                "version": str(record.get("version_id") or "—"),
                "source": str(record.get("detail", {}).get("source_version_id") or "—"),
            }
        )

    content = []
    if error:
        content.append(
            html.Div([_icon("error"), error], className="config-inline-validation danger")
        )
    elif not rows:
        content.append(
            html.Div(
                [
                    _icon("history"),
                    html.H3("暂无配置变更"),
                    html.P("首次从配置管理页保存后，这里会显示版本与操作者。"),
                ],
                className="config-empty-audit",
            )
        )
    else:
        content.append(
            dash_table.DataTable(
                data=rows,
                columns=[
                    {"name": "时间", "id": "timestamp"},
                    {"name": "租户", "id": "tenant"},
                    {"name": "操作者", "id": "actor"},
                    {"name": "动作", "id": "action"},
                    {"name": "版本 ID", "id": "version"},
                    {"name": "来源版本 ID", "id": "source"},
                ],
                page_size=20,
                style_header={
                    "backgroundColor": "#fbfcfe",
                    "fontWeight": 600,
                    "border": "none",
                    "padding": "13px 16px",
                    "textAlign": "left",
                },
                style_cell={
                    "backgroundColor": "#fff",
                    "border": "none",
                    "borderBottom": "1px solid #e7edf3",
                    "padding": "13px 16px",
                    "fontFamily": "Inter, sans-serif",
                    "fontSize": "12px",
                    "textAlign": "left",
                },
            )
        )

    return html.Div(
        [
            html.Div(
                [
                    html.Div(
                        [
                            html.H2("配置审计日志"),
                            html.P(
                                f"追踪租户 {tenant_id} 的草稿、发布、回滚与生成任务。"
                                if tenant_id else "追踪所有租户的配置与生成操作。"
                            ),
                        ]
                    )
                ],
                className="page-header",
            ),
            html.Section(content, className="config-audit-card"),
        ]
    )
