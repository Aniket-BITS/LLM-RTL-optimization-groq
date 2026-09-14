#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected 1 match, found {count}")
    return text.replace(old, new, 1)


def main() -> None:
    ap = argparse.ArgumentParser(description="Upgrade map_to_rtl.py to schema v5 with mapping-time SHA-256 provenance")
    ap.add_argument("map_to_rtl", type=Path)
    args = ap.parse_args()

    path = args.map_to_rtl.resolve()
    if not path.exists():
        raise FileNotFoundError(path)

    text = path.read_text(encoding="utf-8")

    if '"schema_version": 5' in text and 'class SourceHashCache' in text:
        print(f"Already upgraded: {path}")
        return

    if '"schema_version": 4' not in text:
        raise RuntimeError("Expected schema-v4 mapper. No changes made.")

    backup = path.with_suffix(path.suffix + ".pre_hash_v4.bak")
    if not backup.exists():
        shutil.copy2(path, backup)

    text = replace_once(
        text,
        "import argparse\nimport json\nimport re\n",
        "import argparse\nimport hashlib\nimport json\nimport re\n",
        "hashlib import",
    )

    helper = '''\n\nclass SourceHashCache:\n    """Race-aware mapping-time SHA-256 cache for resolved RTL sources."""\n\n    def __init__(self) -> None:\n        self._cache: Dict[str, Dict[str, Any]] = {}\n\n    @staticmethod\n    def _signature(path: Path) -> Tuple[int, int]:\n        st = path.stat()\n        return int(st.st_size), int(st.st_mtime_ns)\n\n    def describe(self, path: Path) -> Dict[str, Any]:\n        resolved = path.resolve()\n        key = str(resolved)\n        before = self._signature(resolved)\n\n        cached = self._cache.get(key)\n        if cached is not None:\n            cached_sig = (int(cached["size_bytes"]), int(cached["mtime_ns"]))\n            if before != cached_sig:\n                raise RuntimeError(\n                    "RTL source changed during map_to_rtl execution: "\n                    f"{resolved}"\n                )\n            return dict(cached)\n\n        h = hashlib.sha256()\n        with resolved.open("rb") as f:\n            for chunk in iter(lambda: f.read(1024 * 1024), b""):\n                h.update(chunk)\n\n        after = self._signature(resolved)\n        if after != before:\n            raise RuntimeError(\n                "RTL source changed while being hashed: "\n                f"{resolved}"\n            )\n\n        record = {\n            "path": key,\n            "hash_algorithm": "sha256",\n            "sha256": h.hexdigest(),\n            "size_bytes": before[0],\n            "mtime_ns": before[1],\n            "provenance_scope": "mapping_time_source_file",\n        }\n        self._cache[key] = record\n        return dict(record)\n\n\ndef source_integrity_manifest(\n    cells: Sequence[Dict[str, Any]],\n) -> Dict[str, Any]:\n    """Collect exact user-RTL files referenced by one mapped RTL cone."""\n\n    files: Dict[str, Dict[str, Any]] = {}\n    errors: List[str] = []\n\n    for cell in cells:\n        for source in cell.get("sources", []):\n            if source.get("source_type") != "user_rtl":\n                continue\n\n            integrity = source.get("source_integrity")\n            resolved = source.get("resolved_file")\n\n            if not isinstance(integrity, dict):\n                errors.append(\n                    "Missing SHA-256 for mapped user RTL source: "\n                    f"{resolved or source.get('file')}"\n                )\n                continue\n\n            file_path = str(integrity.get("path") or resolved or "")\n            sha = integrity.get("sha256")\n\n            if not file_path or not sha:\n                errors.append(\n                    "Incomplete SHA-256 record for mapped user RTL source: "\n                    f"{resolved or source.get('file')}"\n                )\n                continue\n\n            prior = files.get(file_path)\n            if prior is not None and prior.get("sha256") != sha:\n                errors.append(\n                    "Conflicting SHA-256 values for the same RTL file: "\n                    f"{file_path}"\n                )\n                continue\n\n            files[file_path] = {\n                "path": file_path,\n                "hash_algorithm": "sha256",\n                "sha256": sha,\n                "size_bytes": integrity.get("size_bytes"),\n                "mtime_ns": integrity.get("mtime_ns"),\n                "provenance_scope": "mapping_time_source_file",\n            }\n\n    status = (\n        "complete"\n        if files and not errors\n        else "not_applicable"\n        if not files and not errors\n        else "error"\n    )\n\n    return {\n        "status": status,\n        "hash_algorithm": "sha256",\n        "provenance_scope": "mapping_time_source_file",\n        "files": [files[k] for k in sorted(files)],\n        "errors": errors,\n    }\n\n\ndef merge_run_source_integrity(\n    mapped_paths: Sequence[Dict[str, Any]],\n) -> Dict[str, Any]:\n    files: Dict[str, Dict[str, Any]] = {}\n    errors: List[str] = []\n\n    for path in mapped_paths:\n        mapping = path.get("rtl_mapping") or {}\n        manifest = mapping.get("source_integrity") or {}\n\n        errors.extend(str(e) for e in manifest.get("errors", []))\n\n        for item in manifest.get("files", []):\n            file_path = str(item.get("path") or "")\n            sha = item.get("sha256")\n            if not file_path or not sha:\n                errors.append(f"Incomplete path-level source-integrity item: {item}")\n                continue\n\n            prior = files.get(file_path)\n            if prior is not None and prior.get("sha256") != sha:\n                errors.append(\n                    "Conflicting path-level SHA-256 values for RTL file: "\n                    f"{file_path}"\n                )\n                continue\n\n            files[file_path] = dict(item)\n\n    status = (\n        "complete"\n        if files and not errors\n        else "not_applicable"\n        if not files and not errors\n        else "error"\n    )\n\n    return {\n        "status": status,\n        "hash_algorithm": "sha256",\n        "provenance_scope": "mapping_time_source_file",\n        "files": [files[k] for k in sorted(files)],\n        "errors": sorted(set(errors)),\n    }\n'''

    text = replace_once(
        text,
        "\n\ndef norm(value: Any) -> Optional[str]:",
        helper + "\n\ndef norm(value: Any) -> Optional[str]:",
        "hash helper insertion",
    )

    text = replace_once(
        text,
        '''def parse_source(\n    raw: str,\n    flow_dir: Path,\n    rtl_roots: Sequence[Path],\n) -> Dict[str, Any]:\n''',
        '''def parse_source(\n    raw: str,\n    flow_dir: Path,\n    rtl_roots: Sequence[Path],\n    source_hasher: SourceHashCache,\n) -> Dict[str, Any]:\n''',
        "parse_source signature",
    )

    text = replace_once(
        text,
        '''    return {\n        "raw": raw,\n        "file": filename,\n        "resolved_file": (\n            str(resolved)\n            if resolved\n            else None\n        ),\n        "start_line": start,\n        "end_line": end,\n        "source_type": source_type,\n    }\n''',
        '''    source_integrity = None\n\n\n    if source_type == "user_rtl":\n\n        if resolved is None:\n\n            raise RuntimeError(\n                "Mapped USER RTL source could not be resolved for hashing: "\n                f"{filename}"\n            )\n\n\n        source_integrity = source_hasher.describe(\n            resolved\n        )\n\n\n    return {\n        "raw": raw,\n        "file": filename,\n        "resolved_file": (\n            str(resolved)\n            if resolved\n            else None\n        ),\n        "start_line": start,\n        "end_line": end,\n        "source_type": source_type,\n        "sha256": (\n            source_integrity.get("sha256")\n            if source_integrity\n            else None\n        ),\n        "source_integrity": source_integrity,\n    }\n''',
        "parse_source return",
    )

    text = replace_once(
        text,
        '''def serialize_cell(\n    name: str,\n    cell: Dict[str, Any],\n    flow_dir: Path,\n    rtl_roots: Sequence[Path],\n) -> Dict[str, Any]:\n''',
        '''def serialize_cell(\n    name: str,\n    cell: Dict[str, Any],\n    flow_dir: Path,\n    rtl_roots: Sequence[Path],\n    source_hasher: SourceHashCache,\n) -> Dict[str, Any]:\n''',
        "serialize_cell signature",
    )

    text = replace_once(
        text,
        '''            parse_source(\n                raw,\n                flow_dir,\n                rtl_roots,\n            )\n''',
        '''            parse_source(\n                raw,\n                flow_dir,\n                rtl_roots,\n                source_hasher,\n            )\n''',
        "parse_source call",
    )

    text = replace_once(
        text,
        '''                regions[key] = {\n                    "file": filename,\n                    "start_line": int(\n                        start\n                    ),\n''',
        '''                regions[key] = {\n                    "file": filename,\n                    "sha256": source.get(\n                        "sha256"\n                    ),\n                    "hash_algorithm": (\n                        "sha256"\n                        if source.get("sha256")\n                        else None\n                    ),\n                    "start_line": int(\n                        start\n                    ),\n''',
        "rtl_regions hash fields",
    )

    text = replace_once(
        text,
        '''def map_path(\n    path: Dict[str, Any],\n    index: RTLGraphIndex,\n    flow_dir: Path,\n    rtl_roots: Sequence[Path],\n) -> Dict[str, Any]:\n''',
        '''def map_path(\n    path: Dict[str, Any],\n    index: RTLGraphIndex,\n    flow_dir: Path,\n    rtl_roots: Sequence[Path],\n    source_hasher: SourceHashCache,\n) -> Dict[str, Any]:\n''',
        "map_path signature",
    )

    text = replace_once(
        text,
        '''        serialize_cell(\n            name,\n            index.cells[\n                name\n            ],\n            flow_dir,\n            rtl_roots,\n        )\n''',
        '''        serialize_cell(\n            name,\n            index.cells[\n                name\n            ],\n            flow_dir,\n            rtl_roots,\n            source_hasher,\n        )\n''',
        "serialize_cell call",
    )

    text = replace_once(
        text,
        '''    regions = rtl_regions(\n        cells\n    )\n\n\n    branch_points = (\n''',
        '''    regions = rtl_regions(\n        cells\n    )\n\n\n    source_integrity = (\n        source_integrity_manifest(\n            cells\n        )\n    )\n\n\n    if (\n        regions\n        and source_integrity.get("status")\n        != "complete"\n    ):\n\n        raise RuntimeError(\n            "Mapped USER RTL cone does not have complete "\n            "mapping-time SHA-256 provenance: "\n            f"{source_integrity.get('errors', [])}"\n        )\n\n\n    branch_points = (\n''',
        "path integrity construction",
    )

    text = replace_once(
        text,
        '''            "has_user_rtl_source": bool(\n                regions\n            ),\n\n            "direct_wire_path": (\n''',
        '''            "has_user_rtl_source": bool(\n                regions\n            ),\n\n            "source_integrity": (\n                source_integrity\n            ),\n\n            "direct_wire_path": (\n''',
        "mapping integrity field",
    )

    text = replace_once(
        text,
        '''    index = RTLGraphIndex(\n        graph,\n        args.design,\n    )\n\n\n    mapped = [\n''',
        '''    index = RTLGraphIndex(\n        graph,\n        args.design,\n    )\n\n\n    source_hasher = (\n        SourceHashCache()\n    )\n\n\n    mapped = [\n''',
        "hasher construction",
    )

    text = replace_once(
        text,
        '''            index,\n            flow_dir,\n            rtl_roots,\n        )\n\n        for path in paths\n''',
        '''            index,\n            flow_dir,\n            rtl_roots,\n            source_hasher,\n        )\n\n        for path in paths\n''',
        "map_path call",
    )

    text = replace_once(
        text,
        '''    total = len(\n        mapped\n    )\n\n\n    output = {\n''',
        '''    total = len(\n        mapped\n    )\n\n\n    complete_hash_count = sum(\n        1\n        for path in mapped\n        if (\n            (\n                (path.get("rtl_mapping") or {})\n                .get("source_integrity")\n                or {}\n            ).get("status")\n            == "complete"\n        )\n    )\n\n\n    run_source_integrity = (\n        merge_run_source_integrity(\n            mapped\n        )\n    )\n\n\n    if (\n        source_count\n        and run_source_integrity.get("status")\n        != "complete"\n    ):\n\n        raise RuntimeError(\n            "Run-level USER RTL SHA-256 provenance is incomplete: "\n            f"{run_source_integrity.get('errors', [])}"\n        )\n\n\n    output = {\n''',
        "run integrity construction",
    )

    text = replace_once(
        text,
        '        "schema_version": 4,\n',
        '        "schema_version": 5,\n',
        "schema bump",
    )

    text = replace_once(
        text,
        '''        "source_checkpoint": str(\n            checkpoint\n        ),\n\n        "summary": {\n''',
        '''        "source_checkpoint": str(\n            checkpoint\n        ),\n\n        "source_integrity": (\n            run_source_integrity\n        ),\n\n        "summary": {\n''',
        "top-level integrity",
    )

    text = replace_once(
        text,
        '''            "paths_with_remaining_logical_reconvergence": (\n                ambiguous_count\n            ),\n\n            "structural_mapping_coverage": (\n''',
        '''            "paths_with_remaining_logical_reconvergence": (\n                ambiguous_count\n            ),\n\n            "paths_with_complete_mapping_time_source_hash": (\n                complete_hash_count\n            ),\n\n            "mapping_time_source_hash_coverage": (\n                round(\n                    complete_hash_count\n                    / source_count,\n                    4,\n                )\n                if source_count\n                else 0.0\n            ),\n\n            "structural_mapping_coverage": (\n''',
        "summary hash coverage",
    )

    text = replace_once(
        text,
        '''    print(\n        f"Remaining reconvergence : "\n        f"{ambiguous_count}"\n    )\n\n\n    if mapped:\n''',
        '''    print(\n        f"Remaining reconvergence : "\n        f"{ambiguous_count}"\n    )\n\n    print(\n        f"Source hash coverage    : "\n        f"{complete_hash_count}/{source_count}"\n    )\n\n    print(\n        f"Source hash status      : "\n        f"{run_source_integrity.get('status')}"\n    )\n\n\n    if mapped:\n''',
        "console hash status",
    )

    path.write_text(text, encoding="utf-8")
    print(f"Patched: {path}")
    print(f"Backup : {backup}")
    print("Mapper schema: 5")
    print("Source integrity: SHA-256 at mapping time")


if __name__ == "__main__":
    main()
