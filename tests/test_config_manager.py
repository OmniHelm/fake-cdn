from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from fake_cdn.core.config_manager import (
    ConfigConflictError,
    ConfigManager,
    ConfigValidationError,
)


def copy_config(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parent.parent / "config.json"
    target = tmp_path / "config.json"
    target.write_bytes(source.read_bytes())
    return target


def test_load_normalizes_legacy_domains_without_rewriting_source(tmp_path: Path):
    config_path = copy_config(tmp_path)
    original = config_path.read_bytes()
    manager = ConfigManager(config_path, audit_dir=tmp_path / "audit")

    snapshot = manager.load()

    assert snapshot["revision"]
    assert snapshot["config"]["dimensions"]["domains"][0] == {
        "name": "appcircle.io",
        "weight": 1.0,
        "profile": "enterprise_b2b",
    }
    assert snapshot["summary"]["domain_count"] == 34
    assert snapshot["summary"]["estimated_record_count"] == 303_552
    assert config_path.read_bytes() == original


def test_save_is_atomic_and_writes_backup_and_audit(tmp_path: Path):
    config_path = copy_config(tmp_path)
    audit_dir = tmp_path / "audit"
    manager = ConfigManager(config_path, audit_dir=audit_dir)
    snapshot = manager.load()
    candidate = deepcopy(snapshot["config"])
    candidate["target"]["total_flux"]["value"] = 1.25

    saved = manager.save(
        candidate,
        expected_revision=snapshot["revision"],
        actor="tester",
        action="save_draft",
    )

    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["target"]["total_flux"]["value"] == 1.25
    assert Path(saved["backup"]).exists()
    assert saved["audit_error"] is None
    audit = manager.read_audit()
    assert audit[0]["actor"] == "tester"
    assert audit[0]["action"] == "save_draft"
    assert not list(tmp_path.glob(".config.json.*.tmp"))


def test_save_rejects_conflicting_revision(tmp_path: Path):
    config_path = copy_config(tmp_path)
    manager = ConfigManager(config_path, audit_dir=tmp_path / "audit")
    snapshot = manager.load()
    candidate = deepcopy(snapshot["config"])

    config_path.write_text(config_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ConfigConflictError):
        manager.save(candidate, expected_revision=snapshot["revision"])


def test_validation_rejects_duplicate_or_invalid_domains(tmp_path: Path):
    config_path = copy_config(tmp_path)
    manager = ConfigManager(config_path, audit_dir=tmp_path / "audit")
    snapshot = manager.load()

    duplicate = deepcopy(snapshot["config"])
    duplicate["dimensions"]["domains"][1]["name"] = duplicate["dimensions"]["domains"][0]["name"]
    with pytest.raises(ConfigValidationError, match="不能重复"):
        manager.save(duplicate, expected_revision=snapshot["revision"])

    invalid = deepcopy(snapshot["config"])
    invalid["dimensions"]["domains"][0]["name"] = "not a domain"
    with pytest.raises(ConfigValidationError, match="格式不正确"):
        manager.save(invalid, expected_revision=snapshot["revision"])


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda config: config["dimensions"]["domains"][0].update(weight=-1),
            "域名权重不能为负数",
        ),
        (
            lambda config: config["realism"].update(day_noise_ratio=1.1),
            "必须在 0 到 1 之间",
        ),
        (
            lambda config: config["realism"]["cache_hit_rate"].update(min=0.95, max=0.8),
            "max 必须大于等于 min",
        ),
        (
            lambda config: config["realism"]["avg_object_size_kb"].update(min=0),
            "min 必须大于 0",
        ),
    ],
)
def test_validation_rejects_invalid_numeric_boundaries(tmp_path: Path, mutate, message: str):
    config_path = copy_config(tmp_path)
    manager = ConfigManager(config_path, audit_dir=tmp_path / "audit")
    snapshot = manager.load()
    invalid = deepcopy(snapshot["config"])
    mutate(invalid)

    with pytest.raises(ConfigValidationError, match=message):
        manager.save(invalid, expected_revision=snapshot["revision"])
