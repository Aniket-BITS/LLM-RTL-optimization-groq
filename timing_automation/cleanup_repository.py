#!/usr/bin/env python3

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent


JUNK_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


GENERATED_FILES = {
    "timing_summary.json",
    "path_analysis.json",
}


GENERATED_DIRS = {
    "logs",
}


BACKUP_SUFFIXES = (
    ".bak",
    ".backup",
    ".orig",
    ".rej",
    ".swp",
    ".swo",
    ".tmp",
)


def is_zone_identifier(path: Path) -> bool:
    return "Zone.Identifier" in path.name


def is_backup(path: Path) -> bool:
    if path.name.endswith("~"):
        return True

    return any(
        path.name.endswith(suffix)
        for suffix in BACKUP_SUFFIXES
    )


def is_python_cache(path: Path) -> bool:
    return path.suffix in {".pyc", ".pyo"}


def discover_cleanup_targets() -> list[tuple[Path, str]]:
    targets: list[tuple[Path, str]] = []

    # ------------------------------------------------------
    # 1. Nested duplicate tree
    # ------------------------------------------------------

    nested_tree = SCRIPT_DIR / "timing_automation"

    if nested_tree.is_dir():
        targets.append(
            (
                nested_tree,
                "duplicated nested timing_automation tree",
            )
        )

    # ------------------------------------------------------
    # 2. Generated directories
    # ------------------------------------------------------

    for name in GENERATED_DIRS:
        path = SCRIPT_DIR / name

        if path.exists():
            targets.append(
                (
                    path,
                    "generated runtime directory",
                )
            )

    # ------------------------------------------------------
    # 3. Generated root JSON files
    # ------------------------------------------------------

    for name in GENERATED_FILES:
        path = SCRIPT_DIR / name

        if path.exists():
            targets.append(
                (
                    path,
                    "generated analysis output",
                )
            )

    # ------------------------------------------------------
    # 4. Recursive junk scan
    # ------------------------------------------------------

    for path in SCRIPT_DIR.rglob("*"):

        # Nested tree is already removed as one object.
        if nested_tree in path.parents:
            continue

        if path.is_dir():

            if path.name in JUNK_DIRS:
                targets.append(
                    (
                        path,
                        "Python/tool cache directory",
                    )
                )

            continue

        if is_zone_identifier(path):

            targets.append(
                (
                    path,
                    "Zone.Identifier metadata",
                )
            )

        elif is_python_cache(path):

            targets.append(
                (
                    path,
                    "Python bytecode",
                )
            )

        elif is_backup(path):

            targets.append(
                (
                    path,
                    "backup/temp file",
                )
            )

    # Remove duplicate entries.
    unique: dict[Path, str] = {}

    for path, reason in targets:
        unique[path.resolve()] = reason

    return sorted(
        unique.items(),
        key=lambda item: str(item[0]),
    )


def remove_target(path: Path) -> None:

    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def main() -> int:

    parser = argparse.ArgumentParser(
        description="Clean the nEBULA timing_automation source tree."
    )

    mode = parser.add_mutually_exclusive_group()

    mode.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform deletion. Default is dry-run.",
    )

    mode.add_argument(
        "--check",
        action="store_true",
        help="CI mode: delete nothing and exit non-zero if cleanup targets exist.",
    )

    args = parser.parse_args()

    # ------------------------------------------------------
    # Basic safety check
    # ------------------------------------------------------

    required_files = {
        "run_flow.py",
        "run_analysis.py",
        "collect_metrics.py",
        "map_to_rtl.py",
    }

    missing = [
        name
        for name in required_files
        if not (SCRIPT_DIR / name).exists()
    ]

    if missing:

        print("ERROR: this does not look like timing_automation.")
        print("Missing:", ", ".join(missing))

        return 1

    targets = discover_cleanup_targets()

    print("=" * 70)
    print("nEBULA timing_automation cleanup")
    print("=" * 70)

    print(f"Directory : {SCRIPT_DIR}")

    if args.apply:
        print("Mode      : APPLY")
    elif args.check:
        print("Mode      : CHECK")
    else:
        print("Mode      : DRY RUN")

    print()

    if not targets:

        print("Repository source tree is already clean.")
        return 0

    print(f"Found {len(targets)} cleanup target(s):")
    print()

    for path, reason in targets:

        try:
            rel = path.relative_to(SCRIPT_DIR)
        except ValueError:
            rel = path

        print(f"[REMOVE] {rel}")
        print(f"         {reason}")

    if args.check:

        print()
        print("=" * 70)
        print("CHECK FAILED: cleanup targets exist; nothing was deleted.")
        print("=" * 70)

        return 1

    if not args.apply:

        print()
        print("=" * 70)
        print("DRY RUN: nothing was deleted.")
        print("=" * 70)

        print()
        print("If this list looks correct, run:")
        print()
        print("python3 cleanup_repository.py --apply")

        return 0

    print()
    print("=" * 70)
    print("Applying cleanup")
    print("=" * 70)

    for path, _reason in targets:

        try:

            rel = path.relative_to(SCRIPT_DIR)

            remove_target(path)

            print(f"removed: {rel}")

        except Exception as exc:

            print(f"ERROR: {path}: {exc}")

            return 1

    print()
    print("Cleanup complete.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
