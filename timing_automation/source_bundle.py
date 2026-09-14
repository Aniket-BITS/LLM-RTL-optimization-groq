#!/usr/bin/env python3
"""
Canonical multi-file RTL source bundle for nEBULA.

SourceBundle is the runtime representation of all RTL sources belonging
to one DesignManifest.

Goals
-----
1. Stop treating a design as one RTL file.
2. Load the complete source_files[] list from DesignManifest.
3. Hash every source independently.
4. Track which RTL modules are defined in which source file.
5. Detect source mutations deterministically.
6. Preserve source bytes exactly for future patch/splice operations.

Important
---------
The module scanner in this file is intentionally lightweight.

It is suitable for:
    - building a source/module index
    - locating likely module ownership
    - regression checks

It is NOT the authoritative parser for linting or interface comparison.

Later:
    Yosys AST/JSON will be authoritative for structural linting.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple


# ============================================================
# Local imports
# ============================================================

try:
    from design_manifest import (
        DesignManifest,
        ManifestError,
        sha256_file,
    )
except ImportError:
    # Allows:
    #
    #     python3 timing_automation/source_bundle.py
    #
    from timing_automation.design_manifest import (
        DesignManifest,
        ManifestError,
        sha256_file,
    )


# ============================================================
# Exceptions
# ============================================================

class SourceBundleError(RuntimeError):
    """Raised when a SourceBundle cannot be constructed safely."""


class DuplicateModuleError(SourceBundleError):
    """
    Raised when the same module name is defined by multiple source files.
    """


class ModuleNotFoundError(SourceBundleError):
    """
    Raised when a requested module does not exist in the source bundle.
    """


# ============================================================
# Module scanning
# ============================================================

# Lightweight module declaration recognizer.
#
# Supports common forms:
#
#   module foo (...);
#   module foo #(parameter WIDTH = 8) (...);
#   module automatic foo (...);
#
# This is intentionally not used as an authoritative HDL parser.
MODULE_DECL_RE = re.compile(
    r"""
    (?<![\w$])
    module
    \s+
    (?:
        automatic
        \s+
    )?
    (?P<name>[A-Za-z_][A-Za-z0-9_$]*)
    """,
    re.VERBOSE,
)


def strip_verilog_comments(text: str) -> str:
    """
    Replace Verilog comments with whitespace while preserving line count.

    This prevents commented-out declarations such as:

        // module old_design(...);

    from being detected.

    Newlines are preserved so future line-number diagnostics remain
    meaningful.

    This is intentionally a lexical helper, not a Verilog parser.
    """

    result: List[str] = []

    i = 0
    length = len(text)

    in_line_comment = False
    in_block_comment = False
    in_string = False

    while i < length:

        ch = text[i]

        next_ch = (
            text[i + 1]
            if i + 1 < length
            else ""
        )

        # ----------------------------------------------------
        # String literal
        # ----------------------------------------------------

        if in_string:

            result.append(ch)

            if ch == "\\":
                # Preserve escaped character.
                if i + 1 < length:
                    result.append(
                        text[i + 1]
                    )
                    i += 2
                    continue

            elif ch == '"':
                in_string = False

            i += 1
            continue

        # ----------------------------------------------------
        # Line comment
        # ----------------------------------------------------

        if in_line_comment:

            if ch == "\n":
                result.append("\n")
                in_line_comment = False
            else:
                result.append(" ")

            i += 1
            continue

        # ----------------------------------------------------
        # Block comment
        # ----------------------------------------------------

        if in_block_comment:

            if ch == "*" and next_ch == "/":
                result.append(" ")
                result.append(" ")
                i += 2
                in_block_comment = False
                continue

            if ch == "\n":
                result.append("\n")
            else:
                result.append(" ")

            i += 1
            continue

        # ----------------------------------------------------
        # Enter string/comment
        # ----------------------------------------------------

        if ch == '"':
            result.append(ch)
            in_string = True
            i += 1
            continue

        if ch == "/" and next_ch == "/":
            result.append(" ")
            result.append(" ")
            in_line_comment = True
            i += 2
            continue

        if ch == "/" and next_ch == "*":
            result.append(" ")
            result.append(" ")
            in_block_comment = True
            i += 2
            continue

        result.append(ch)

        i += 1

    return "".join(result)


def scan_module_names(text: str) -> List[str]:
    """
    Return module names declared in Verilog/SystemVerilog source.

    Order is source order.

    Duplicate declarations inside the same text are preserved here so
    SourceBundle validation can detect them.
    """

    cleaned = strip_verilog_comments(
        text
    )

    return [
        match.group("name")
        for match in MODULE_DECL_RE.finditer(
            cleaned
        )
    ]


# ============================================================
# Source file object
# ============================================================

@dataclass(frozen=True)
class SourceFile:
    """
    One immutable source snapshot inside a SourceBundle.

    `content` is stored as raw bytes so future deterministic patching can
    preserve untouched bytes exactly.
    """

    manifest_path: str

    absolute_path: Path

    content: bytes

    sha256: str

    modules: Tuple[str, ...]

    # --------------------------------------------------------
    # Convenience
    # --------------------------------------------------------

    @property
    def size_bytes(self) -> int:
        return len(
            self.content
        )

    def text(
        self,
        encoding: str = "utf-8",
    ) -> str:
        """
        Decode source text.

        RTL source is expected to be UTF-8 compatible.
        """

        try:
            return self.content.decode(
                encoding
            )

        except UnicodeDecodeError as exc:
            raise SourceBundleError(
                f"cannot decode RTL source "
                f"{self.manifest_path!r} as {encoding}"
            ) from exc


# ============================================================
# SourceBundle
# ============================================================

class SourceBundle:
    """
    Complete source snapshot for one DesignManifest.

    Example:

        manifest
            source_files:
                - rtl/top.v
                - rtl/alu.v
                - rtl/helper.v

        SourceBundle
            files["rtl/top.v"]
            files["rtl/alu.v"]
            files["rtl/helper.v"]

            module_to_file["top"]    -> "rtl/top.v"
            module_to_file["alu"]    -> "rtl/alu.v"
            module_to_file["helper"] -> "rtl/helper.v"
    """

    def __init__(
        self,
        *,
        manifest: DesignManifest,
        base_dir: Path,
        files: Mapping[str, SourceFile],
    ) -> None:

        self.manifest = manifest

        self.base_dir = Path(
            base_dir
        ).resolve()

        self.files: Dict[str, SourceFile] = dict(
            files
        )

        self.module_to_file: Dict[str, str] = {}

        self._build_module_index()

        self.validate()

    # --------------------------------------------------------
    # Construction
    # --------------------------------------------------------

    @classmethod
    def from_manifest(
        cls,
        manifest: DesignManifest,
        base_dir: Path,
        *,
        verify_manifest_hashes: bool = True,
    ) -> "SourceBundle":
        """
        Load all source files declared by a DesignManifest.

        If verify_manifest_hashes=True:
            current source bytes must match source_hashes stored in the
            manifest.
        """

        base_dir = Path(
            base_dir
        ).resolve()

        manifest.validate(
            require_source_hashes=verify_manifest_hashes
        )

        loaded: Dict[str, SourceFile] = {}

        for source_name in manifest.source_files:

            absolute_path = (
                manifest.resolve_path(
                    source_name,
                    base_dir,
                )
            )

            if not absolute_path.is_file():
                raise SourceBundleError(
                    f"source file does not exist: "
                    f"{source_name!r} -> {absolute_path}"
                )

            try:
                content = absolute_path.read_bytes()

            except OSError as exc:
                raise SourceBundleError(
                    f"failed to read source file "
                    f"{absolute_path}: {exc}"
                ) from exc

            digest = hashlib.sha256(
                content
            ).hexdigest()

            if verify_manifest_hashes:

                expected = (
                    manifest.source_hashes.get(
                        source_name
                    )
                )

                if expected is None:
                    raise SourceBundleError(
                        f"manifest has no SHA-256 for "
                        f"{source_name!r}"
                    )

                if digest.lower() != expected.lower():
                    raise SourceBundleError(
                        "source hash mismatch for "
                        f"{source_name!r}: "
                        f"expected {expected}, "
                        f"got {digest}"
                    )

            try:
                text = content.decode(
                    "utf-8"
                )

            except UnicodeDecodeError as exc:
                raise SourceBundleError(
                    f"RTL source is not valid UTF-8: "
                    f"{absolute_path}"
                ) from exc

            modules = tuple(
                scan_module_names(
                    text
                )
            )

            loaded[source_name] = SourceFile(
                manifest_path=source_name,
                absolute_path=absolute_path,
                content=content,
                sha256=digest,
                modules=modules,
            )

        return cls(
            manifest=manifest,
            base_dir=base_dir,
            files=loaded,
        )

    # --------------------------------------------------------
    # Validation
    # --------------------------------------------------------

    def validate(self) -> None:

        expected_files = set(
            self.manifest.source_files
        )

        actual_files = set(
            self.files
        )

        if expected_files != actual_files:

            missing = (
                expected_files
                - actual_files
            )

            unexpected = (
                actual_files
                - expected_files
            )

            parts: List[str] = []

            if missing:
                parts.append(
                    "missing: "
                    + ", ".join(
                        sorted(missing)
                    )
                )

            if unexpected:
                parts.append(
                    "unexpected: "
                    + ", ".join(
                        sorted(unexpected)
                    )
                )

            raise SourceBundleError(
                "SourceBundle file set does not match manifest: "
                + "; ".join(parts)
            )

        if not self.files:
            raise SourceBundleError(
                "SourceBundle contains no source files"
            )

        # Top module must be discoverable in the bundle.
        if (
            self.manifest.top
            not in self.module_to_file
        ):
            raise ModuleNotFoundError(
                f"top module "
                f"{self.manifest.top!r} "
                "was not found in any source file"
            )

    # --------------------------------------------------------
    # Module index
    # --------------------------------------------------------

    def _build_module_index(self) -> None:

        module_to_file: Dict[str, str] = {}

        for source_name in self.manifest.source_files:

            source = self.files[
                source_name
            ]

            local_seen = set()

            for module in source.modules:

                if module in local_seen:
                    raise DuplicateModuleError(
                        f"module {module!r} is declared "
                        f"more than once in "
                        f"{source_name!r}"
                    )

                local_seen.add(
                    module
                )

                existing = (
                    module_to_file.get(
                        module
                    )
                )

                if existing is not None:
                    raise DuplicateModuleError(
                        f"module {module!r} is defined "
                        f"in both {existing!r} "
                        f"and {source_name!r}"
                    )

                module_to_file[module] = (
                    source_name
                )

        self.module_to_file = (
            module_to_file
        )

    # --------------------------------------------------------
    # Accessors
    # --------------------------------------------------------

    def source_names(self) -> List[str]:
        """
        Return sources in manifest order.
        """

        return list(
            self.manifest.source_files
        )

    def modules(self) -> List[str]:
        """
        Return all discovered module names sorted alphabetically.
        """

        return sorted(
            self.module_to_file
        )

    def get_source(
        self,
        source_name: str,
    ) -> SourceFile:

        try:
            return self.files[
                source_name
            ]

        except KeyError as exc:
            raise SourceBundleError(
                f"source not present in bundle: "
                f"{source_name!r}"
            ) from exc

    def get_source_for_module(
        self,
        module_name: str,
    ) -> SourceFile:

        source_name = (
            self.module_to_file.get(
                module_name
            )
        )

        if source_name is None:
            raise ModuleNotFoundError(
                f"module {module_name!r} "
                "not found in SourceBundle"
            )

        return self.files[
            source_name
        ]

    def file_for_module(
        self,
        module_name: str,
    ) -> str:
        """
        Return manifest-relative source filename containing module.
        """

        source = (
            self.get_source_for_module(
                module_name
            )
        )

        return source.manifest_path

    def top_source(self) -> SourceFile:
        """
        Return source file containing manifest.top.
        """

        return self.get_source_for_module(
            self.manifest.top
        )

    # --------------------------------------------------------
    # Hash helpers
    # --------------------------------------------------------

    def source_hashes(self) -> Dict[str, str]:
        """
        Return snapshot SHA-256 values for all sources.
        """

        return {
            source_name: self.files[
                source_name
            ].sha256
            for source_name
            in self.manifest.source_files
        }

    @staticmethod
    def _canonical_executable_text(text: str) -> str:
        """Remove comments and formatting-only whitespace without changing strings.

        This is intentionally *not* semantic equivalence.  It only provides an
        early identity for comment/whitespace-only rewrites so expensive formal
        and physical stages are not spent on obvious textual no-ops.
        """
        cleaned = strip_verilog_comments(text)
        out: List[str] = []
        pending_space = False
        in_string = False
        escaped = False

        def word_char(ch: str) -> bool:
            return ch.isalnum() or ch in "_$"

        for ch in cleaned:
            if in_string:
                out.append(ch)
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                # Keep a separator only when whitespace separates two lexical words.
                if pending_space and out and word_char(out[-1]):
                    out.append(" ")
                pending_space = False
                out.append(ch)
                in_string = True
                continue
            if ch.isspace():
                pending_space = True
                continue
            if pending_space and out and word_char(out[-1]) and word_char(ch):
                out.append(" ")
            pending_space = False
            out.append(ch)
        return "".join(out).strip()

    def bundle_sha256(self) -> str:
        """Deterministic identity for executable sources plus stable design contract."""
        import json
        payload = {
            "top": self.manifest.top,
            "language": self.manifest.language,
            "source_files": list(self.manifest.source_files),
            "source_hashes": self.source_hashes(),
            "defines": list(self.manifest.defines),
            "parameters": dict(self.manifest.parameters),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def executable_identity_sha256(self) -> str:
        """Identity that ignores comments/formatting but not executable token changes."""
        import json
        sources = []
        for source_name in self.manifest.source_files:
            canonical = self._canonical_executable_text(self.files[source_name].text())
            sources.append({
                "path": source_name,
                "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            })
        payload = {
            "top": self.manifest.top,
            "language": self.manifest.language,
            "sources": sources,
            "defines": list(self.manifest.defines),
            "parameters": dict(self.manifest.parameters),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def identity_record(self, *, include_text: bool = False) -> Dict[str, object]:
        """Return deterministic whole-design identity for optimizer requests.

        Mutation authority is the exact SourceBundle, not diagnostic focus ranges.
        """
        h = hashlib.sha256()
        sources = []
        for source_name in self.manifest.source_files:
            source = self.files[source_name]
            h.update(f"{source_name}\0{source.sha256}\n".encode("utf-8"))
            item = {
                "path": source_name,
                "sha256": source.sha256,
                "size_bytes": source.size_bytes,
                "modules": list(source.modules),
            }
            if include_text:
                item["text"] = source.text()
            sources.append(item)
        return {
            "schema": "nebula_source_bundle_identity/2",
            "dataset": self.manifest.dataset,
            "design_id": self.manifest.design_id,
            "top": self.manifest.top,
            "bundle_sha256": self.bundle_sha256(),
            "legacy_source_only_sha256": h.hexdigest(),
            "executable_identity_sha256": self.executable_identity_sha256(),
            "sources": sources,
            "module_to_file": dict(sorted(self.module_to_file.items())),
        }

    def prompt_context(self) -> Dict[str, object]:
        """Whole-design immutable prompt snapshot with exact file hashes."""
        return self.identity_record(include_text=True)

    def verify_against_disk(self) -> Dict[str, bool]:
        """
        Check whether each source still matches this bundle snapshot.

        This detects edits made after SourceBundle construction.
        """

        result: Dict[str, bool] = {}

        for source_name in self.manifest.source_files:

            source = self.files[
                source_name
            ]

            path = source.absolute_path

            if not path.is_file():
                result[source_name] = False
                continue

            try:
                current_digest = (
                    sha256_file(
                        path
                    )
                )
            except OSError:
                result[source_name] = False
                continue

            result[source_name] = (
                current_digest.lower()
                == source.sha256.lower()
            )

        return result

    def all_sources_unchanged(self) -> bool:
        """
        Return True if every source still matches this snapshot.
        """

        verification = (
            self.verify_against_disk()
        )

        return all(
            verification.values()
        )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    def summary(self) -> Dict[str, object]:

        return {
            "dataset": self.manifest.dataset,
            "design_id": self.manifest.design_id,
            "top": self.manifest.top,
            "source_count": len(
                self.files
            ),
            "module_count": len(
                self.module_to_file
            ),
            "sources": [
                {
                    "path": source_name,
                    "sha256": (
                        self.files[
                            source_name
                        ].sha256
                    ),
                    "size_bytes": (
                        self.files[
                            source_name
                        ].size_bytes
                    ),
                    "modules": list(
                        self.files[
                            source_name
                        ].modules
                    ),
                }
                for source_name
                in self.manifest.source_files
            ],
            "module_to_file": dict(
                sorted(
                    self.module_to_file.items()
                )
            ),
        }


# ============================================================
# Self-test
# ============================================================

def run_self_test() -> None:
    """
    Standalone SourceBundle regression test.

    Tests:
        - multi-file loading
        - independent hashes
        - module ownership
        - top-module resolution
        - comments do not produce fake modules
        - disk mutation detection
        - duplicate module rejection
    """

    from design_manifest import (
        ClockSpec,
        PhysicalPolicy,
        ResetSpec,
        VerificationPolicy,
    )

    with tempfile.TemporaryDirectory() as tmp:

        root = Path(
            tmp
        )

        rtl = (
            root
            / "rtl"
        )

        rtl.mkdir()

        top_file = (
            rtl
            / "top.v"
        )

        alu_file = (
            rtl
            / "alu.v"
        )

        helper_file = (
            rtl
            / "helper.v"
        )

        top_file.write_text(
            (
                "// module fake_commented_module();\n"
                "// endmodule\n"
                "\n"
                "module top (\n"
                "    input  wire clk,\n"
                "    input  wire rst,\n"
                "    input  wire [7:0] a,\n"
                "    input  wire [7:0] b,\n"
                "    output wire [7:0] y\n"
                ");\n"
                "\n"
                "alu u_alu (\n"
                "    .a(a),\n"
                "    .b(b),\n"
                "    .y(y)\n"
                ");\n"
                "\n"
                "endmodule\n"
            ),
            encoding="utf-8",
        )

        alu_file.write_text(
            (
                "/*\n"
                "module also_fake();\n"
                "endmodule\n"
                "*/\n"
                "\n"
                "module alu #(\n"
                "    parameter WIDTH = 8\n"
                ") (\n"
                "    input  wire [WIDTH-1:0] a,\n"
                "    input  wire [WIDTH-1:0] b,\n"
                "    output wire [WIDTH-1:0] y\n"
                ");\n"
                "\n"
                "assign y = a + b;\n"
                "\n"
                "endmodule\n"
            ),
            encoding="utf-8",
        )

        helper_file.write_text(
            (
                "module helper (\n"
                "    input  wire a,\n"
                "    output wire y\n"
                ");\n"
                "\n"
                "assign y = a;\n"
                "\n"
                "endmodule\n"
            ),
            encoding="utf-8",
        )

        manifest = DesignManifest(
            dataset="source_bundle_self_test",
            design_id="bundle_test",
            top="top",
            source_files=[
                "rtl/top.v",
                "rtl/alu.v",
                "rtl/helper.v",
            ],
            include_dirs=[
                "rtl",
            ],
            defines=[],
            parameters={
                "WIDTH": 8,
            },
            language="verilog",
            clocks=[
                ClockSpec(
                    name="clk",
                    port="clk",
                    period_ns=1.0,
                )
            ],
            resets=[
                ResetSpec(
                    name="rst",
                    port="rst",
                )
            ],
            constraints={},
            verification=VerificationPolicy(
                backend="yosys",
                mode="sequential_equivalence",
            ),
            physical=PhysicalPolicy(
                platform="nangate45",
                evaluation_stage="synth",
            ),
        )

        manifest.refresh_source_hashes(
            root
        )

        bundle = (
            SourceBundle.from_manifest(
                manifest,
                root,
            )
        )

        # ----------------------------------------------------
        # Multi-file behavior
        # ----------------------------------------------------

        assert bundle.source_names() == [
            "rtl/top.v",
            "rtl/alu.v",
            "rtl/helper.v",
        ]

        assert len(
            bundle.source_hashes()
        ) == 3

        assert len(
            set(
                bundle.source_hashes().values()
            )
        ) == 3

        # ----------------------------------------------------
        # Module ownership
        # ----------------------------------------------------

        assert bundle.modules() == [
            "alu",
            "helper",
            "top",
        ]

        assert (
            bundle.file_for_module(
                "top"
            )
            == "rtl/top.v"
        )

        assert (
            bundle.file_for_module(
                "alu"
            )
            == "rtl/alu.v"
        )

        assert (
            bundle.file_for_module(
                "helper"
            )
            == "rtl/helper.v"
        )

        # Commented-out declarations must not appear.
        assert (
            "fake_commented_module"
            not in bundle.modules()
        )

        assert (
            "also_fake"
            not in bundle.modules()
        )

        # ----------------------------------------------------
        # Top resolution
        # ----------------------------------------------------

        assert (
            bundle.top_source().manifest_path
            == "rtl/top.v"
        )

        # ----------------------------------------------------
        # Mutation detection
        # ----------------------------------------------------

        assert (
            bundle.all_sources_unchanged()
            is True
        )

        helper_file.write_text(
            (
                "module helper (\n"
                "    input  wire a,\n"
                "    output wire y\n"
                ");\n"
                "\n"
                "assign y = ~a;\n"
                "\n"
                "endmodule\n"
            ),
            encoding="utf-8",
        )

        status = (
            bundle.verify_against_disk()
        )

        assert status == {
            "rtl/top.v": True,
            "rtl/alu.v": True,
            "rtl/helper.v": False,
        }

        # ----------------------------------------------------
        # Duplicate module rejection
        # ----------------------------------------------------

        duplicate_file = (
            rtl
            / "duplicate.v"
        )

        duplicate_file.write_text(
            (
                "module alu (\n"
                "    input wire a,\n"
                "    output wire y\n"
                ");\n"
                "assign y = a;\n"
                "endmodule\n"
            ),
            encoding="utf-8",
        )

        duplicate_manifest = (
            DesignManifest(
                dataset="source_bundle_self_test",
                design_id="duplicate_test",
                top="top",
                source_files=[
                    "rtl/top.v",
                    "rtl/alu.v",
                    "rtl/duplicate.v",
                ],
                language="verilog",
                verification=VerificationPolicy(
                    backend="yosys",
                    mode=(
                        "sequential_equivalence"
                    ),
                ),
                physical=PhysicalPolicy(
                    platform="nangate45",
                    evaluation_stage="synth",
                ),
            )
        )

        duplicate_manifest.refresh_source_hashes(
            root
        )

        duplicate_rejected = False

        try:
            SourceBundle.from_manifest(
                duplicate_manifest,
                root,
            )

        except DuplicateModuleError:
            duplicate_rejected = True

        assert duplicate_rejected

        print(
            "PASS: SourceBundle self-test"
        )

        print(
            "PASS: complete multi-file source loading"
        )

        print(
            "PASS: independent per-source hashing"
        )

        print(
            "PASS: module-to-file ownership index"
        )

        print(
            "PASS: top-module source resolution"
        )

        print(
            "PASS: commented module declarations ignored"
        )

        print(
            "PASS: per-file mutation detection"
        )

        print(
            "PASS: duplicate module definitions rejected"
        )


# ============================================================
# CLI
# ============================================================

def print_bundle(
    bundle: SourceBundle,
) -> None:

    print("=" * 72)
    print("SourceBundle")
    print("=" * 72)

    print(
        f"Dataset      : "
        f"{bundle.manifest.dataset}"
    )

    print(
        f"Design       : "
        f"{bundle.manifest.design_id}"
    )

    print(
        f"Top          : "
        f"{bundle.manifest.top}"
    )

    print(
        f"Source files : "
        f"{len(bundle.files)}"
    )

    print(
        f"Modules      : "
        f"{len(bundle.module_to_file)}"
    )

    print()

    for source_name in bundle.source_names():

        source = bundle.get_source(
            source_name
        )

        print(
            source_name
        )

        print(
            f"  SHA256  : "
            f"{source.sha256}"
        )

        print(
            f"  Bytes   : "
            f"{source.size_bytes}"
        )

        print(
            f"  Modules : "
            f"{', '.join(source.modules) if source.modules else '(none)'}"
        )


def main() -> int:

    parser = argparse.ArgumentParser(
        description=(
            "nEBULA canonical multi-source RTL bundle"
        )
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
        help=(
            "run standalone SourceBundle tests"
        ),
    )

    parser.add_argument(
        "--manifest",
        metavar="DESIGN_MANIFEST.json",
        help=(
            "load a DesignManifest and inspect its SourceBundle"
        ),
    )

    parser.add_argument(
        "--base-dir",
        default=".",
        help=(
            "base directory used for relative manifest paths "
            "(default: current directory)"
        ),
    )

    args = parser.parse_args()

    if args.self_test:

        run_self_test()

        return 0

    if args.manifest:

        manifest_path = Path(
            args.manifest
        )

        manifest = (
            DesignManifest.read_json(
                manifest_path
            )
        )

        bundle = (
            SourceBundle.from_manifest(
                manifest,
                Path(
                    args.base_dir
                ),
            )
        )

        print_bundle(
            bundle
        )

        return 0

    parser.print_help()

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
