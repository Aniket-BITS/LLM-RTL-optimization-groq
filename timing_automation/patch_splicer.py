#!/usr/bin/env python3
"""
Deterministic source patching for nEBULA.

This module implements the architectural change from:

    "LLM rewrites the RTL/design"

to:

    "LLM proposes one or more local patches"

A patch identifies:

    target_file
    target_module
    original_sha256
    start_line
    end_line
    replacement_text

Line ranges are:

    1-based
    inclusive

Example:

    start_line = 10
    end_line   = 12

means original source lines 10, 11 and 12 are replaced.

Important guarantees
--------------------
1. The complete original SourceBundle is the input.
2. Every patch is tied to an exact original source SHA-256.
3. A patch may only target the file containing target_module.
4. The requested line range must be inside target_module.
5. Overlapping patches are rejected.
6. Untouched source bytes are preserved exactly.
7. Untouched source files are preserved exactly.
8. The original files on disk are never modified.

The module-span scanner here is a deterministic safety check.
Yosys AST/JSON will later become authoritative for structural linting.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple


# ============================================================
# Local imports
# ============================================================

try:
    from design_manifest import (
        ClockSpec,
        DesignManifest,
        PhysicalPolicy,
        ResetSpec,
        VerificationPolicy,
        is_sha256,
    )

    from source_bundle import (
        SourceBundle,
    )

except ImportError:
    from timing_automation.design_manifest import (
        ClockSpec,
        DesignManifest,
        PhysicalPolicy,
        ResetSpec,
        VerificationPolicy,
        is_sha256,
    )

    from timing_automation.source_bundle import (
        SourceBundle,
    )


# ============================================================
# Exceptions
# ============================================================

class PatchError(RuntimeError):
    """Base error for deterministic patching."""


class StaleSourceError(PatchError):
    """
    Patch was generated against a different source version.
    """


class PatchTargetError(PatchError):
    """
    Patch targets the wrong file/module/span.
    """


class OverlappingPatchError(PatchError):
    """
    Two patches modify overlapping original source ranges.
    """


# ============================================================
# Lexical module-span scanner
# ============================================================

MODULE_TOKEN_RE = re.compile(
    r"""
    (?:
        (?<![\w$])
        endmodule
        \b
    )
    |
    (?:
        (?<![\w$])
        module
        \s+
        (?:
            automatic
            \s+
        )?
        (?P<module_name>[A-Za-z_][A-Za-z0-9_$]*)
    )
    """,
    re.VERBOSE,
)


def mask_comments_and_strings(text: str) -> str:
    """
    Replace comments and string contents with spaces while preserving
    every newline.

    This keeps source line numbers stable and prevents text such as:

        // module fake();
        "endmodule"

    from confusing the module-span scanner.
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
        # Inside string
        # ----------------------------------------------------

        if in_string:

            if ch == "\\":
                result.append(" ")

                if i + 1 < length:

                    escaped = text[i + 1]

                    if escaped == "\n":
                        result.append("\n")
                    else:
                        result.append(" ")

                    i += 2
                    continue

            if ch == '"':
                result.append(" ")
                in_string = False

            elif ch == "\n":
                result.append("\n")

            else:
                result.append(" ")

            i += 1
            continue

        # ----------------------------------------------------
        # Inside // comment
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
        # Inside /* */ comment
        # ----------------------------------------------------

        if in_block_comment:

            if ch == "*" and next_ch == "/":

                result.append(" ")
                result.append(" ")

                in_block_comment = False

                i += 2
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
            result.append(" ")
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


def source_line_number(
    text: str,
    offset: int,
) -> int:
    """
    Convert a character offset into a 1-based source line number.
    """

    return (
        text.count(
            "\n",
            0,
            offset,
        )
        + 1
    )


def find_module_line_spans(
    text: str,
) -> Dict[str, Tuple[int, int]]:
    """
    Return:

        {
            "top": (1, 20),
            "alu": (22, 40)
        }

    Both line numbers are inclusive.

    Verilog modules cannot normally nest, so encountering a second module
    before endmodule is considered malformed for this deterministic
    scanner.
    """

    masked = mask_comments_and_strings(
        text
    )

    spans: Dict[str, Tuple[int, int]] = {}

    active_name: str | None = None
    active_start: int | None = None

    for match in MODULE_TOKEN_RE.finditer(
        masked
    ):

        token = match.group(0)

        module_name = match.group(
            "module_name"
        )

        # ----------------------------------------------------
        # module <name>
        # ----------------------------------------------------

        if module_name is not None:

            if active_name is not None:
                raise PatchTargetError(
                    "module-span scanner encountered "
                    f"module {module_name!r} before "
                    f"endmodule for {active_name!r}"
                )

            if module_name in spans:
                raise PatchTargetError(
                    f"module {module_name!r} occurs "
                    "more than once in one source"
                )

            active_name = module_name

            active_start = source_line_number(
                masked,
                match.start(),
            )

            continue

        # ----------------------------------------------------
        # endmodule
        # ----------------------------------------------------

        if re.match(
            r"\s*endmodule\b",
            token,
        ):

            if active_name is None:
                raise PatchTargetError(
                    "endmodule encountered without "
                    "matching module declaration"
                )

            end_line = source_line_number(
                masked,
                match.start(),
            )

            assert active_start is not None

            spans[active_name] = (
                active_start,
                end_line,
            )

            active_name = None
            active_start = None

    if active_name is not None:
        raise PatchTargetError(
            f"module {active_name!r} has no matching endmodule"
        )

    return spans


# ============================================================
# Patch object
# ============================================================

@dataclass(frozen=True)
class SourcePatch:
    """
    One deterministic source replacement.

    Line numbers refer to the ORIGINAL SourceBundle snapshot.
    """

    target_file: str

    target_module: str

    original_sha256: str

    start_line: int

    end_line: int

    replacement_text: str

    patch_id: str = ""

    # --------------------------------------------------------
    # Validation
    # --------------------------------------------------------

    def validate(self) -> None:

        if not self.target_file:
            raise PatchError(
                "target_file must not be empty"
            )

        if not self.target_module:
            raise PatchError(
                "target_module must not be empty"
            )

        if not is_sha256(
            self.original_sha256
        ):
            raise PatchError(
                "original_sha256 must be a valid "
                "64-character SHA-256 digest"
            )

        if not isinstance(
            self.start_line,
            int,
        ):
            raise PatchError(
                "start_line must be an integer"
            )

        if not isinstance(
            self.end_line,
            int,
        ):
            raise PatchError(
                "end_line must be an integer"
            )

        if self.start_line < 1:
            raise PatchError(
                "start_line must be >= 1"
            )

        if self.end_line < self.start_line:
            raise PatchError(
                "end_line must be >= start_line"
            )

        if not isinstance(
            self.replacement_text,
            str,
        ):
            raise PatchError(
                "replacement_text must be a string"
            )

    # --------------------------------------------------------
    # JSON-compatible conversion
    # --------------------------------------------------------

    def to_dict(self) -> Dict[str, object]:

        result: Dict[str, object] = {
            "target_file": self.target_file,
            "target_module": self.target_module,
            "original_sha256": self.original_sha256,
            "changed_source_range": {
                "start_line": self.start_line,
                "end_line": self.end_line,
            },
            "replacement_text": self.replacement_text,
        }

        if self.patch_id:
            result["patch_id"] = (
                self.patch_id
            )

        return result

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, object],
    ) -> "SourcePatch":

        range_data = data.get(
            "changed_source_range"
        )

        if not isinstance(
            range_data,
            Mapping,
        ):
            raise PatchError(
                "patch requires "
                "changed_source_range object"
            )

        try:

            patch = cls(
                patch_id=str(
                    data.get(
                        "patch_id",
                        "",
                    )
                ),
                target_file=str(
                    data["target_file"]
                ),
                target_module=str(
                    data["target_module"]
                ),
                original_sha256=str(
                    data["original_sha256"]
                ),
                start_line=int(
                    range_data[
                        "start_line"
                    ]
                ),
                end_line=int(
                    range_data[
                        "end_line"
                    ]
                ),
                replacement_text=str(
                    data[
                        "replacement_text"
                    ]
                ),
            )

        except KeyError as exc:

            raise PatchError(
                f"missing patch field: "
                f"{exc.args[0]}"
            ) from exc

        patch.validate()

        return patch


# ============================================================
# Splice result
# ============================================================

@dataclass(frozen=True)
class SpliceResult:
    """
    Complete candidate source contents after applying patches.

    `files` contains EVERY source in the SourceBundle, including files
    which were untouched.
    """

    files: Dict[str, bytes]

    changed_files: Tuple[str, ...]

    original_hashes: Dict[str, str]

    candidate_hashes: Dict[str, str]

    def get_bytes(
        self,
        source_name: str,
    ) -> bytes:

        try:
            return self.files[
                source_name
            ]

        except KeyError as exc:
            raise PatchError(
                f"source {source_name!r} "
                "not present in splice result"
            ) from exc

    def get_text(
        self,
        source_name: str,
    ) -> str:

        try:
            return self.get_bytes(
                source_name
            ).decode(
                "utf-8"
            )

        except UnicodeDecodeError as exc:
            raise PatchError(
                f"candidate source "
                f"{source_name!r} "
                "is not valid UTF-8"
            ) from exc


# ============================================================
# Byte/line helpers
# ============================================================

def line_byte_ranges(
    content: bytes,
) -> List[Tuple[int, int]]:
    """
    Return byte ranges for source lines.

    Example:

        b"a\\r\\nb\\r\\n"

    gives:

        line 1 -> bytes [0, 3)
        line 2 -> bytes [3, 6)

    Original newline bytes are therefore preserved.
    """

    lines = content.splitlines(
        keepends=True
    )

    ranges: List[
        Tuple[int, int]
    ] = []

    offset = 0

    for line in lines:

        start = offset

        offset += len(
            line
        )

        ranges.append(
            (
                start,
                offset,
            )
        )

    return ranges


def patch_byte_range(
    content: bytes,
    patch: SourcePatch,
) -> Tuple[int, int]:
    """
    Convert 1-based inclusive source lines to a byte slice:

        [start_byte:end_byte]
    """

    ranges = line_byte_ranges(
        content
    )

    line_count = len(
        ranges
    )

    if patch.start_line > line_count:
        raise PatchTargetError(
            f"patch starts at line "
            f"{patch.start_line}, but "
            f"{patch.target_file!r} "
            f"contains only {line_count} lines"
        )

    if patch.end_line > line_count:
        raise PatchTargetError(
            f"patch ends at line "
            f"{patch.end_line}, but "
            f"{patch.target_file!r} "
            f"contains only {line_count} lines"
        )

    start_byte = ranges[
        patch.start_line - 1
    ][0]

    end_byte = ranges[
        patch.end_line - 1
    ][1]

    return (
        start_byte,
        end_byte,
    )


# ============================================================
# Patch validation
# ============================================================

def validate_patch_target(
    bundle: SourceBundle,
    patch: SourcePatch,
) -> None:
    """
    Validate one patch against the ORIGINAL SourceBundle.
    """

    patch.validate()

    # --------------------------------------------------------
    # File must exist
    # --------------------------------------------------------

    if patch.target_file not in bundle.files:
        raise PatchTargetError(
            f"patch target file "
            f"{patch.target_file!r} "
            "is not in SourceBundle"
        )

    source = bundle.get_source(
        patch.target_file
    )

    # --------------------------------------------------------
    # SHA safety gate
    # --------------------------------------------------------

    if (
        patch.original_sha256.lower()
        != source.sha256.lower()
    ):
        raise StaleSourceError(
            f"patch for {patch.target_file!r} "
            "was generated against a different source SHA: "
            f"expected {source.sha256}, "
            f"patch contains {patch.original_sha256}"
        )

    # --------------------------------------------------------
    # Module ownership
    # --------------------------------------------------------

    try:

        actual_file = (
            bundle.file_for_module(
                patch.target_module
            )
        )

    except Exception as exc:

        raise PatchTargetError(
            f"target module "
            f"{patch.target_module!r} "
            "does not exist in SourceBundle"
        ) from exc

    if actual_file != patch.target_file:
        raise PatchTargetError(
            f"target module "
            f"{patch.target_module!r} "
            f"is located in {actual_file!r}, "
            f"not {patch.target_file!r}"
        )

    # --------------------------------------------------------
    # Line range must exist
    # --------------------------------------------------------

    patch_byte_range(
        source.content,
        patch,
    )

    # --------------------------------------------------------
    # Line range must remain inside target module
    # --------------------------------------------------------

    spans = find_module_line_spans(
        source.text()
    )

    module_span = spans.get(
        patch.target_module
    )

    if module_span is None:
        raise PatchTargetError(
            f"could not locate source span for "
            f"module {patch.target_module!r}"
        )

    module_start, module_end = (
        module_span
    )

    if (
        patch.start_line < module_start
        or patch.end_line > module_end
    ):
        raise PatchTargetError(
            f"patch range "
            f"{patch.start_line}-{patch.end_line} "
            f"is outside module "
            f"{patch.target_module!r} span "
            f"{module_start}-{module_end}"
        )


def validate_no_overlaps(
    patches: Sequence[SourcePatch],
) -> None:
    """
    Reject overlapping ranges within the same original source file.

    All line coordinates refer to the original file.
    """

    by_file: Dict[
        str,
        List[SourcePatch]
    ] = {}

    for patch in patches:

        by_file.setdefault(
            patch.target_file,
            [],
        ).append(
            patch
        )

    for source_name, file_patches in (
        by_file.items()
    ):

        ordered = sorted(
            file_patches,
            key=lambda patch: (
                patch.start_line,
                patch.end_line,
            ),
        )

        previous: SourcePatch | None = None

        for patch in ordered:

            if previous is not None:

                if (
                    patch.start_line
                    <= previous.end_line
                ):
                    raise OverlappingPatchError(
                        f"overlapping patches in "
                        f"{source_name!r}: "
                        f"{previous.start_line}-"
                        f"{previous.end_line} and "
                        f"{patch.start_line}-"
                        f"{patch.end_line}"
                    )

            previous = patch


# ============================================================
# Deterministic splicer
# ============================================================

def apply_patches(
    bundle: SourceBundle,
    patches: Sequence[SourcePatch],
) -> SpliceResult:
    """
    Apply patches entirely in memory.

    The SourceBundle and original files on disk are not modified.

    Every range is interpreted against the ORIGINAL source snapshot.

    Patches within each file are therefore applied from bottom to top so
    earlier replacements cannot shift later original line coordinates.
    """

    patches = list(
        patches
    )

    if not patches:
        raise PatchError(
            "at least one patch is required"
        )

    # Validate ALL patches before mutating any in-memory candidate.
    for patch in patches:
        validate_patch_target(
            bundle,
            patch,
        )

    validate_no_overlaps(
        patches
    )

    # Complete candidate source bundle begins as exact byte copies.
    candidate_files: Dict[
        str,
        bytes
    ] = {
        source_name: bundle.get_source(
            source_name
        ).content
        for source_name
        in bundle.source_names()
    }

    by_file: Dict[
        str,
        List[SourcePatch]
    ] = {}

    for patch in patches:

        by_file.setdefault(
            patch.target_file,
            [],
        ).append(
            patch
        )

    changed_files: List[str] = []

    for source_name, file_patches in (
        by_file.items()
    ):

        original = bundle.get_source(
            source_name
        ).content

        candidate = original

        # Highest original line number first.
        ordered = sorted(
            file_patches,
            key=lambda patch: (
                patch.start_line,
                patch.end_line,
            ),
            reverse=True,
        )

        for patch in ordered:

            start_byte, end_byte = (
                patch_byte_range(
                    original,
                    patch,
                )
            )

            replacement = (
                patch.replacement_text.encode(
                    "utf-8"
                )
            )

            # Preserve the original line boundary so a final // comment
            # cannot consume untouched following RTL.
            if original[start_byte:end_byte].endswith(b"\n") and replacement and not replacement.endswith(b"\n"):
                replacement += b"\r\n" if original[start_byte:end_byte].endswith(b"\r\n") else b"\n"

            candidate = (
                candidate[:start_byte]
                + replacement
                + candidate[end_byte:]
            )

        candidate_files[
            source_name
        ] = candidate

        if candidate != original:
            changed_files.append(
                source_name
            )

    original_hashes = (
        bundle.source_hashes()
    )

    candidate_hashes = {
        source_name: hashlib.sha256(
            candidate_files[
                source_name
            ]
        ).hexdigest()
        for source_name
        in bundle.source_names()
    }

    return SpliceResult(
        files=candidate_files,
        changed_files=tuple(
            sorted(
                changed_files
            )
        ),
        original_hashes=original_hashes,
        candidate_hashes=candidate_hashes,
    )


# ============================================================
# Self-test
# ============================================================

def run_self_test() -> None:

    with tempfile.TemporaryDirectory() as tmp:

        root = Path(
            tmp
        )

        rtl = root / "rtl"

        rtl.mkdir()

        top_file = (
            rtl
            / "top.v"
        )

        helper_file = (
            rtl
            / "helper.v"
        )

        # Deliberately use CRLF.
        # This lets us verify untouched bytes are not normalized.
        original_top = (
            b"module top (\r\n"
            b"    input  wire clk,\r\n"
            b"    input  wire a,\r\n"
            b"    input  wire b,\r\n"
            b"    output wire y\r\n"
            b");\r\n"
            b"    wire t;\r\n"
            b"    assign t = a & b;\r\n"
            b"    assign y = t;\r\n"
            b"endmodule\r\n"
            b"\r\n"
            b"module untouched (\r\n"
            b"    input  wire a,\r\n"
            b"    output wire y\r\n"
            b");\r\n"
            b"    assign y = a;\r\n"
            b"endmodule\r\n"
        )

        original_helper = (
            b"module helper (\n"
            b"    input wire a,\n"
            b"    output wire y\n"
            b");\n"
            b"assign y = a;\n"
            b"endmodule\n"
        )

        top_file.write_bytes(
            original_top
        )

        helper_file.write_bytes(
            original_helper
        )

        manifest = DesignManifest(
            dataset="patch_self_test",
            design_id="patch_test",
            top="top",
            source_files=[
                "rtl/top.v",
                "rtl/helper.v",
            ],
            language="verilog",
            clocks=[
                ClockSpec(
                    name="clk",
                    port="clk",
                    period_ns=1.0,
                )
            ],
            resets=[],
            constraints={},
            verification=(
                VerificationPolicy(
                    backend="yosys",
                    mode=(
                        "sequential_equivalence"
                    ),
                )
            ),
            physical=(
                PhysicalPolicy(
                    platform="nangate45",
                    evaluation_stage="synth",
                )
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

        top_sha = (
            bundle.get_source(
                "rtl/top.v"
            ).sha256
        )

        # ----------------------------------------------------
        # Module spans
        # ----------------------------------------------------

        spans = find_module_line_spans(
            bundle.get_source(
                "rtl/top.v"
            ).text()
        )

        assert spans["top"] == (
            1,
            10,
        )

        assert spans[
            "untouched"
        ] == (
            12,
            17,
        )

        # ----------------------------------------------------
        # Valid local patch
        #
        # Replace:
        #
        #   wire t;
        #   assign t = a & b;
        #   assign y = t;
        #
        # with:
        #
        #   assign y = a | b;
        #
        # ----------------------------------------------------

        patch = SourcePatch(
            patch_id="patch_001",
            target_file="rtl/top.v",
            target_module="top",
            original_sha256=top_sha,
            start_line=7,
            end_line=9,
            replacement_text=(
                "    assign y = a | b;\r\n"
            ),
        )

        result = apply_patches(
            bundle,
            [
                patch,
            ],
        )

        candidate_top = (
            result.get_bytes(
                "rtl/top.v"
            )
        )

        # Changed source should differ.
        assert (
            candidate_top
            != original_top
        )

        # Other source file must be byte-for-byte identical.
        assert (
            result.get_bytes(
                "rtl/helper.v"
            )
            == original_helper
        )

        # ----------------------------------------------------
        # Untouched module must be byte-for-byte identical
        # ----------------------------------------------------

        untouched_marker = (
            b"module untouched"
        )

        old_untouched_offset = (
            original_top.index(
                untouched_marker
            )
        )

        new_untouched_offset = (
            candidate_top.index(
                untouched_marker
            )
        )

        assert (
            original_top[
                old_untouched_offset:
            ]
            == candidate_top[
                new_untouched_offset:
            ]
        )

        # ----------------------------------------------------
        # Prefix before patch must also remain exact
        # ----------------------------------------------------

        old_prefix = (
            original_top.split(
                b"    wire t;\r\n",
                1,
            )[0]
        )

        new_prefix = (
            candidate_top.split(
                b"    assign y = a | b;\r\n",
                1,
            )[0]
        )

        assert (
            old_prefix
            == new_prefix
        )

        # CRLF in untouched portion must survive.
        assert (
            b"module untouched (\r\n"
            in candidate_top
        )

        # ----------------------------------------------------
        # Original disk source must never be modified
        # ----------------------------------------------------

        assert (
            top_file.read_bytes()
            == original_top
        )

        assert (
            helper_file.read_bytes()
            == original_helper
        )

        # ----------------------------------------------------
        # Hash behavior
        # ----------------------------------------------------

        assert (
            result.original_hashes[
                "rtl/top.v"
            ]
            == top_sha
        )

        assert (
            result.candidate_hashes[
                "rtl/top.v"
            ]
            != top_sha
        )

        assert (
            result.candidate_hashes[
                "rtl/helper.v"
            ]
            == result.original_hashes[
                "rtl/helper.v"
            ]
        )

        assert result.changed_files == (
            "rtl/top.v",
        )

        # ----------------------------------------------------
        # Stale SHA must be rejected
        # ----------------------------------------------------

        stale_rejected = False

        stale_patch = SourcePatch(
            target_file="rtl/top.v",
            target_module="top",
            original_sha256=(
                "0" * 64
            ),
            start_line=7,
            end_line=9,
            replacement_text=(
                "    assign y = a;\r\n"
            ),
        )

        try:

            apply_patches(
                bundle,
                [
                    stale_patch,
                ],
            )

        except StaleSourceError:
            stale_rejected = True

        assert stale_rejected

        # ----------------------------------------------------
        # Wrong module span must be rejected
        #
        # line 16 belongs to module untouched, not top.
        # ----------------------------------------------------

        cross_module_rejected = False

        bad_scope_patch = SourcePatch(
            target_file="rtl/top.v",
            target_module="top",
            original_sha256=top_sha,
            start_line=16,
            end_line=16,
            replacement_text=(
                "    assign y = 1'b0;\r\n"
            ),
        )

        try:

            apply_patches(
                bundle,
                [
                    bad_scope_patch,
                ],
            )

        except PatchTargetError:
            cross_module_rejected = True

        assert cross_module_rejected

        # ----------------------------------------------------
        # Wrong module/file ownership must be rejected
        # ----------------------------------------------------

        wrong_file_rejected = False

        wrong_file_patch = SourcePatch(
            target_file="rtl/helper.v",
            target_module="top",
            original_sha256=(
                bundle.get_source(
                    "rtl/helper.v"
                ).sha256
            ),
            start_line=1,
            end_line=1,
            replacement_text=(
                "module top;\n"
            ),
        )

        try:

            apply_patches(
                bundle,
                [
                    wrong_file_patch,
                ],
            )

        except PatchTargetError:
            wrong_file_rejected = True

        assert wrong_file_rejected

        # ----------------------------------------------------
        # Overlapping patches must be rejected
        # ----------------------------------------------------

        overlap_rejected = False

        patch_a = SourcePatch(
            target_file="rtl/top.v",
            target_module="top",
            original_sha256=top_sha,
            start_line=7,
            end_line=8,
            replacement_text=(
                "    wire t;\r\n"
            ),
        )

        patch_b = SourcePatch(
            target_file="rtl/top.v",
            target_module="top",
            original_sha256=top_sha,
            start_line=8,
            end_line=9,
            replacement_text=(
                "    assign y = a;\r\n"
            ),
        )

        try:

            apply_patches(
                bundle,
                [
                    patch_a,
                    patch_b,
                ],
            )

        except OverlappingPatchError:
            overlap_rejected = True

        assert overlap_rejected

        # ----------------------------------------------------
        # Multiple non-overlapping patches must work
        # ----------------------------------------------------

        patch_1 = SourcePatch(
            target_file="rtl/top.v",
            target_module="top",
            original_sha256=top_sha,
            start_line=7,
            end_line=7,
            replacement_text=(
                "    wire optimized_t;\r\n"
            ),
        )

        patch_2 = SourcePatch(
            target_file="rtl/top.v",
            target_module="top",
            original_sha256=top_sha,
            start_line=9,
            end_line=9,
            replacement_text=(
                "    assign y = optimized_t;\r\n"
            ),
        )

        multiple_result = (
            apply_patches(
                bundle,
                [
                    patch_1,
                    patch_2,
                ],
            )
        )

        multiple_text = (
            multiple_result.get_text(
                "rtl/top.v"
            )
        )

        assert (
            "wire optimized_t;"
            in multiple_text
        )

        assert (
            "assign y = optimized_t;"
            in multiple_text
        )

        print(
            "PASS: patch splicer self-test"
        )

        print(
            "PASS: module/span targeting"
        )

        print(
            "PASS: original SHA safety gate"
        )

        print(
            "PASS: untouched source files preserved byte-for-byte"
        )

        print(
            "PASS: untouched modules preserved byte-for-byte"
        )

        print(
            "PASS: original CRLF bytes preserved"
        )

        print(
            "PASS: original disk sources never modified"
        )

        print(
            "PASS: cross-module patches rejected"
        )

        print(
            "PASS: wrong module/file ownership rejected"
        )

        print(
            "PASS: overlapping patches rejected"
        )

        print(
            "PASS: multiple non-overlapping patches applied"
        )


# ============================================================
# CLI
# ============================================================

def main() -> int:

    parser = argparse.ArgumentParser(
        description=(
            "nEBULA deterministic module/span patch splicer"
        )
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
        help=(
            "run standalone patch-splicer regression tests"
        ),
    )

    args = parser.parse_args()

    if args.self_test:

        run_self_test()

        return 0

    parser.print_help()

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
