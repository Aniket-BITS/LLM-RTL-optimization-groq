#!/usr/bin/env python3
"""Restore relative links after extraction under an existing ORFS flow/versions."""
import json
import os
from pathlib import Path

root = Path(__file__).resolve().parent
flow = root.parent.parent
if root.parent.name != 'versions' or not (flow / 'Makefile').is_file():
    raise SystemExit('Extract this folder into an existing OpenROAD-flow-scripts/flow/versions directory first.')
links = json.loads((root / 'SHARED_DEPENDENCIES.json').read_text())
missing = []
for relative, dependency in links.items():
    link = root / relative
    target = flow / dependency
    if not target.exists():
        missing.append(dependency)
    if link.is_symlink():
        if os.path.abspath(link.parent / link.readlink()) != os.path.abspath(target):
            raise SystemExit(f'Refusing to replace a different link: {link}')
        continue
    if link.exists():
        raise SystemExit(f'Refusing to replace an existing file/directory: {link}')
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(os.path.relpath(target, link.parent), target_is_directory=target.is_dir())
print(f'Restored {len(links)} shared dependency links.')
if missing:
    print('Dependencies absent in this ORFS checkout (some inherited benchmarks are optional):')
    print('\n'.join(sorted(set(missing))))
