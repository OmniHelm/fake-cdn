"""管理员任务中心页面与回调。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
from typing import Dict, List, Optional

import dash
from dash import Input, Output, State, dash_table, dcc, html
from dash.exceptions import PreventUpdate
from flask import session

from fake_cdn.core.config_manager import ConfigManagerError
from fake_cdn.core.job_service import JobService
from fake_cdn.core.tenant_config import JOB_ACTIVE_STATUSES, TenantConfigStore
from fake_cdn.dashboard.auth import is_admin_session

MODE_LABELS = {
    "simulation": "完整模拟",
    "catchup": "历史补推",
    "realtime": "实时推送",
}
STATUS_LABELS = {
    "queued": "排队中",
    "running": "运行中",
    "cancel_requested": "停止中",
    "succeeded": "已成功",
    "failed": "已失败",
    "cancelled": "已取消",
    "interrupted": "已中断",
}


def _icon(name: str):
    return html.Span(name, className="material-symbols-outlined", **{"aria-hidden": "true"})


def _format_time(value: Optional[str]) -> str:
    if not value:
        return "-"
    try:
        resolved = datetime.fromisoformat(value)
    except ValueError:
        return value
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _duration(job: Dict) -> str:
    start_value = job.get("started_at") or job.get("created_at")
    end_value = job.get("finished_at")
    try:
        start = datetime.fromisoformat(start_value)
        end = datetime.fromisoformat(end_value) if end_value else datetime.now(timezone.utc)
        seconds = max(0, int((end - start).total_seconds()))
    except (TypeError, ValueError):
        return "-"
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds // 3600} 小时 {(seconds % 3600) // 60} 分"


def _job_rows(jobs: List[Dict]) -> List[Dict]:
    return [
        {
            "id": job["job_id"],
            "job": job["job_id"][-14:],
            "tenant": job["tenant_id"],
            "version": f"v{job['version_no']}",
            "mode": MODE_LABELS.get(job["mode"], job["mode"]),
            "status": STATUS_LABELS.get(job["status"], job["status"]),
            "created_by": job["created_by"],
            "created_at": _format_time(job["created_at"]),
            "duration": _duration(job),
        }
        for job in jobs
    ]


def _summary_strip(counts: Dict[str, int]):
    items = [
        ("全部任务", counts.get("all", 0)),
        ("活动任务", counts.get("active", 0)),
        ("已成功", counts.get("succeeded", 0)),
        ("异常结束", counts.get("failed", 0) + counts.get("interrupted", 0)),
    ]
    return [
        html.Div(
            [html.Span(label), html.Strong(f"{value:,}")],
            className="jobs-summary-item",
        )
        for label, value in items
    ]


def _view_revision(jobs, counts, online, filters):
    """生成任务列表可见状态摘要，避免轮询时重复渲染相同内容。"""
    active = any(job["status"] in JOB_ACTIVE_STATUSES for job in jobs)
    payload = {
        "filters": filters,
        "online": online,
        "counts": counts,
        "active_bucket": (int(datetime.now(tz=timezone.utc).timestamp() // 5) if active else None),
        "jobs": [
            [
                job.get("job_id"),
                job.get("tenant_id"),
                job.get("version_no"),
                job.get("mode"),
                job.get("status"),
                job.get("created_by"),
                job.get("created_at"),
                job.get("started_at"),
                job.get("finished_at"),
            ]
            for job in jobs
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _snapshot_preview(store: TenantConfigStore, tenant_id: Optional[str]):
    if not tenant_id:
        return html.Div("请选择租户", className="jobs-empty-inline")
    try:
        snapshot = JobService(store).active_snapshot(tenant_id)
    except ConfigManagerError as exc:
        return html.Div(str(exc), className="jobs-inline-alert danger")
    mode_label = "Dry-Run" if snapshot["dry_run"] else "真实推送"
    return html.Div(
        [
            html.Div([html.Span("发布版本"), html.Strong(f"v{snapshot['version_no']}")]),
            html.Div([html.Span("执行模式"), html.Strong(mode_label)]),
            html.Div(
                [html.Span("预计记录"), html.Strong(f"{snapshot['estimated_record_count']:,}")]
            ),
            html.Div(
                [
                    html.Span("配置窗口"),
                    html.Strong(f"{snapshot['time_start']} - {snapshot['time_end']}"),
                ]
            ),
            html.Div([html.Span("配置校验和"), html.Code(snapshot["checksum"][:16])]),
        ],
        className="jobs-version-preview",
    )


def _job_detail(store: TenantConfigStore, job_id: Optional[str]):
    if not job_id:
        return html.Div(
            [_icon("ads_click"), html.Span("选择一项任务查看详情")],
            className="jobs-detail-empty",
        )
    job = store.get_job(job_id)
    try:
        log_tail = JobService(store).read_log_tail(job_id)
    except ConfigManagerError as exc:
        log_tail = str(exc)
    parameters = {key: value for key, value in job["parameters"].items() if key != "push_confirmed"}
    return html.Div(
        [
            html.Div(
                [
                    html.Div(
                        [
                            html.Span(
                                STATUS_LABELS.get(job["status"], job["status"]),
                                className=f"jobs-status {job['status']}",
                            ),
                            html.Code(job["job_id"]),
                        ],
                        className="jobs-detail-identity",
                    ),
                    html.Div(
                        [
                            html.Div([html.Span("租户"), html.Strong(job["tenant_id"])]),
                            html.Div([html.Span("配置"), html.Strong(f"v{job['version_no']}")]),
                            html.Div(
                                [
                                    html.Span("模式"),
                                    html.Strong(MODE_LABELS.get(job["mode"], job["mode"])),
                                ]
                            ),
                            html.Div([html.Span("创建人"), html.Strong(job["created_by"])]),
                            html.Div(
                                [
                                    html.Span("创建时间"),
                                    html.Strong(_format_time(job["created_at"])),
                                ]
                            ),
                            html.Div([html.Span("耗时"), html.Strong(_duration(job))]),
                        ],
                        className="jobs-detail-grid",
                    ),
                ],
                className="jobs-detail-meta",
            ),
            html.Div(
                [
                    html.H4("任务参数"),
                    html.Pre(
                        json.dumps(parameters, ensure_ascii=False, indent=2),
                        className="jobs-json",
                    ),
                ],
                className="jobs-detail-section",
            ),
            (
                html.Div(
                    [
                        html.H4("执行结果"),
                        html.Pre(
                            json.dumps(job["stats"], ensure_ascii=False, indent=2),
                            className="jobs-json",
                        ),
                    ],
                    className="jobs-detail-section",
                )
                if job.get("stats")
                else None
            ),
            (
                html.Div(
                    [html.H4("错误"), html.Pre(job["error_text"], className="jobs-error")],
                    className="jobs-detail-section",
                )
                if job.get("error_text")
                else None
            ),
            html.Div(
                [
                    html.H4("运行日志"),
                    html.Pre(log_tail or "暂无日志", className="jobs-log"),
                ],
                className="jobs-detail-section",
            ),
        ],
        className="jobs-detail-content",
    )


def create_job_page(store: TenantConfigStore, initial_tenant_id: Optional[str] = None):
    tenants = [item for item in store.list_tenants() if item["status"] == "active"]
    tenant_options = [
        {"label": f"{item['display_name']} ({item['tenant_id']})", "value": item["tenant_id"]}
        for item in tenants
        if item.get("active_version_id")
    ]
    tenant_values = {item["value"] for item in tenant_options}
    selected_tenant = (
        initial_tenant_id
        if initial_tenant_id in tenant_values
        else (tenant_options[0]["value"] if tenant_options else None)
    )
    initial_filter = selected_tenant if initial_tenant_id in tenant_values else ""
    return html.Div(
        [
            dcc.Interval(id="jobs-refresh-interval", interval=5000, n_intervals=0),
            dcc.Store(id="jobs-create-token", data=0),
            dcc.Store(id="jobs-action-token", data=0),
            dcc.Store(id="jobs-view-revision"),
            dcc.Store(id="jobs-selected-id"),
            html.Div(
                [
                    html.Div([html.H2("任务中心")]),
                    html.Div(
                        [
                            html.Div(id="jobs-worker-status", className="jobs-worker-status"),
                            html.Button(
                                [_icon("add"), html.Span("新建任务")],
                                id="jobs-create-open",
                                n_clicks=0,
                                className="config-button primary large",
                                disabled=not tenant_options,
                            ),
                        ],
                        className="jobs-page-actions",
                    ),
                ],
                className="page-header jobs-header",
            ),
            html.Div(id="jobs-summary", className="jobs-summary-strip"),
            html.Div(
                [
                    dcc.Dropdown(
                        id="jobs-tenant-filter",
                        options=[{"label": "全部租户", "value": ""}] + tenant_options,
                        value=initial_filter,
                        clearable=False,
                        className="jobs-filter",
                    ),
                    dcc.Dropdown(
                        id="jobs-mode-filter",
                        options=[{"label": "全部模式", "value": ""}]
                        + [
                            {"label": label, "value": value} for value, label in MODE_LABELS.items()
                        ],
                        value="",
                        clearable=False,
                        className="jobs-filter",
                    ),
                    dcc.Dropdown(
                        id="jobs-status-filter",
                        options=[{"label": "全部状态", "value": ""}]
                        + [
                            {"label": label, "value": value}
                            for value, label in STATUS_LABELS.items()
                        ],
                        value="",
                        clearable=False,
                        className="jobs-filter",
                    ),
                ],
                className="jobs-toolbar",
            ),
            html.Div(
                dash_table.DataTable(
                    id="jobs-table",
                    columns=[
                        {"name": "任务", "id": "job"},
                        {"name": "租户", "id": "tenant"},
                        {"name": "版本", "id": "version"},
                        {"name": "模式", "id": "mode"},
                        {"name": "状态", "id": "status"},
                        {"name": "创建人", "id": "created_by"},
                        {"name": "创建时间", "id": "created_at"},
                        {"name": "耗时", "id": "duration"},
                    ],
                    data=[],
                    row_selectable="single",
                    selected_row_ids=[],
                    page_size=15,
                    sort_action="native",
                    style_as_list_view=True,
                    style_cell={
                        "padding": "12px 10px",
                        "fontFamily": "inherit",
                        "fontSize": "12px",
                        "textAlign": "left",
                        "border": "0",
                        "borderBottom": "1px solid #edf1f5",
                        "whiteSpace": "normal",
                        "height": "48px",
                    },
                    style_header={
                        "backgroundColor": "#f7f9fb",
                        "color": "#526071",
                        "fontWeight": "600",
                        "border": "0",
                        "borderBottom": "1px solid #dfe6ed",
                    },
                    style_data_conditional=[
                        {
                            "if": {"filter_query": '{status} = "已成功"', "column_id": "status"},
                            "color": "#047857",
                            "fontWeight": "600",
                        },
                        {
                            "if": {"filter_query": '{status} = "运行中"', "column_id": "status"},
                            "color": "#0369a1",
                            "fontWeight": "600",
                        },
                        {
                            "if": {
                                "filter_query": '{status} = "已失败" || {status} = "已中断"',
                                "column_id": "status",
                            },
                            "color": "#b91c1c",
                            "fontWeight": "600",
                        },
                    ],
                ),
                className="jobs-table-shell",
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.H3("任务详情"),
                            html.Div(
                                [
                                    html.Button(
                                        [_icon("stop_circle"), html.Span("停止")],
                                        id="jobs-cancel",
                                        n_clicks=0,
                                        className="config-button secondary",
                                        disabled=True,
                                    ),
                                    html.Button(
                                        [_icon("replay"), html.Span("重试")],
                                        id="jobs-retry",
                                        n_clicks=0,
                                        className="config-button secondary",
                                        disabled=True,
                                    ),
                                ],
                                className="jobs-detail-actions",
                            ),
                        ],
                        className="jobs-detail-heading",
                    ),
                    html.Div(id="jobs-action-result", role="status"),
                    html.Label(
                        [
                            html.Span("真实推送重试确认"),
                            dcc.Input(
                                id="jobs-retry-confirmation",
                                type="text",
                                className="config-input",
                                placeholder="输入租户 ID",
                                autoComplete="off",
                            ),
                        ],
                        id="jobs-retry-confirm-field",
                        className="jobs-retry-confirm hidden",
                    ),
                    html.Div(id="jobs-detail", children=_job_detail(store, None)),
                ],
                className="jobs-detail-panel",
            ),
            html.Div(id="jobs-create-result", role="status"),
            html.Div(
                [
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.H3("新建任务"),
                                    html.Button(
                                        _icon("close"),
                                        id="jobs-create-close",
                                        n_clicks=0,
                                        className="jobs-icon-button",
                                        title="关闭",
                                    ),
                                ],
                                className="jobs-modal-title",
                            ),
                            html.Div(
                                [
                                    html.Label(
                                        [
                                            html.Span("租户"),
                                            dcc.Dropdown(
                                                id="jobs-create-tenant",
                                                options=tenant_options,
                                                value=selected_tenant,
                                                clearable=False,
                                                className="config-dropdown",
                                            ),
                                        ],
                                        className="jobs-field",
                                    ),
                                    html.Label(
                                        [
                                            html.Span("任务模式"),
                                            dcc.RadioItems(
                                                id="jobs-create-mode",
                                                options=[
                                                    {"label": label, "value": value}
                                                    for value, label in MODE_LABELS.items()
                                                ],
                                                value="simulation",
                                                className="jobs-segmented",
                                            ),
                                        ],
                                        className="jobs-field full",
                                    ),
                                    html.Label(
                                        [
                                            html.Span("开始时间"),
                                            dcc.Input(
                                                id="jobs-start-datetime",
                                                type="datetime-local",
                                                step=300,
                                                className="config-input",
                                            ),
                                        ],
                                        id="jobs-start-field",
                                        className="jobs-field hidden",
                                    ),
                                    html.Label(
                                        [
                                            html.Span("结束时间"),
                                            dcc.Input(
                                                id="jobs-end-datetime",
                                                type="datetime-local",
                                                step=300,
                                                className="config-input",
                                            ),
                                        ],
                                        id="jobs-end-field",
                                        className="jobs-field hidden",
                                    ),
                                    html.Div(
                                        dcc.Checklist(
                                            id="jobs-once",
                                            options=[
                                                {"label": "仅执行当前时间点", "value": "once"}
                                            ],
                                            value=[],
                                            className="jobs-check",
                                        ),
                                        id="jobs-once-field",
                                        className="jobs-field hidden",
                                    ),
                                    html.Div(
                                        dcc.Checklist(
                                            id="jobs-force-dry-run",
                                            options=[{"label": "强制 Dry-Run", "value": "dry"}],
                                            value=[],
                                            className="jobs-check",
                                        ),
                                        className="jobs-field",
                                    ),
                                    html.Div(
                                        _snapshot_preview(store, selected_tenant),
                                        id="jobs-config-preview",
                                        className="jobs-preview-shell",
                                    ),
                                    html.Label(
                                        [
                                            html.Span("真实推送确认"),
                                            dcc.Input(
                                                id="jobs-push-confirmation",
                                                type="text",
                                                className="config-input",
                                                placeholder="输入租户 ID",
                                                autoComplete="off",
                                            ),
                                        ],
                                        id="jobs-confirm-field",
                                        className="jobs-field full hidden",
                                    ),
                                ],
                                className="jobs-form-grid",
                            ),
                            html.Div(id="jobs-modal-validation", role="status"),
                            html.Div(
                                [
                                    html.Button(
                                        "取消",
                                        id="jobs-create-cancel",
                                        n_clicks=0,
                                        className="config-button secondary large",
                                    ),
                                    html.Button(
                                        [_icon("play_arrow"), html.Span("加入队列")],
                                        id="jobs-create-submit",
                                        n_clicks=0,
                                        className="config-button primary large",
                                    ),
                                ],
                                className="jobs-modal-actions",
                            ),
                        ],
                        className="jobs-modal",
                    )
                ],
                id="jobs-create-modal",
                className="jobs-modal-backdrop hidden",
            ),
        ],
        className="jobs-page",
    )


def register_job_callbacks(app: dash.Dash, store: TenantConfigStore) -> None:
    service = JobService(store)

    @app.callback(
        [
            Output("jobs-table", "data"),
            Output("jobs-summary", "children"),
            Output("jobs-worker-status", "children"),
            Output("jobs-worker-status", "className"),
            Output("jobs-view-revision", "data"),
        ],
        [
            Input("jobs-refresh-interval", "n_intervals"),
            Input("jobs-create-token", "data"),
            Input("jobs-action-token", "data"),
            Input("jobs-tenant-filter", "value"),
            Input("jobs-mode-filter", "value"),
            Input("jobs-status-filter", "value"),
        ],
        [State("jobs-view-revision", "data")],
    )
    def refresh_jobs(_interval, _created, _action, tenant_id, mode, status, current_revision):
        if not is_admin_session():
            return (
                [],
                [],
                [_icon("lock"), html.Span("无权限")],
                "jobs-worker-status offline",
                "unauthorized",
            )
        jobs = store.list_jobs(
            tenant_id=tenant_id or None,
            mode=mode or None,
            status=status or None,
            limit=500,
        )
        online = service.worker_online()
        counts = service.counts()
        revision = _view_revision(jobs, counts, online, [tenant_id, mode, status])
        if dash.ctx.triggered_id == "jobs-refresh-interval" and revision == current_revision:
            return (dash.no_update,) * 5
        worker_status = [_icon("memory"), html.Span("Worker 在线" if online else "Worker 离线")]
        worker_class = f"jobs-worker-status {'online' if online else 'offline'}"
        return _job_rows(jobs), _summary_strip(counts), worker_status, worker_class, revision

    @app.callback(
        [
            Output("jobs-config-preview", "children"),
            Output("jobs-start-field", "className"),
            Output("jobs-end-field", "className"),
            Output("jobs-once-field", "className"),
            Output("jobs-confirm-field", "className"),
            Output("jobs-create-submit", "children"),
        ],
        [
            Input("jobs-create-tenant", "value"),
            Input("jobs-create-mode", "value"),
            Input("jobs-force-dry-run", "value"),
        ],
    )
    def update_create_form(tenant_id, mode, force_dry_values):
        if not is_admin_session():
            return (
                html.Div("无权限", className="jobs-empty-inline"),
                "jobs-field hidden",
                "jobs-field hidden",
                "jobs-field hidden",
                "jobs-field full hidden",
                [_icon("lock"), html.Span("无权限")],
            )
        preview = _snapshot_preview(store, tenant_id)
        start_class = "jobs-field" if mode == "catchup" else "jobs-field hidden"
        end_class = "jobs-field" if mode in {"catchup", "realtime"} else "jobs-field hidden"
        once_class = "jobs-field" if mode == "realtime" else "jobs-field hidden"
        requires_confirmation = False
        if tenant_id:
            try:
                requires_confirmation = not service.active_snapshot(tenant_id)["dry_run"]
            except ConfigManagerError:
                pass
        if "dry" in (force_dry_values or []):
            requires_confirmation = False
        confirm_class = "jobs-field full" if requires_confirmation else "jobs-field full hidden"
        label = "确认推送" if requires_confirmation else "加入队列"
        return (
            preview,
            start_class,
            end_class,
            once_class,
            confirm_class,
            [_icon("play_arrow"), html.Span(label)],
        )

    @app.callback(
        [
            Output("jobs-create-modal", "className"),
            Output("jobs-modal-validation", "children"),
            Output("jobs-modal-validation", "className"),
            Output("jobs-create-result", "children"),
            Output("jobs-create-result", "className"),
            Output("jobs-create-token", "data"),
        ],
        [
            Input("jobs-create-open", "n_clicks"),
            Input("jobs-create-close", "n_clicks"),
            Input("jobs-create-cancel", "n_clicks"),
            Input("jobs-create-submit", "n_clicks"),
        ],
        [
            State("jobs-create-tenant", "value"),
            State("jobs-create-mode", "value"),
            State("jobs-start-datetime", "value"),
            State("jobs-end-datetime", "value"),
            State("jobs-once", "value"),
            State("jobs-force-dry-run", "value"),
            State("jobs-push-confirmation", "value"),
            State("jobs-create-token", "data"),
        ],
        prevent_initial_call=True,
    )
    def manage_create_modal(
        _open,
        _close,
        _cancel,
        _submit,
        tenant_id,
        mode,
        start_datetime,
        end_datetime,
        once_values,
        force_dry_values,
        confirmation,
        token,
    ):
        trigger = dash.ctx.triggered_id
        no_validation = (None, "")
        no_result = (dash.no_update, dash.no_update)
        if trigger == "jobs-create-open":
            return "jobs-modal-backdrop", *no_validation, *no_result, dash.no_update
        if trigger in {"jobs-create-close", "jobs-create-cancel"}:
            return "jobs-modal-backdrop hidden", *no_validation, *no_result, dash.no_update
        if trigger != "jobs-create-submit":
            raise PreventUpdate
        if not is_admin_session():
            return (
                "jobs-modal-backdrop",
                "仅管理员可以创建任务。",
                "jobs-inline-alert danger",
                *no_result,
                dash.no_update,
            )
        parameters: Dict = {"force_dry_run": "dry" in (force_dry_values or [])}
        if mode == "catchup":
            parameters.update({"start_datetime": start_datetime, "end_datetime": end_datetime})
        elif mode == "realtime":
            parameters.update({"once": "once" in (once_values or []), "end_datetime": end_datetime})
            if not end_datetime:
                parameters.pop("end_datetime")
        try:
            job = service.enqueue(
                tenant_id,
                mode,
                parameters,
                actor=session.get("username", "dashboard"),
                push_confirmed=confirmation == tenant_id,
            )
        except (ConfigManagerError, TypeError, ValueError) as exc:
            return (
                "jobs-modal-backdrop",
                str(exc),
                "jobs-inline-alert danger",
                *no_result,
                dash.no_update,
            )
        result = html.Div(
            [_icon("check_circle"), html.Span(f"任务 {job['job_id'][-14:]} 已加入队列")]
        )
        return (
            "jobs-modal-backdrop hidden",
            None,
            "",
            result,
            "jobs-toast success",
            int(token or 0) + 1,
        )

    @app.callback(
        [
            Output("jobs-selected-id", "data"),
            Output("jobs-detail", "children"),
            Output("jobs-cancel", "disabled"),
            Output("jobs-retry", "disabled"),
            Output("jobs-retry-confirm-field", "className"),
        ],
        [
            Input("jobs-table", "selected_row_ids"),
            Input("jobs-table", "data"),
            Input("jobs-refresh-interval", "n_intervals"),
        ],
    )
    def show_job_detail(selected_ids, _rows, _interval):
        if not is_admin_session() or not selected_ids:
            if is_admin_session() and dash.ctx.triggered_id == "jobs-refresh-interval":
                raise PreventUpdate
            return None, _job_detail(store, None), True, True, "jobs-retry-confirm hidden"
        job_id = selected_ids[0]
        try:
            job = store.get_job(job_id)
        except ConfigManagerError:
            return None, _job_detail(store, None), True, True, "jobs-retry-confirm hidden"
        cancel_disabled = job["status"] not in JOB_ACTIVE_STATUSES
        retry_disabled = job["status"] in JOB_ACTIVE_STATUSES
        confirmation_class = (
            "jobs-retry-confirm"
            if not retry_disabled and not job["parameters"].get("effective_dry_run", True)
            else "jobs-retry-confirm hidden"
        )
        return (
            job_id,
            _job_detail(store, job_id),
            cancel_disabled,
            retry_disabled,
            confirmation_class,
        )

    @app.callback(
        [
            Output("jobs-action-result", "children"),
            Output("jobs-action-result", "className"),
            Output("jobs-action-token", "data"),
        ],
        [Input("jobs-cancel", "n_clicks"), Input("jobs-retry", "n_clicks")],
        [
            State("jobs-selected-id", "data"),
            State("jobs-action-token", "data"),
            State("jobs-retry-confirmation", "value"),
        ],
        prevent_initial_call=True,
    )
    def perform_job_action(_cancel_clicks, _retry_clicks, job_id, token, retry_confirmation):
        if not is_admin_session():
            return "仅管理员可以操作任务。", "jobs-inline-alert danger", dash.no_update
        if not job_id:
            raise PreventUpdate
        trigger = dash.ctx.triggered_id
        try:
            if trigger == "jobs-cancel":
                job = service.request_cancel(job_id, actor=session.get("username", "dashboard"))
                message = f"任务状态已更新为 {STATUS_LABELS.get(job['status'], job['status'])}"
            elif trigger == "jobs-retry":
                source = store.get_job(job_id)
                job = service.retry(
                    job_id,
                    actor=session.get("username", "dashboard"),
                    push_confirmed=retry_confirmation == source["tenant_id"],
                )
                message = f"重试任务 {job['job_id'][-14:]} 已加入队列"
            else:
                raise PreventUpdate
        except (ConfigManagerError, TypeError, ValueError) as exc:
            return str(exc), "jobs-inline-alert danger", dash.no_update
        return message, "jobs-inline-alert success", int(token or 0) + 1
