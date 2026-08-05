"""租户列表、创建和配置版本操作页面。"""

from __future__ import annotations

from copy import deepcopy

import dash
from dash import Input, Output, State, dcc, html
from dash.exceptions import PreventUpdate
from flask import session

from fake_cdn.core.config_manager import ConfigManagerError
from fake_cdn.core.tenant_config import TenantConfigStore
from fake_cdn.dashboard.auth import is_admin_session


def create_tenant_list_page(store: TenantConfigStore):
    tenants = store.list_tenants()
    cards = []
    for tenant in tenants:
        tenant_id = tenant["tenant_id"]
        cards.append(
            html.Article(
                [
                    html.Div(
                        [
                            html.Span(tenant_id[:1].upper(), className="tenant-avatar"),
                            html.Div(
                                [
                                    html.H3(tenant["display_name"]),
                                    html.Code(tenant_id),
                                ]
                            ),
                        ],
                        className="tenant-card-identity",
                    ),
                    html.Div(
                        [
                            html.Span(
                                f"生效 v{tenant.get('active_version_no') or '—'}",
                                className="status-badge active",
                            ),
                            html.Span(
                                (
                                    f"草稿 v{tenant['draft_version_no']}"
                                    if tenant.get("draft_version_no")
                                    else "无草稿"
                                ),
                                className="tenant-meta",
                            ),
                            html.Span(f"{tenant['version_count']} 个版本", className="tenant-meta"),
                        ],
                        className="tenant-card-meta",
                    ),
                    html.Div(
                        [
                            html.A(
                                "查看 CDN 数据",
                                href=f"/tenants/{tenant_id}/overview",
                                className="config-button secondary",
                            ),
                            html.A(
                                "管理配置",
                                href=f"/config/tenants/{tenant_id}",
                                className="config-button primary",
                            ),
                        ],
                        className="tenant-card-actions",
                    ),
                ],
                className="tenant-card",
            )
        )

    source_options = [
        {"label": f"{item['display_name']} ({item['tenant_id']})", "value": item["tenant_id"]}
        for item in tenants
        if item.get("active_version_id")
    ]
    create_disabled = not source_options
    return html.Div(
        [
            html.Div(
                [
                    html.Div(
                        [
                            html.H2("Tenant 配置中心"),
                            html.P("租户是配置、任务、日志与前端查询的强制隔离边界。"),
                        ]
                    ),
                    html.Div(
                        [
                            html.Span(f"{len(tenants)} 个租户", className="tenant-count"),
                            html.Button(
                                [
                                    html.Span(
                                        "add",
                                        className="material-symbols-outlined",
                                        **{"aria-hidden": "true"},
                                    ),
                                    html.Span("创建租户"),
                                ],
                                id="tenant-create-open",
                                className="config-button primary large",
                                n_clicks=0,
                                disabled=create_disabled,
                                title=(
                                    "需要至少一个已发布租户作为配置模板"
                                    if create_disabled
                                    else None
                                ),
                            ),
                        ],
                        className="tenant-page-actions",
                    ),
                ],
                className="page-header",
            ),
            html.Section(
                cards
                or [
                    html.Div(
                        [
                            html.Span(
                                "domain_disabled",
                                className="material-symbols-outlined",
                                **{"aria-hidden": "true"},
                            ),
                            html.H3("暂无租户"),
                            html.P("请先通过迁移命令导入一个已发布配置。"),
                        ],
                        className="tenant-empty-state",
                    )
                ],
                className="tenant-grid",
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Div(
                                        [
                                            html.Span(
                                                "domain_add",
                                                className="material-symbols-outlined",
                                                **{"aria-hidden": "true"},
                                            ),
                                            html.Div(
                                                [
                                                    html.H3("创建租户", id="tenant-create-title"),
                                                    html.P("从已发布配置复制一份独立的租户配置。"),
                                                ]
                                            ),
                                        ],
                                        className="tenant-modal-heading",
                                    ),
                                    html.Button(
                                        html.Span(
                                            "close",
                                            className="material-symbols-outlined",
                                            **{"aria-hidden": "true"},
                                        ),
                                        id="tenant-create-close",
                                        className="tenant-modal-close",
                                        n_clicks=0,
                                        title="关闭",
                                        **{"aria-label": "关闭创建租户弹窗"},
                                    ),
                                ],
                                className="tenant-modal-header",
                            ),
                            html.Div(
                                [
                                    html.Label(
                                        [
                                            html.Span("租户 ID"),
                                            dcc.Input(
                                                id="tenant-create-id",
                                                placeholder="例如 acme-sg",
                                                className="config-input",
                                            ),
                                            html.Small(
                                                "创建后不可修改，建议使用小写字母和连字符。"
                                            ),
                                        ],
                                        className="tenant-modal-field",
                                    ),
                                    html.Label(
                                        [
                                            html.Span("显示名称"),
                                            dcc.Input(
                                                id="tenant-create-name",
                                                placeholder="例如 Acme 新加坡",
                                                className="config-input",
                                            ),
                                        ],
                                        className="tenant-modal-field",
                                    ),
                                    html.Label(
                                        [
                                            html.Span("配置模板"),
                                            dcc.Dropdown(
                                                id="tenant-create-source",
                                                options=source_options,
                                                value=(
                                                    source_options[0]["value"]
                                                    if source_options
                                                    else None
                                                ),
                                                clearable=False,
                                                placeholder="选择已发布配置",
                                                className="config-dropdown",
                                            ),
                                            html.Small(
                                                "仅复制配置，不会复制日志、任务或审计记录。"
                                            ),
                                        ],
                                        className="tenant-modal-field",
                                    ),
                                    html.Div(id="tenant-create-result", role="status"),
                                ],
                                className="tenant-modal-body",
                            ),
                            html.Div(
                                [
                                    html.Button(
                                        "取消",
                                        id="tenant-create-cancel",
                                        className="config-button secondary large",
                                        n_clicks=0,
                                    ),
                                    html.Button(
                                        "创建租户",
                                        id="tenant-create-submit",
                                        className="config-button primary large",
                                        n_clicks=0,
                                        disabled=create_disabled,
                                    ),
                                ],
                                className="tenant-modal-footer",
                            ),
                        ],
                        className="tenant-modal-panel",
                        role="dialog",
                        **{
                            "aria-modal": "true",
                            "aria-labelledby": "tenant-create-title",
                        },
                    ),
                ],
                id="tenant-create-modal",
                className="tenant-create-modal",
            ),
        ],
        className="tenant-admin-page",
    )


def create_version_panel(store: TenantConfigStore, tenant_id: str):
    versions = store.versions(tenant_id)
    options = [
        {
            "label": f"v{item['version_no']} · {item['status']} · {item['checksum'][:12]}",
            "value": item["id"],
        }
        for item in versions
    ]
    tenant = store.get_tenant(tenant_id)
    rollback_options = [
        item for item in options if item["value"] != tenant.get("active_version_id")
    ]
    return html.Section(
        [
            html.Div(
                [
                    html.Div(
                        [html.H3("版本历史"), html.P("回滚会复制目标版本并产生新的发布版本。")]
                    ),
                    html.A("查看审计", href=f"/config/tenants/{tenant_id}/audit"),
                ],
                className="tenant-version-heading",
            ),
            html.Div(
                [
                    dcc.Dropdown(
                        id="tenant-rollback-version",
                        options=rollback_options,
                        placeholder="选择要恢复的历史版本",
                        clearable=False,
                        className="config-dropdown",
                    ),
                    html.Button(
                        "回滚并发布",
                        id="tenant-rollback-submit",
                        className="config-button secondary",
                        n_clicks=0,
                        disabled=not rollback_options,
                    ),
                    html.A(
                        [
                            html.Span(
                                "play_circle",
                                className="material-symbols-outlined",
                                **{"aria-hidden": "true"},
                            ),
                            html.Span("创建任务"),
                        ],
                        href=f"/admin/jobs/{tenant_id}",
                        className="config-button primary",
                    ),
                ],
                className="tenant-version-actions",
            ),
            dcc.Store(id="tenant-version-id", data=tenant_id),
            dcc.Store(id="tenant-version-revision", data=str(tenant["revision"])),
            html.Div(id="tenant-version-result", role="status"),
        ],
        className="tenant-version-card",
    )


def register_tenant_callbacks(app: dash.Dash, store: TenantConfigStore) -> None:
    @app.callback(
        [
            Output("tenant-create-modal", "className"),
            Output("tenant-create-result", "children"),
        ],
        [
            Input("tenant-create-open", "n_clicks"),
            Input("tenant-create-close", "n_clicks"),
            Input("tenant-create-cancel", "n_clicks"),
            Input("tenant-create-submit", "n_clicks"),
        ],
        [
            State("tenant-create-id", "value"),
            State("tenant-create-name", "value"),
            State("tenant-create-source", "value"),
        ],
        prevent_initial_call=True,
    )
    def manage_create_tenant_modal(
        _open_clicks,
        _close_clicks,
        _cancel_clicks,
        _submit_clicks,
        tenant_id,
        display_name,
        source_tenant,
    ):
        trigger = dash.ctx.triggered_id
        if trigger == "tenant-create-open":
            return "tenant-create-modal is-open", None
        if trigger in {"tenant-create-close", "tenant-create-cancel"}:
            return "tenant-create-modal", None
        if trigger != "tenant-create-submit":
            raise PreventUpdate

        if not is_admin_session():
            return (
                "tenant-create-modal is-open",
                html.Div(
                    "仅管理员可以创建租户。",
                    className="config-inline-validation danger",
                ),
            )
        if not tenant_id or not source_tenant:
            return (
                "tenant-create-modal is-open",
                html.Div(
                    "请填写租户 ID 并选择配置模板。",
                    className="config-inline-validation danger",
                ),
            )
        try:
            source = store.resolve_active(source_tenant)
            created = store.create_tenant(
                tenant_id,
                display_name or tenant_id,
                deepcopy(source["config"]),
                actor=session.get("username", "dashboard"),
            )
            return (
                "tenant-create-modal is-open",
                html.Div(
                    [
                        html.Strong(f"租户 {created['tenant_id']} 已创建。"),
                        html.A("进入配置", href=f"/config/tenants/{created['tenant_id']}"),
                    ],
                    className="config-inline-validation success",
                ),
            )
        except (ConfigManagerError, TypeError, ValueError) as exc:
            return (
                "tenant-create-modal is-open",
                html.Div(str(exc), className="config-inline-validation danger"),
            )

    @app.callback(
        [
            Output("tenant-version-result", "children"),
            Output("tenant-version-revision", "data"),
        ],
        Input("tenant-rollback-submit", "n_clicks"),
        [
            State("tenant-rollback-version", "value"),
            State("tenant-version-id", "data"),
            State("tenant-version-revision", "data"),
        ],
        prevent_initial_call=True,
    )
    def rollback_version(_clicks, version_id, tenant_id, revision):
        if not is_admin_session():
            return (
                html.Div("仅管理员可以回滚配置。", className="config-inline-validation danger"),
                dash.no_update,
            )
        if not version_id:
            raise PreventUpdate
        try:
            result = store.rollback(
                tenant_id,
                int(version_id),
                expected_revision=revision,
                actor=session.get("username", "dashboard"),
            )
            return (
                html.Div(
                    f"已回滚并发布为 v{result['version_no']}，刷新页面可查看新版本。",
                    className="config-inline-validation success",
                ),
                result["revision"],
            )
        except (ConfigManagerError, TypeError, ValueError) as exc:
            return html.Div(str(exc), className="config-inline-validation danger"), dash.no_update
