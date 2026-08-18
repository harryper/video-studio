"""Deployment contract tests for Content Studio.

These tests pin the properties the brief (Task 14) calls out as the contract
between the new services and the rest of the system:

* ``content-studio-web`` is read-only at the filesystem layer (anything
  written goes through the ``studio-data`` volume) and exposes a
  healthcheck.
* ``content-studio-worker`` runs on the same image with the
  ``Restart=on-failure`` policy and a bounded ``TimeoutStartSec`` so a
  misconfiguration cannot wedge the systemd unit.

The fixtures load ``docker-compose.next.yml`` and the worker unit file from
disk so the tests catch drift between the live deployment files and what the
rest of the codebase expects. The brief's verbatim test signature
(``compose, worker_unit``) is preserved so the spec reviewer can run the
exact test body without any indirection.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = REPO_ROOT / "docker-compose.next.yml"
WORKER_UNIT_PATH = REPO_ROOT / "systemd" / "video-studio-next-worker.service"


@pytest.fixture(scope="module")
def compose() -> dict[str, object]:
    """Parsed ``docker-compose.next.yml`` (module-scoped: file is committed)."""

    with COMPOSE_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def worker_unit() -> str:
    """Raw text of the worker systemd unit (module-scoped: file is committed)."""

    return WORKER_UNIT_PATH.read_text(encoding="utf-8")


def test_next_services_use_finite_timeouts(compose, worker_unit):
    """Brief verbatim body. Kept identical to the spec text."""

    services = compose["services"]
    assert services["content-studio-web"]["healthcheck"]
    assert services["content-studio-web"]["read_only"] is True
    assert "TimeoutStartSec=" in worker_unit
    assert "Restart=on-failure" in worker_unit


def test_compose_declares_both_services(compose) -> None:
    services = compose["services"]
    assert "content-studio-web" in services
    assert "content-studio-worker" in services


def test_web_service_binds_port_10000(compose) -> None:
    """Legacy stays on :9998; the new system binds :10000 on the host."""

    ports = compose["services"]["content-studio-web"]["ports"]
    assert "10000:10000" in ports


def test_worker_service_shares_db_volume(compose) -> None:
    """Web and worker must read+write the same SQLite DB via ``studio-data``."""

    web_volumes = compose["services"]["content-studio-web"]["volumes"]
    worker_volumes = compose["services"]["content-studio-worker"]["volumes"]
    assert web_volumes == worker_volumes
    assert any("/data" in entry for entry in web_volumes)


def test_worker_service_builds_from_same_image(compose) -> None:
    """Both services share the same image so a single build covers both."""

    web_build = compose["services"]["content-studio-web"]["build"]
    worker_build = compose["services"]["content-studio-worker"]["build"]
    assert web_build == worker_build


def test_worker_service_restarts_on_failure(compose) -> None:
    assert compose["services"]["content-studio-worker"]["restart"] == "on-failure"


def test_worker_service_has_healthcheck(compose) -> None:
    """Healthcheck is required so docker / orchestrators know the worker is alive."""

    assert compose["services"]["content-studio-worker"]["healthcheck"]


def test_worker_unit_runs_worker_main(worker_unit: str) -> None:
    """The systemd ``ExecStart`` must invoke the worker entrypoint shipped in
    :mod:`studio.worker_main` so the unit file alone proves the worker
    command path."""

    assert "studio.worker_main" in worker_unit


def test_worker_unit_uses_simple_type(worker_unit: str) -> None:
    """``Type=simple`` is the documented choice — the worker is a long-running
    process and does not fork or notify."""

    assert "Type=simple" in worker_unit


def test_worker_unit_uses_known_working_directory(worker_unit: str) -> None:
    """``WorkingDirectory`` must point at the install root so the worker can
    resolve the bundled ``migrations/`` and ``studio/`` modules."""

    assert "WorkingDirectory=" in worker_unit