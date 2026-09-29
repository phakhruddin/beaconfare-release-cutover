"""Verifier configuration, read fresh from the environment for every run."""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import HarnessError


@dataclass(frozen=True)
class Config:
    submission_dir: Path
    logs_dir: Path
    evidence_dir: Path
    config_path: Path
    source_submission: Path
    region: str
    endpoint_url: str
    values: dict[str, Any]
    truth: dict[str, Any]

    @property
    def manifest_path(self) -> Path:
        return self.submission_dir / "manifest.json"

    @property
    def infra_dir(self) -> Path:
        return self.submission_dir / "infra"

    @property
    def prefix(self) -> str:
        return str(self.values["resource_prefix"])

    @property
    def desired(self) -> int:
        return int(self.values["api_desired_count"])

    @property
    def initial_release(self) -> str:
        return str(self.values["initial_release"])

    @property
    def versions(self) -> list[str]:
        return [str(r["version"]) for r in self.values["releases"]]

    @property
    def images(self) -> dict[str, str]:
        return {str(r["version"]): str(r["image"]) for r in self.values["releases"]}

    @property
    def production_port(self) -> int:
        return int(self.values["production_listener_port"])

    @property
    def preview_port(self) -> int:
        return int(self.values["preview_listener_port"])

    @property
    def log_retention_days(self) -> int:
        return int(self.values["log_retention_days"])

    @property
    def defective_release(self) -> str:
        return str(self.truth["defective_release"])

    @property
    def good_release(self) -> str:
        return str(self.truth["good_release"])

    @property
    def lock_table(self) -> str:
        return f"{self.prefix}-release-lock"

    @property
    def lock_lease_seconds(self) -> int:
        return int(self.values["lock_lease_seconds"])

    @property
    def legacy_table(self) -> str:
        return f"{self.prefix}-legacy-quotes"

    @property
    def legacy_cluster(self) -> str:
        return f"{self.prefix}-legacy-cluster"

    @property
    def legacy_log_group(self) -> str:
        return f"/beaconfare/{self.prefix}-legacy/api"


def _writable_copy(source: Path, work: Path) -> Path:
    """Harbor injects the submission read-only.

    Terraform must write var files, .terraform/ and state beside the submitted
    configuration, so the verifier exercises an exact copy it owns. Copying
    the whole tree preserves every relative path inside the submission.
    """
    if not source.is_dir():
        return source
    shutil.copytree(source, work, dirs_exist_ok=True)
    work.chmod(work.stat().st_mode | 0o700)
    for path in work.rglob("*"):
        try:
            path.chmod(path.stat().st_mode | (0o700 if path.is_dir() else 0o600))
        except OSError:
            pass
    return work


def from_environment() -> Config:
    source_submission = Path(os.getenv("BEACONFARE_SUBMISSION_DIR", "/workspace/submission"))
    submission = _writable_copy(
        source_submission,
        Path(os.getenv("BEACONFARE_WORK_DIR", "/tmp/beaconfare-submission")),
    )
    logs = Path(os.getenv("BEACONFARE_LOGS_DIR", "/logs/verifier"))
    evidence = Path(os.getenv("BEACONFARE_EVIDENCE_DIR", "/workspace/evidence"))
    config_path = Path(os.getenv("BEACONFARE_CONFIG", "/workspace/config/config.json"))
    backup = Path(os.getenv("BEACONFARE_BACKUP_CONFIG", "/workspace/runtime-config/config.json"))
    truth_path = Path(os.getenv("BEACONFARE_TRUTH", "/opt/beaconfare-private/release-truth.json"))

    if not config_path.is_file() and backup.is_file():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(backup, config_path)
    if not config_path.is_file():
        raise HarnessError(f"runtime configuration is missing at {config_path}")
    if not truth_path.is_file():
        raise HarnessError(f"release truth is missing at {truth_path}")

    values = json.loads(config_path.read_text(encoding="utf-8"))
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    logs.mkdir(parents=True, exist_ok=True)
    return Config(
        submission_dir=submission,
        source_submission=source_submission,
        logs_dir=logs,
        evidence_dir=evidence,
        config_path=config_path,
        region=str(values["region"]),
        endpoint_url=str(values["aws_endpoint_url"]),
        values=values,
        truth=truth,
    )
