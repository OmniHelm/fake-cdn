from __future__ import annotations

import json
from pathlib import Path

from fake_cdn.core.tenant_config import TenantConfigStore
from fake_cdn.dashboard.tenant_admin import create_tenant_list_page


def _walk(component):
    yield component
    children = getattr(component, "children", None)
    if children is None:
        return
    if not isinstance(children, (list, tuple)):
        children = [children]
    for child in children:
        if hasattr(child, "children"):
            yield from _walk(child)


def test_tenant_list_keeps_creation_form_inside_modal(tmp_path: Path):
    store = TenantConfigStore(tmp_path / "config.db")
    config_path = Path(__file__).resolve().parent.parent / "config.json"
    store.create_tenant(
        "tenant-a",
        "Tenant A",
        json.loads(config_path.read_text(encoding="utf-8")),
    )

    page = create_tenant_list_page(store)
    components = list(_walk(page))
    components_by_id = {
        component.id: component for component in components if getattr(component, "id", None)
    }

    grid = next(
        component
        for component in components
        if getattr(component, "className", None) == "tenant-grid"
    )
    assert all(card.className == "tenant-card" for card in grid.children)
    assert not any(
        getattr(component, "className", None) == "tenant-create-card" for component in components
    )

    assert components_by_id["tenant-create-open"].children[-1].children == "创建租户"
    assert components_by_id["tenant-create-modal"].className == "tenant-create-modal"
    assert components_by_id["tenant-create-submit"].children == "创建租户"
    assert components_by_id["tenant-create-result"].role == "status"
