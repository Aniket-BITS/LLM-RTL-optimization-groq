from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest


TEST_DIR = Path(__file__).resolve().parent
TIMING_AUTOMATION_DIR = TEST_DIR.parent
FLOW_DIR = TIMING_AUTOMATION_DIR.parent
ORFS_ROOT = FLOW_DIR.parent


def _executable_from_env_or_path(
    env_names: tuple[str, ...],
    name: str,
    orfs_relative: Path | None = None,
) -> str | None:
    candidates: list[str] = []
    for env_name in env_names:
        value = os.environ.get(env_name)
        if value:
            candidates.append(value)
    if orfs_relative is not None:
        candidates.append(str(orfs_relative))
    found_on_path = shutil.which(name)
    if found_on_path:
        candidates.append(found_on_path)

    for candidate in candidates:
        p = Path(candidate).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p.resolve())
        found = shutil.which(candidate)
        if found:
            return found
    return None


@pytest.fixture(scope="session")
def orfs_tree() -> Path:
    required = [FLOW_DIR / "designs", FLOW_DIR / "platforms", FLOW_DIR / "Makefile"]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        pytest.skip("ORFS tree unavailable; missing: " + ", ".join(missing))
    return FLOW_DIR


@pytest.fixture(scope="session")
def yosys() -> str:
    exe = _executable_from_env_or_path(
        ("NEBULA_YOSYS", "YOSYS_EXE"),
        "yosys",
        ORFS_ROOT / "tools" / "install" / "yosys" / "bin" / "yosys",
    )
    if not exe:
        pytest.skip("Yosys not available in PATH/NEBULA_YOSYS/YOSYS_EXE")
    return exe


@pytest.fixture(scope="session")
def openroad() -> str:
    exe = _executable_from_env_or_path(
        ("NEBULA_OPENROAD", "OPENROAD_EXE"),
        "openroad",
        ORFS_ROOT / "tools" / "install" / "OpenROAD" / "bin" / "openroad",
    )
    if not exe:
        pytest.skip("OpenROAD not available in PATH/NEBULA_OPENROAD/OPENROAD_EXE")
    return exe
