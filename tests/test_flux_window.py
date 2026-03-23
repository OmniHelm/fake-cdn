from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

from fake_cdn.core.generator import CDNLogGenerator, TrafficTargetParser, normalize_config
from fake_cdn.core.validator import FluxWindowValidator


def build_test_config(tmp_path: Path, start: str, end: str, total_flux_pb: float = 0.002, domain_count: int = 4):
    with open(Path(__file__).resolve().parent.parent / "config.json", "r", encoding="utf-8") as file:
        config = json.load(file)

    config = deepcopy(config)
    config["target"]["total_flux"]["value"] = total_flux_pb
    config["time"]["start_datetime"] = start
    config["time"]["end_datetime"] = end
    config["dimensions"]["domains"] = config["dimensions"]["domains"][:domain_count]
    config["mode"]["output_dir"] = str(tmp_path)
    config["mode"]["dry_run"] = True
    return normalize_config(config)


def test_flux_window_plan_matches_target_total(tmp_path: Path):
    config = build_test_config(
        tmp_path,
        start="2026-03-11 00:00:00",
        end="2026-03-12 23:55:00",
        total_flux_pb=0.001,
        domain_count=3,
    )

    generator = CDNLogGenerator(config)
    plan = generator.generate_window_plan()
    target_total_flux = TrafficTargetParser.parse_total_flux_bytes(config)

    assert len(plan) == 576
    assert sum(point.flux_bytes for point in plan) == target_total_flux
    assert plan[0].timestamp_ms < plan[-1].timestamp_ms


def test_generated_logs_pass_validator_and_weekday_higher_than_weekend(tmp_path: Path):
    config = build_test_config(
        tmp_path,
        start="2026-03-09 00:00:00",
        end="2026-03-15 23:55:00",
        total_flux_pb=0.003,
        domain_count=4,
    )

    generator = CDNLogGenerator(config)
    logs, stats = generator.generate_window_logs()
    result = FluxWindowValidator.validate_logs(logs, config)

    assert stats["actual_total_flux_bytes"] == TrafficTargetParser.parse_total_flux_bytes(config)
    assert result["validation"]["passed"] is True
    assert result["overall"]["weekday_gt_weekend"] is True
    assert result["integrity"] == {}


def test_cli_simulation_creates_expected_output_files(tmp_path: Path):
    config = build_test_config(
        tmp_path,
        start="2026-03-11 00:00:00",
        end="2026-03-11 23:55:00",
        total_flux_pb=0.0005,
        domain_count=2,
    )

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-m", "fake_cdn", "simulation", "--config", str(config_path), "--dry-run"],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert (tmp_path / "stats.json").exists()
    assert (tmp_path / "flux_curve.csv").exists()
    assert (tmp_path / "cdn_logs.db").exists()

    with sqlite3.connect(tmp_path / "cdn_logs.db") as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM cdn_logs")
        assert cursor.fetchone()[0] > 0
