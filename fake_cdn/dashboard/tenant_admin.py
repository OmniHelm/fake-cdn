"""租户列表、创建和配置版本操作页面。"""

from __future__ import annotations

from copy import deepcopy

import dash
from dash import Input, Output, State, dcc, html
from dash.exceptions import PreventUpdate
from flask import session

from fake_cdn.core.config_manager import ConfigManagerError
from fake_cdn.core.tenant_config import TenantConfigStore


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
                                f"草稿 v{tenant['draft_version_no']}"
                                if tenant.get("draft_version_no") else "无草稿",
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
        for item in tenants if item.get("active_version_id")
    ]
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
                    html.Span(f"{len(tenants)} 个租户", className="tenant-count"),
                ],
                className="page-header",
            ),
            html.Section(cards or [html.P("暂无租户，请先创建。")], className="tenant-grid"),
            html.Section(
                [
                    html.Div(
                        [html.H3("创建租户"), html.P("从已发布配置复制模板，tenant_id 会被强制替换。")],
                        className="tenant-create-heading",
                    ),
                    html.Div(
                        [
                            dcc.Input(
                                id="tenant-create-id",
                                placeholder="tenant_id，例如 acme-sg",
                                className="config-input",
                            ),
                            dcc.Input(
                                id="tenant-create-name",
                                placeholder="显示名称",
                                className="config-input",
                            ),
                            dcc.Dropdown(
                                id="tenant-create-source",
                                options=source_options,
                                value=source_options[0]["value"] if source_options else None,
                                clearable=False,
                                placeholder="选择配置模板",
                                className="config-dropdown",
                            ),
                            html.Button(
                                "创建并进入配置",
                                id="tenant-create-submit",
                                className="config-button primary large",
                                n_clicks=0,
                                disabled=not source_options,
                            ),
                        ],
                        className="tenant-create-form",
                    ),
                    html.Div(id="tenant-create-result", role="status"),
                ],
                className="tenant-create-card",
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
                    html.Div([html.H3("版本历史"), html.P("回滚会复制目标版本并产生新的发布版本。")]),
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
        Output("tenant-create-result", "children"),
        Input("tenant-create-submit", "n_clicks"),
        [
            State("tenant-create-id", "value"),
            State("tenant-create-name", "value"),
            State("tenant-create-source", "value"),
        ],
        prevent_initial_call=True,
    )
    def create_tenant(_clicks, tenant_id, display_name, source_tenant):
        if not tenant_id or not source_tenant:
            return html.Div("请填写 tenant_id 并选择模板。", className="config-inline-validation danger")
        try:
            source = store.resolve_active(source_tenant)
            created = store.create_tenant(
                tenant_id,
                display_name or tenant_id,
                deepcopy(source["config"]),
                actor=session.get("username", "dashboard"),
            )
            return html.Div(
                [
                    html.Strong(f"租户 {created['tenant_id']} 已创建。"),
                    html.A("进入配置", href=f"/config/tenants/{created['tenant_id']}")
                ],
                className="config-inline-validation success",
            )
        except (ConfigManagerError, TypeError, ValueError) as exc:
            return html.Div(str(exc), className="config-inline-validation danger")

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
