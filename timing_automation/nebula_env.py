#!/usr/bin/env python3
"""Central path/tool resolution for nEBULA.

Executable precedence is deliberately deterministic:
1. explicit CLI value
2. NEBULA_* environment variable (legacy *_EXE is accepted after it)
3. ORFS-relative install path
4. PATH
"""
from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterable


class NebulaEnvError(RuntimeError):
    pass


PACKAGE_DIR = Path(__file__).resolve().parent


def _usable_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _dedupe(paths: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            resolved = path.expanduser().absolute()
        if resolved not in seen:
            seen.add(resolved)
            out.append(resolved)
    return out


def orfs_root(explicit: str | Path | None = None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("NEBULA_ORFS_ROOT")
    if env:
        candidates.append(Path(env))

    # Installed-in-flow layout, but only accept it when the surrounding
    # structure actually looks like ORFS.
    for ancestor in [PACKAGE_DIR, *PACKAGE_DIR.parents]:
        if ancestor.name == "flow":
            candidates.append(ancestor.parent)
        if (ancestor / "flow").is_dir():
            candidates.append(ancestor)

    for candidate in _dedupe(candidates):
        if (candidate / "flow").is_dir():
            return candidate

    rendered = "\n".join(f"  - {p}" for p in _dedupe(candidates)) or "  <none>"
    raise NebulaEnvError(
        "Could not resolve ORFS root (directory containing flow/). Checked:\n"
        + rendered
        + "\nSet NEBULA_ORFS_ROOT or pass an explicit root."
    )


def flow_dir(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("NEBULA_FLOW_DIR")
    if env:
        return Path(env).expanduser().resolve()

    package_flow = PACKAGE_DIR.parent
    # The package may be checked out directly for tests, or installed under
    # <ORFS>/flow. In either case its parent is the natural project/flow root.
    if package_flow.is_dir():
        return package_flow.resolve()

    return (orfs_root() / "flow").resolve()


def tools_root(orfs: str | Path | None = None) -> Path:
    return (orfs_root(orfs) / "tools" / "install").resolve()


def _resolve_executable(
    *,
    tool_name: str,
    explicit: str | Path | None,
    env_var: str,
    legacy_env_var: str | None,
    orfs_relative: str,
    orfs: str | Path | None = None,
    flow: str | Path | None = None,
    required: bool = True,
) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get(env_var):
        candidates.append(Path(os.environ[env_var]))
    if legacy_env_var and os.environ.get(legacy_env_var):
        candidates.append(Path(os.environ[legacy_env_var]))

    inferred_orfs: Path | None = None
    if orfs:
        inferred_orfs = Path(orfs).expanduser().resolve()
    elif flow:
        flow_path = Path(flow).expanduser().resolve()
        inferred_orfs = flow_path.parent if flow_path.name == "flow" else None
    else:
        try:
            inferred_orfs = orfs_root()
        except NebulaEnvError:
            inferred_orfs = None

    if inferred_orfs is not None:
        candidates.append(inferred_orfs / orfs_relative)

    on_path = shutil.which(tool_name)
    if on_path:
        candidates.append(Path(on_path))

    resolved = _dedupe(candidates)
    for candidate in resolved:
        if _usable_executable(candidate):
            return candidate

    if not required:
        if resolved:
            return resolved[0]
        # Preserve a lazy command-name fallback for modules that only need to
        # construct a command now and will validate the tool when they execute.
        return Path(tool_name)

    rendered = "\n".join(f"  - {p}" for p in resolved) or "  <none>"
    raise NebulaEnvError(
        f"Could not find executable {tool_name!r}. Checked:\n{rendered}\n"
        f"Use an explicit flag or set {env_var}."
    )


def yosys(
    explicit: str | Path | None = None,
    *,
    orfs: str | Path | None = None,
    flow: str | Path | None = None,
    required: bool = True,
) -> Path:
    return _resolve_executable(
        tool_name="yosys",
        explicit=explicit,
        env_var="NEBULA_YOSYS",
        legacy_env_var="YOSYS_EXE",
        orfs_relative="tools/install/yosys/bin/yosys",
        orfs=orfs,
        flow=flow,
        required=required,
    )


def openroad(
    explicit: str | Path | None = None,
    *,
    orfs: str | Path | None = None,
    flow: str | Path | None = None,
    required: bool = True,
) -> Path:
    return _resolve_executable(
        tool_name="openroad",
        explicit=explicit,
        env_var="NEBULA_OPENROAD",
        legacy_env_var="OPENROAD_EXE",
        orfs_relative="tools/install/OpenROAD/bin/openroad",
        orfs=orfs,
        flow=flow,
        required=required,
    )


def run_self_test() -> None:
    old = {k: os.environ.get(k) for k in ("NEBULA_ORFS_ROOT", "NEBULA_YOSYS")}
    try:
        with tempfile.TemporaryDirectory(prefix="nebula_env_") as tmp:
            root = Path(tmp)
            (root / "flow").mkdir()
            y = root / "tools/install/yosys/bin/yosys"
            y.parent.mkdir(parents=True)
            y.write_text("#!/bin/sh\nexit 0\n")
            y.chmod(0o755)

            os.environ["NEBULA_ORFS_ROOT"] = str(root)
            assert orfs_root() == root.resolve()
            assert tools_root() == (root / "tools/install").resolve()
            assert yosys() == y.resolve()

            explicit = root / "explicit-yosys"
            explicit.write_text("#!/bin/sh\nexit 0\n")
            explicit.chmod(0o755)
            assert yosys(explicit) == explicit.resolve()

            env_y = root / "env-yosys"
            env_y.write_text("#!/bin/sh\nexit 0\n")
            env_y.chmod(0o755)
            os.environ["NEBULA_YOSYS"] = str(env_y)
            assert yosys() == env_y.resolve()
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("nebula_env.py self-test: PASS")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--show", action="store_true", help="Print resolved paths.")
    args = ap.parse_args()
    if args.self_test:
        run_self_test()
        return 0
    if args.show:
        values = {
            "orfs_root": str(orfs_root()),
            "flow_dir": str(flow_dir()),
            "tools_root": str(tools_root()),
            "yosys": str(yosys()),
            "openroad": str(openroad()),
        }
        for key, value in values.items():
            print(f"{key}: {value}")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
