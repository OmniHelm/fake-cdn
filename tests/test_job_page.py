from __future__ import annotations

import json
from pathlib import Path

from fake_cdn.core.tenant_config import TenantConfigStore
from fake_cdn.dashboard.app import create_sidebar
from fake_cdn.dashboard.job_page import create_job_page


def _walk(component):
    yield component
    children = getattr(component, "children", None)
    if children is None:
        return
    if not isinstance(children, (list, tuple)):
        children = [children]
    for child in children:
        if hasattr(child, "to_plotly_json"):
            yield from _walk(child)


def test_admin_job_page_contains_filters_actions_and_confirmations(tmp_path: Path):
    source = Path(__file__).resolve().parent.parent / "config.json"
    config = json.loads(source.read_text(encoding="utf-8"))
    store = TenantConfigStore(tmp_path / "config.db")
    store.create_tenant("tenant-a", "Tenant A", config)

    page = create_job_page(store, "tenant-a")
    components = list(_walk(page))
    by_id = {component.id: component for component in components if getattr(component, "id", None)}

    expected_ids = {
        "jobs-table",
        "jobs-tenant-filter",
        "jobs-mode-filter",
        "jobs-status-filter",
        "jobs-create-open",
        "jobs-create-modal",
        "jobs-create-tenant",
        "jobs-create-mode",
        "jobs-push-confirmation",
        "jobs-cancel",
        "jobs-retry",
        "jobs-retry-confirmation",
        "jobs-detail",
    }
    assert expected_ids <= set(by_id)
    assert by_id["jobs-create-tenant"].value == "tenant-a"
    assert by_id["jobs-tenant-filter"].value == "tenant-a"
    assert by_id["jobs-create-modal"].className == "jobs-modal-backdrop hidden"


def test_task_center_navigation_is_admin_only():
    admin_sidebar = repr(create_sidebar("/admin/jobs", "tenant-a", is_admin=True))
    tenant_sidebar = repr(create_sidebar("/tenants/tenant-a/overview", "tenant-a", is_admin=False))

    assert "任务中心" in admin_sidebar
    assert "/admin/jobs" in admin_sidebar
    assert "任务中心" not in tenant_sidebar
    assert "/admin/jobs" not in tenant_sidebar
