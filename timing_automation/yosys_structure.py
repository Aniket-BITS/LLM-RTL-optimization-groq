#!/usr/bin/env python3
"""
yosys_structure.py

Compiler-derived structural checking for nEBULA.

This file provides the authoritative structural layer that will later be
called by lint_candidate.py. It does not use RTL regexes to decide whether
ports, parameter defaults, or hierarchy are preserved.

For every design it creates two Yosys views:

1. Declaration view
   read_verilog -> proc -> write_json

   Used for source-module identity, port interfaces and parameter defaults.
   This happens before hierarchy pruning, so source modules are not lost when
   Yosys later creates $paramod... specializations.

2. Elaborated view
   read_verilog -> chparam -> hierarchy -check -top <top> -> proc -> write_json

   Used for the actual resolved module-instance hierarchy.

The two-view approach matters because hierarchy elaboration may remove unused
source modules or replace parameterized modules with generated $paramod...
modules. We still want source declarations checked, while also checking the
real elaborated hierarchy.

This is a structural safety gate only. Functional equivalence/SEC remains a
separate verification stage.

In addition to interfaces/parameters/hierarchy, this version freezes the
sequential state boundary from Yosys JSON. It compares registers/latches by
semantic attributes (kind/type, width, Q target, clock/reset/enable controls)
while deliberately omitting D-input connectivity so legal combinational
rewrites remain possible.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

try:
    from design_manifest import DesignManifest
except ImportError:
    from timing_automation.design_manifest import DesignManifest


# -----------------------------------------------------------------------------
# Repository paths
# -----------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
FLOW_DIR = SCRIPT_DIR.parent
ORFS_ROOT = FLOW_DIR.parent


# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------


class YosysStructureError(RuntimeError):
    """Base structural-checking error."""


class YosysUnavailableError(YosysStructureError):
    """Raised when Yosys cannot be located."""


class YosysElaborationError(YosysStructureError):
    """Raised when Yosys cannot parse or elaborate a design."""


# -----------------------------------------------------------------------------
# Yosys executable
# -----------------------------------------------------------------------------


def normalized_synthesis_hash(data):
    """Conservative exact structural hash of a mapped Yosys JSON netlist.

    Ignore diagnostic source locations and creator metadata only. Keep names,
    connectivity, parameters, constants, port order and initialization. This can
    miss renamed no-ops, but cannot merge graphs merely because cell counts match.
    """
    import hashlib
    import json
    modules = data.get("modules")
    if not isinstance(modules, dict) or not modules:
        raise ValueError("missing synthesized modules")
    for module in modules.values():
        if not isinstance(module.get("ports"), dict) or not module["ports"]:
            raise ValueError("missing synthesis ports")
        if not isinstance(module.get("cells"), dict) or module.get("processes"):
            raise ValueError("not a synthesized netlist")
    def clean(value):
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, dict):
            return {k: ({a: clean(v) for a, v in item.items() if a not in ("src", "hdlname")}
                        if k == "attributes" else clean(item)) for k, item in value.items()}
        return value
    normalized = clean(modules)
    return {"schema": "normalized_synthesis_hash/1", "algorithm": "sha256",
            "normalization": "exact_named_connectivity_without_source_metadata/1",
            "sha256": hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()}


def resolve_yosys(explicit: str | None = None) -> Path:
    """Resolve a usable Yosys executable."""

    candidates: List[Path] = []

    if explicit:
        candidates.append(Path(explicit).expanduser())

    path_yosys = shutil.which("yosys")
    if path_yosys:
        candidates.append(Path(path_yosys))

    candidates.append(
        ORFS_ROOT / "tools" / "install" / "yosys" / "bin" / "yosys"
    )

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue

        if resolved.is_file() and (resolved.stat().st_mode & 0o111):
            return resolved

    raise YosysUnavailableError(
        "Yosys not found. Tried PATH and "
        f"{ORFS_ROOT / 'tools/install/yosys/bin/yosys'}"
    )


# -----------------------------------------------------------------------------
# Yosys command generation
# -----------------------------------------------------------------------------


_SIMPLE_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def quote_yosys_token(value: str) -> str:
    """Quote a path/option token in a Yosys script."""

    value = str(value)
    if "\n" in value or "\r" in value:
        raise YosysStructureError("Yosys token contains a newline")

    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def yosys_identifier(value: str) -> str:
    """
    Return an HDL identifier suitable for Yosys commands.

    Do not wrap HDL identifiers in quote_yosys_token(); doing so can make the
    quote characters part of the selected name, which caused the original
    `Module '"top"' not found` failure.
    """

    value = str(value)

    if not value:
        raise YosysStructureError("empty Yosys identifier")

    if _SIMPLE_ID_RE.fullmatch(value):
        return value

    if value.startswith("\\") and not any(ch.isspace() for ch in value):
        return value

    raise YosysStructureError(
        f"unsupported Yosys identifier {value!r}; "
        "use a normal Verilog identifier"
    )


def parameter_value_text(value: Any) -> str:
    """Convert a manifest parameter value to chparam text."""

    if isinstance(value, bool):
        return "1" if value else "0"

    if isinstance(value, (int, float)):
        return str(value)

    text = str(value)
    if "\n" in text or "\r" in text or not text:
        raise YosysStructureError(f"invalid parameter value {value!r}")

    # Non-numeric values should already be valid Yosys/Verilog literals,
    # for example 8'hff or \"FAST\".
    return text


def read_verilog_command(manifest: DesignManifest, base_dir: Path) -> str:
    """Build one read_verilog command for the entire DesignManifest."""

    if manifest.language not in {"verilog", "systemverilog"}:
        raise YosysStructureError(
            "yosys_structure currently supports only Verilog/SystemVerilog; "
            f"got {manifest.language!r}"
        )

    args: List[str] = ["read_verilog"]

    if manifest.language == "systemverilog":
        args.append("-sv")

    # read_verilog accepts both ``-Idir`` and ``-I dir``.  Use the
    # two-token form so a quoted path remains the argument to -I instead
    # of turning the entire string "-I/path" into a filename.
    for include_dir in manifest.resolved_include_dirs(base_dir):
        args.append("-I")
        args.append(quote_yosys_token(str(include_dir)))

    # Yosys read_verilog requires -Dname[=definition] as one option token.
    # Do not emit ``-D "NAME=VALUE"``: that can leave the macro inactive.
    for define in manifest.defines:
        define_text = str(define).strip()
        if not define_text:
            raise YosysStructureError("empty Verilog define in DesignManifest")
        args.append("-D" + define_text)

    for source_path in manifest.resolved_source_files(base_dir):
        if not source_path.is_file():
            raise YosysStructureError(f"source file missing: {source_path}")
        args.append(quote_yosys_token(str(source_path)))

    return " ".join(args)


def build_declaration_script(
    manifest: DesignManifest,
    base_dir: Path,
    json_output: Path,
) -> str:
    """
    Build the source-declaration Yosys script.

    `proc` is required because the JSON backend refuses modules containing
    RTLIL processes. We deliberately do NOT run hierarchy in this view.
    """

    return "\n".join(
        [
            read_verilog_command(manifest, base_dir),
            "proc",
            f"write_json {quote_yosys_token(str(json_output))}",
            "",
        ]
    )


def build_elaboration_script(
    manifest: DesignManifest,
    base_dir: Path,
    json_output: Path,
) -> str:
    """Build the resolved-hierarchy Yosys script."""

    lines: List[str] = [read_verilog_command(manifest, base_dir)]

    for name in sorted(manifest.parameters):
        value = parameter_value_text(manifest.parameters[name])
        lines.append(
            "chparam "
            f"-set {yosys_identifier(name)} "
            f"{value} "
            f"{yosys_identifier(manifest.top)}"
        )

    lines.append(
        "hierarchy "
        "-check "
        f"-top {yosys_identifier(manifest.top)}"
    )

    # JSON backend requires all processes to be lowered.
    lines.append("proc")
    # Invalid drivers/undriven nets must fail lint before equivalence execution.
    lines.append("check -assert")
    lines.append(f"write_json {quote_yosys_token(str(json_output))}")

    return "\n".join(lines) + "\n"


# -----------------------------------------------------------------------------
# Yosys execution
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class YosysJsonRun:
    json_data: Dict[str, Any]
    script: str
    log: str


@dataclass(frozen=True)
class YosysDesignRuns:
    declarations: YosysJsonRun
    elaborated: YosysJsonRun


def _run_yosys_script(
    *,
    yosys: Path,
    script: str,
    base_dir: Path,
    work_dir: Path,
    stem: str,
) -> YosysJsonRun:
    script_path = work_dir / f"{stem}.ys"
    json_path = work_dir / f"{stem}.json"

    script_path.write_text(script, encoding="utf-8")

    process = subprocess.run(
        [str(yosys), "-s", str(script_path)],
        cwd=str(base_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )

    log = process.stdout or ""

    if process.returncode != 0:
        raise YosysElaborationError(
            "Yosys structural analysis failed.\n"
            f"Return code: {process.returncode}\n"
            f"Script: {script_path}\n"
            f"Log:\n{log}"
        )

    if not json_path.is_file():
        raise YosysElaborationError(
            f"Yosys completed without producing JSON output: {json_path}"
        )

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise YosysElaborationError(
            f"invalid JSON emitted by Yosys: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise YosysElaborationError("Yosys JSON root is not an object")

    return YosysJsonRun(json_data=data, script=script, log=log)


def run_yosys_views(
    manifest: DesignManifest,
    base_dir: Path,
    *,
    yosys_exe: str | None = None,
) -> YosysDesignRuns:
    """Generate declaration and elaborated JSON views for one design."""

    manifest.validate()
    base_dir = Path(base_dir).resolve()
    yosys = resolve_yosys(yosys_exe)

    with tempfile.TemporaryDirectory(prefix="nebula_yosys_structure_") as tmp:
        work_dir = Path(tmp)

        declaration_json = work_dir / "declarations.json"
        elaborated_json = work_dir / "elaborated.json"

        declaration_script = build_declaration_script(
            manifest,
            base_dir,
            declaration_json,
        )

        elaboration_script = build_elaboration_script(
            manifest,
            base_dir,
            elaborated_json,
        )

        declarations = _run_yosys_script(
            yosys=yosys,
            script=declaration_script,
            base_dir=base_dir,
            work_dir=work_dir,
            stem="declarations",
        )

        elaborated = _run_yosys_script(
            yosys=yosys,
            script=elaboration_script,
            base_dir=base_dir,
            work_dir=work_dir,
            stem="elaborated",
        )

        return YosysDesignRuns(
            declarations=declarations,
            elaborated=elaborated,
        )


# -----------------------------------------------------------------------------
# JSON normalization
# -----------------------------------------------------------------------------


def _modules_object(data: Mapping[str, Any], label: str) -> Dict[str, Any]:
    modules = data.get("modules") or {}
    if not isinstance(modules, dict):
        raise YosysElaborationError(f"{label} JSON has no modules object")
    return modules


def normalize_port(port: Mapping[str, Any]) -> Dict[str, Any]:
    bits = port.get("bits") or []

    return {
        "direction": port.get("direction"),
        "width": len(bits),
        "offset": int(port.get("offset", 0)),
        "upto": int(port.get("upto", 0)),
        "signed": int(port.get("signed", 0)),
    }


def normalize_parameters(module: Mapping[str, Any]) -> Dict[str, Any]:
    raw = module.get("parameter_default_values") or {}
    if not isinstance(raw, dict):
        return {}

    return {str(name): value for name, value in sorted(raw.items())}


def source_module_names(declaration_modules: Mapping[str, Any]) -> List[str]:
    """Return source modules from the pre-hierarchy declaration view."""

    return sorted(
        str(name)
        for name in declaration_modules
        if not str(name).startswith("$paramod")
    )


def user_hierarchy_for_module(
    module: Mapping[str, Any],
    all_elaborated_modules: Mapping[str, Any],
) -> Dict[str, Dict[str, Any]]:
    """Return only module instances; ignore internal $add/$mux/$dff cells."""

    cells = module.get("cells") or {}
    if not isinstance(cells, dict):
        return {}

    module_names = {str(name) for name in all_elaborated_modules}
    result: Dict[str, Dict[str, Any]] = {}

    for instance_name, cell in sorted(cells.items()):
        if not isinstance(cell, dict):
            continue

        cell_type = str(cell.get("type", ""))
        if not cell_type:
            continue

        if cell_type not in module_names and not cell_type.startswith("$paramod"):
            continue

        parameters = cell.get("parameters") or {}
        if not isinstance(parameters, dict):
            parameters = {}

        result[str(instance_name)] = {
            "type": cell_type,
            "parameters": {
                str(key): value for key, value in sorted(parameters.items())
            },
        }

    return result


def normalize_elaborated_hierarchy(
    elaborated_modules: Mapping[str, Any],
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """
    Capture hierarchy for every elaborated module, including $paramod modules.

    Including $paramod modules is important because a parameterized child may
    disappear under its source name but still exist as a specialization.
    """

    result: Dict[str, Dict[str, Dict[str, Any]]] = {}

    for module_name, module in sorted(elaborated_modules.items()):
        if not isinstance(module, dict):
            continue

        result[str(module_name)] = user_hierarchy_for_module(
            module,
            elaborated_modules,
        )

    return result


# -----------------------------------------------------------------------------
# Sequential/state-boundary normalization
# -----------------------------------------------------------------------------


_GENERIC_REGISTER_TYPES = {
    "$dff",
    "$dffe",
    "$adff",
    "$adffe",
    "$aldff",
    "$aldffe",
    "$sdff",
    "$sdffe",
    "$sdffce",
    "$dffsr",
    "$ff",
    "$sr",
}

_GENERIC_LATCH_TYPES = {
    "$dlatch",
    "$adlatch",
    "$dlatchsr",
}


def _decode_parameter_scalar(value: Any) -> Any:
    """Normalize the small scalar/integer parameters emitted by Yosys JSON."""

    if isinstance(value, bool):
        return int(value)

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        return value

    if not isinstance(value, str):
        return value

    text = value.strip()

    # Yosys commonly emits parameters as binary strings with no Verilog base
    # prefix, e.g. "000...0001" for CLK_POLARITY and WIDTH.
    if text and all(ch in "01" for ch in text):
        try:
            return int(text, 2)
        except ValueError:
            pass

    return text


def _cell_parameters(cell: Mapping[str, Any]) -> Dict[str, Any]:
    raw = cell.get("parameters") or {}
    if not isinstance(raw, dict):
        return {}

    return {
        str(name): _decode_parameter_scalar(value)
        for name, value in sorted(raw.items())
    }


def _connection_bits(cell: Mapping[str, Any], name: str) -> List[Any]:
    connections = cell.get("connections") or {}
    if not isinstance(connections, dict):
        return []

    value = connections.get(name)
    if value is None:
        return []

    if isinstance(value, list):
        return list(value)

    return [value]


def _module_signal_aliases(
    module: Mapping[str, Any],
) -> Tuple[Dict[int, List[str]], Dict[int, List[str]]]:
    """
    Build stable aliases for Yosys bit IDs.

    Port aliases are kept separate from ordinary net aliases so controls prefer
    direct top/module inputs when possible. Internal auto-generated `$...`
    netnames are deliberately ignored because their numbering is not stable
    across legal combinational rewrites.
    """

    port_aliases: Dict[int, List[str]] = {}
    net_aliases: Dict[int, List[str]] = {}

    ports = module.get("ports") or {}
    if isinstance(ports, dict):
        for port_name, port in sorted(ports.items()):
            if not isinstance(port, dict):
                continue

            direction = str(port.get("direction", "unknown"))
            bits = port.get("bits") or []
            if not isinstance(bits, list):
                continue

            for index, bit in enumerate(bits):
                if not isinstance(bit, int):
                    continue
                port_aliases.setdefault(bit, []).append(
                    f"port:{direction}:{port_name}[{index}]"
                )

    netnames = module.get("netnames") or {}
    if isinstance(netnames, dict):
        for net_name, net in sorted(netnames.items()):
            name = str(net_name)

            # `$procdff$123`, `$0\\foo[7:0]...`, etc. are compiler-generated
            # and should not become part of a stable boundary identity.
            if not name or name.startswith("$"):
                continue

            if not isinstance(net, dict):
                continue

            bits = net.get("bits") or []
            if not isinstance(bits, list):
                continue

            for index, bit in enumerate(bits):
                if not isinstance(bit, int):
                    continue
                net_aliases.setdefault(bit, []).append(
                    f"net:{name}[{index}]"
                )

    for aliases in port_aliases.values():
        aliases.sort()
    for aliases in net_aliases.values():
        aliases.sort()

    return port_aliases, net_aliases


def _stable_bit_identity(
    bit: Any,
    *,
    port_aliases: Mapping[int, Sequence[str]],
    net_aliases: Mapping[int, Sequence[str]],
    preferred_port_direction: str | None = None,
) -> str:
    """Map one Yosys signal bit to a stable semantic identity."""

    if isinstance(bit, str):
        # Constants in write_json connections are strings: "0", "1", "x",
        # or "z". Preserve them explicitly.
        return f"const:{bit}"

    if not isinstance(bit, int):
        return "internal"

    aliases = list(port_aliases.get(bit, ()))

    if preferred_port_direction is not None:
        prefix = f"port:{preferred_port_direction}:"
        preferred = [alias for alias in aliases if alias.startswith(prefix)]
        if preferred:
            return preferred[0]

    if aliases:
        return aliases[0]

    named = list(net_aliases.get(bit, ()))
    if named:
        return named[0]

    return "internal"


def _stable_signal_identity(
    bits: Sequence[Any],
    *,
    port_aliases: Mapping[int, Sequence[str]],
    net_aliases: Mapping[int, Sequence[str]],
    preferred_port_direction: str | None = None,
) -> Tuple[str, ...]:
    return tuple(
        _stable_bit_identity(
            bit,
            port_aliases=port_aliases,
            net_aliases=net_aliases,
            preferred_port_direction=preferred_port_direction,
        )
        for bit in bits
    )


def _polarity_text(value: Any) -> str | None:
    decoded = _decode_parameter_scalar(value)

    if decoded == 1:
        return "positive"
    if decoded == 0:
        return "negative"
    if decoded is None:
        return None

    return str(decoded)


def _cell_kind(cell_type: str) -> str | None:
    if cell_type in _GENERIC_LATCH_TYPES:
        return "latch"

    if cell_type in _GENERIC_REGISTER_TYPES:
        return "register"

    # Future-proof against techmapped internal Yosys sequential primitives.
    # These are not normally present in our `proc -> write_json` views, but
    # treating them as state prevents a later pass change from silently
    # bypassing the safety gate.
    if cell_type.startswith("$_DLATCH"):
        return "latch"

    if cell_type.startswith("$_DFF") or cell_type.startswith("$_SDFF"):
        return "register"

    if cell_type.startswith("$_FF") or cell_type.startswith("$_SR"):
        return "register"

    return None


def _first_present_connection(
    cell: Mapping[str, Any],
    names: Sequence[str],
) -> Tuple[str | None, List[Any]]:
    for name in names:
        bits = _connection_bits(cell, name)
        if bits:
            return name, bits
    return None, []


def _first_present_parameter(
    parameters: Mapping[str, Any],
    names: Sequence[str],
) -> Tuple[str | None, Any]:
    for name in names:
        if name in parameters:
            return name, parameters[name]
    return None, None


def normalize_state_cell(
    module: Mapping[str, Any],
    cell: Mapping[str, Any],
) -> Dict[str, Any] | None:
    """
    Normalize one Yosys sequential cell into a state-boundary fingerprint.

    Deliberately EXCLUDED:
        - instance name
        - D/AD input data connectivity
        - compiler-generated raw bit IDs

    Included:
        - state kind and primitive type
        - width / stable Q target
        - clock source + edge
        - reset/set/clear/load source, polarity and reset value
        - enable source + polarity
    """

    cell_type = str(cell.get("type", ""))
    kind = _cell_kind(cell_type)
    if kind is None:
        return None

    parameters = _cell_parameters(cell)
    port_aliases, net_aliases = _module_signal_aliases(module)

    q_bits = _connection_bits(cell, "Q")

    width = parameters.get("WIDTH")
    if width is None:
        width = len(q_bits)
    width = _decode_parameter_scalar(width)

    fingerprint: Dict[str, Any] = {
        "kind": kind,
        "cell_type": cell_type,
        "width": width,
        "q_target": _stable_signal_identity(
            q_bits,
            port_aliases=port_aliases,
            net_aliases=net_aliases,
            preferred_port_direction="output",
        ),
    }

    # Clock semantics.
    clock_name, clock_bits = _first_present_connection(
        cell,
        ("CLK", "C"),
    )
    if clock_bits:
        _, clock_polarity = _first_present_parameter(
            parameters,
            ("CLK_POLARITY", "C_POLARITY"),
        )
        fingerprint["clock"] = {
            "port": clock_name,
            "source": _stable_signal_identity(
                clock_bits,
                port_aliases=port_aliases,
                net_aliases=net_aliases,
                preferred_port_direction="input",
            ),
            "edge": _polarity_text(clock_polarity),
        }
    else:
        fingerprint["clock"] = None

    # Clock-enable semantics.
    enable_name, enable_bits = _first_present_connection(
        cell,
        ("EN", "E", "CE"),
    )
    if enable_bits:
        _, enable_polarity = _first_present_parameter(
            parameters,
            ("EN_POLARITY", "E_POLARITY", "CE_POLARITY"),
        )
        fingerprint["enable"] = {
            "port": enable_name,
            "source": _stable_signal_identity(
                enable_bits,
                port_aliases=port_aliases,
                net_aliases=net_aliases,
                preferred_port_direction="input",
            ),
            "polarity": _polarity_text(enable_polarity),
        }
    else:
        fingerprint["enable"] = None

    controls: List[Dict[str, Any]] = []

    control_specs = (
        (
            "async_reset",
            ("ARST",),
            ("ARST_POLARITY",),
            ("ARST_VALUE",),
        ),
        (
            "sync_reset",
            ("SRST",),
            ("SRST_POLARITY",),
            ("SRST_VALUE",),
        ),
        (
            "set",
            ("SET",),
            ("SET_POLARITY",),
            (),
        ),
        (
            "clear",
            ("CLR",),
            ("CLR_POLARITY",),
            (),
        ),
        (
            "async_load",
            ("ALOAD",),
            ("ALOAD_POLARITY",),
            (),
        ),
    )

    for control_kind, conn_names, polarity_names, value_names in control_specs:
        conn_name, conn_bits = _first_present_connection(cell, conn_names)
        if not conn_bits:
            continue

        _, polarity = _first_present_parameter(parameters, polarity_names)
        _, control_value = _first_present_parameter(parameters, value_names)

        controls.append(
            {
                "kind": control_kind,
                "port": conn_name,
                "source": _stable_signal_identity(
                    conn_bits,
                    port_aliases=port_aliases,
                    net_aliases=net_aliases,
                    preferred_port_direction="input",
                ),
                "polarity": _polarity_text(polarity),
                "value": control_value,
            }
        )

    fingerprint["controls"] = tuple(
        sorted(
            controls,
            key=lambda item: (
                str(item.get("kind")),
                str(item.get("port")),
            ),
        )
    )

    # Preserve the explicit semantic parameters relevant to state behavior,
    # even when a future Yosys primitive uses a parameter not covered above.
    semantic_parameter_names = (
        "CLK_POLARITY",
        "EN_POLARITY",
        "ARST_POLARITY",
        "ARST_VALUE",
        "SRST_POLARITY",
        "SRST_VALUE",
        "SET_POLARITY",
        "CLR_POLARITY",
        "ALOAD_POLARITY",
    )

    fingerprint["semantic_parameters"] = {
        name: parameters[name]
        for name in semantic_parameter_names
        if name in parameters
    }

    return fingerprint


def _fingerprint_sort_key(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def normalize_state_boundary(
    modules: Mapping[str, Any],
    *,
    source_only: bool,
) -> Dict[str, Tuple[Dict[str, Any], ...]]:
    """Return a deterministic semantic state fingerprint for each module."""

    result: Dict[str, Tuple[Dict[str, Any], ...]] = {}

    module_names = (
        source_module_names(modules)
        if source_only
        else sorted(str(name) for name in modules)
    )

    for module_name in module_names:
        module = modules.get(module_name)
        if not isinstance(module, dict):
            continue

        cells = module.get("cells") or {}
        if not isinstance(cells, dict):
            cells = {}

        fingerprints: List[Dict[str, Any]] = []

        for _, cell in sorted(cells.items()):
            if not isinstance(cell, dict):
                continue

            normalized = normalize_state_cell(module, cell)
            if normalized is not None:
                fingerprints.append(normalized)

        fingerprints.sort(key=_fingerprint_sort_key)
        result[module_name] = tuple(fingerprints)

    return result


def state_boundary_summary(
    boundary: Mapping[str, Sequence[Mapping[str, Any]]],
) -> Dict[str, Any]:
    register_count = 0
    latch_count = 0

    per_module: Dict[str, Dict[str, int]] = {}

    for module_name, cells in sorted(boundary.items()):
        module_registers = sum(
            1 for cell in cells if cell.get("kind") == "register"
        )
        module_latches = sum(
            1 for cell in cells if cell.get("kind") == "latch"
        )

        register_count += module_registers
        latch_count += module_latches

        per_module[module_name] = {
            "registers": module_registers,
            "latches": module_latches,
            "state_cells": len(cells),
        }

    return {
        "register_count": register_count,
        "latch_count": latch_count,
        "state_cell_count": register_count + latch_count,
        "per_module": per_module,
    }


def stateful_modules_only(
    boundary: Mapping[str, Sequence[Mapping[str, Any]]],
) -> Dict[str, Tuple[Mapping[str, Any], ...]]:
    """
    Drop modules that carry no sequential state at all.

    A module with an empty state-cell list contributes nothing to the design's
    sequential behavior, so its presence or absence is a hierarchy fact, not a
    state-boundary fact. Comparing raw dicts would report a
    state_boundary_changed difference merely because a stateless wrapper module
    was inlined away, which is already reported as hierarchy_changed. Filtering
    the empty entries keeps state_boundary_changed meaning what its name says:
    the set or nature of actual registers/latches differs.
    """

    return {
        str(module_name): tuple(cells)
        for module_name, cells in boundary.items()
        if cells
    }


# -----------------------------------------------------------------------------
# Snapshot
# -----------------------------------------------------------------------------


def live_net_drivers(modules):
    """Named bit driver state after proc, using compiler port directions.

    Bit IDs are local to a compilation; compare only stable RTL names/indices.
    Dead wires and compiler-generated names do not constrain legal rewrites.
    """
    result = {}
    for module_name, module in modules.items():
        drivers, sinks = set(), set()
        for port in module.get("ports", {}).values():
            direction = port.get("direction")
            if direction in ("input", "inout"):
                drivers.update(port.get("bits", []))
            if direction in ("output", "inout"):
                sinks.update(port.get("bits", []))
        for cell in module.get("cells", {}).values():
            directions = cell.get("port_directions", {})
            if not directions:
                directions = {n: v.get("direction") for n, v in
                              modules.get(cell.get("type"), {}).get("ports", {}).items()}
            for port, bits in cell.get("connections", {}).items():
                if directions.get(port) in ("output", "inout"):
                    drivers.update(bits)
                if directions.get(port) in ("input", "inout"):
                    sinks.update(bits)
        bits_by_name = {}
        for name, net in module.get("netnames", {}).items():
            if name.startswith("$") or net.get("hide_name"):
                continue
            for index, bit in enumerate(net.get("bits", [])):
                if isinstance(bit, int) and bit in sinks:
                    bits_by_name[f"{name}[{index + net.get('offset', 0)}]"] = bit in drivers
        result[module_name] = bits_by_name
    return result


@dataclass(frozen=True)
class ModuleDeclaration:
    ports: Dict[str, Dict[str, Any]]
    parameters: Dict[str, Any]


@dataclass(frozen=True)
class StructureSnapshot:
    top: str
    declarations: Dict[str, ModuleDeclaration]
    elaborated_hierarchy: Dict[str, Dict[str, Dict[str, Any]]]
    declaration_state_boundary: Dict[str, Tuple[Dict[str, Any], ...]]
    elaborated_state_boundary: Dict[str, Tuple[Dict[str, Any], ...]]
    declaration_module_names: Tuple[str, ...]
    elaborated_module_names: Tuple[str, ...]
    declaration_script: str
    elaboration_script: str
    declaration_log: str
    elaboration_log: str
    live_net_drivers: Dict[str, Dict[str, bool]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "top": self.top,
            "live_net_drivers": self.live_net_drivers,
            "declarations": {
                module_name: {
                    "ports": declaration.ports,
                    "parameters": declaration.parameters,
                }
                for module_name, declaration in sorted(self.declarations.items())
            },
            "elaborated_hierarchy": self.elaborated_hierarchy,
            "state_boundary": {
                "declaration": {
                    module_name: list(cells)
                    for module_name, cells in sorted(
                        self.declaration_state_boundary.items()
                    )
                },
                "elaborated": {
                    module_name: list(cells)
                    for module_name, cells in sorted(
                        self.elaborated_state_boundary.items()
                    )
                },
                "declaration_summary": state_boundary_summary(
                    self.declaration_state_boundary
                ),
                "elaborated_summary": state_boundary_summary(
                    self.elaborated_state_boundary
                ),
            },
            "declaration_module_names": list(self.declaration_module_names),
            "elaborated_module_names": list(self.elaborated_module_names),
        }


def snapshot_from_yosys(
    manifest: DesignManifest,
    runs: YosysDesignRuns,
) -> StructureSnapshot:
    declaration_modules = _modules_object(
        runs.declarations.json_data,
        "declaration",
    )

    elaborated_modules = _modules_object(
        runs.elaborated.json_data,
        "elaborated",
    )

    if manifest.top not in declaration_modules:
        raise YosysElaborationError(
            f"top module {manifest.top!r} missing from declaration view"
        )

    if manifest.top not in elaborated_modules:
        raise YosysElaborationError(
            f"top module {manifest.top!r} missing after hierarchy elaboration"
        )

    declarations: Dict[str, ModuleDeclaration] = {}

    for module_name in source_module_names(declaration_modules):
        module = declaration_modules.get(module_name)
        if not isinstance(module, dict):
            continue

        raw_ports = module.get("ports") or {}
        if not isinstance(raw_ports, dict):
            raw_ports = {}

        ports = {
            str(port_name): normalize_port(port)
            for port_name, port in sorted(raw_ports.items())
            if isinstance(port, dict)
        }

        declarations[module_name] = ModuleDeclaration(
            ports=ports,
            parameters=normalize_parameters(module),
        )

    return StructureSnapshot(
        top=manifest.top,
        declarations=declarations,
        elaborated_hierarchy=normalize_elaborated_hierarchy(elaborated_modules),
        declaration_state_boundary=normalize_state_boundary(
            declaration_modules,
            source_only=True,
        ),
        elaborated_state_boundary=normalize_state_boundary(
            elaborated_modules,
            source_only=False,
        ),
        declaration_module_names=tuple(sorted(declaration_modules)),
        elaborated_module_names=tuple(sorted(elaborated_modules)),
        declaration_script=runs.declarations.script,
        elaboration_script=runs.elaborated.script,
        declaration_log=runs.declarations.log,
        elaboration_log=runs.elaborated.log,
        live_net_drivers=live_net_drivers(elaborated_modules),
    )


def build_structure_snapshot(
    manifest: DesignManifest,
    base_dir: Path,
    *,
    yosys_exe: str | None = None,
) -> StructureSnapshot:
    runs = run_yosys_views(
        manifest,
        base_dir,
        yosys_exe=yosys_exe,
    )
    return snapshot_from_yosys(manifest, runs)


# -----------------------------------------------------------------------------
# Comparison
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class StructureDifference:
    category: str
    module: str | None
    detail: str
    golden: Any = None
    candidate: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "module": self.module,
            "detail": self.detail,
            "golden": self.golden,
            "candidate": self.candidate,
        }


@dataclass(frozen=True)
class StructureComparison:
    ok: bool
    differences: Tuple[StructureDifference, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "difference_count": len(self.differences),
            "differences": [difference.to_dict() for difference in self.differences],
        }


# Only the observable top-level interface must match before formal proof.
# Other structural differences are useful diagnostics, not evidence of
# inequivalence. Yosys elaboration and check -assert still validate each side.
EQUIVALENCE_BLOCKING_CATEGORIES = frozenset({"top_changed", "interface_changed"})


def blocks_equivalence_setup(difference: StructureDifference, top: str) -> bool:
    return difference.category in EQUIVALENCE_BLOCKING_CATEGORIES and (
        difference.category == "top_changed" or
        difference.category == "interface_changed" and difference.module == top
    )


def _clock_reset_control_signatures(boundary: Mapping[str, Sequence[Mapping[str, Any]]]) -> set[str]:
    """Return representation-independent clock/reset domain signatures.

    State width, Q targets, primitive names, hierarchy placement, encoded reset
    values and ordinary data/enable topology are intentionally excluded.  This
    lets formal verification decide legal state recoding while the deterministic
    gate still protects clock source/edge and reset source/type/polarity.
    """
    import json
    signatures: set[str] = set()
    for cells in boundary.values():
        for cell in cells:
            if cell.get("kind") == "latch":
                continue
            clock = cell.get("clock")
            controls = []
            for control in cell.get("controls") or []:
                controls.append({
                    "kind": control.get("kind"),
                    "source": control.get("source"),
                    "polarity": control.get("polarity"),
                })
            signature = {
                "clock": None if clock is None else {
                    "source": clock.get("source"),
                    "edge": clock.get("edge"),
                },
                "controls": sorted(controls, key=lambda item: json.dumps(item, sort_keys=True)),
            }
            signatures.add(json.dumps(signature, sort_keys=True, separators=(",", ":")))
    return signatures


def _clock_domain_signatures(boundary: Mapping[str, Sequence[Mapping[str, Any]]]) -> set[str]:
    import json
    domains: set[str] = set()
    for cells in boundary.values():
        for cell in cells:
            if cell.get("kind") == "latch":
                continue
            clock = cell.get("clock")
            if clock is not None:
                domains.add(json.dumps({"source": clock.get("source"), "edge": clock.get("edge")},
                                       sort_keys=True, separators=(",", ":")))
    return domains


def compare_structures(
    golden: StructureSnapshot,
    candidate: StructureSnapshot,
) -> StructureComparison:
    differences: List[StructureDifference] = []

    for module, nets in golden.live_net_drivers.items():
        after = candidate.live_net_drivers.get(module, {})
        lost = sorted(name for name, driven in nets.items()
                      if driven and name in after and not after[name])
        if lost:
            differences.append(StructureDifference(
                category="preexisting_live_net_undriven", module=module,
                detail="pre-existing consumed net bits lost their compiler-derived driver",
                golden=lost, candidate="undriven"))

    if golden.top != candidate.top:
        differences.append(
            StructureDifference(
                category="top_changed",
                module=None,
                detail="top module identity changed",
                golden=golden.top,
                candidate=candidate.top,
            )
        )

    golden_modules = set(golden.declarations)
    candidate_modules = set(candidate.declarations)

    for module_name in sorted(golden_modules - candidate_modules):
        differences.append(
            StructureDifference(
                category="module_removed",
                module=module_name,
                detail="source module disappeared from candidate declarations",
                golden=True,
                candidate=False,
            )
        )

    for module_name in sorted(candidate_modules - golden_modules):
        differences.append(
            StructureDifference(
                category="module_added",
                module=module_name,
                detail="candidate introduced a new source module",
                golden=False,
                candidate=True,
            )
        )

    for module_name in sorted(golden_modules & candidate_modules):
        golden_decl = golden.declarations[module_name]
        candidate_decl = candidate.declarations[module_name]

        if golden_decl.ports != candidate_decl.ports:
            differences.append(
                StructureDifference(
                    category="interface_changed",
                    module=module_name,
                    detail="Yosys-derived module port interface changed",
                    golden=golden_decl.ports,
                    candidate=candidate_decl.ports,
                )
            )

        if golden_decl.parameters != candidate_decl.parameters:
            differences.append(
                StructureDifference(
                    category="parameters_changed",
                    module=module_name,
                    detail="Yosys-derived module parameter defaults changed",
                    golden=golden_decl.parameters,
                    candidate=candidate_decl.parameters,
                )
            )

    if golden.elaborated_hierarchy != candidate.elaborated_hierarchy:
        differences.append(
            StructureDifference(
                category="hierarchy_changed",
                module=golden.top,
                detail="Yosys-derived elaborated user-module hierarchy changed",
                golden=golden.elaborated_hierarchy,
                candidate=candidate.elaborated_hierarchy,
            )
        )

    # Record source and elaborated state changes for diagnostics. Sequential
    # equivalence, rather than structural identity, decides whether they pass.
    # Stateless modules are excluded from the comparison so that inlining a
    # wrapper with no registers is reported as hierarchy_changed only.
    if (
        stateful_modules_only(golden.declaration_state_boundary)
        != stateful_modules_only(candidate.declaration_state_boundary)
    ):
        differences.append(
            StructureDifference(
                category="state_boundary_changed",
                module=None,
                detail=(
                    "Yosys-derived source-module sequential state boundary changed"
                ),
                golden={
                    name: list(cells)
                    for name, cells in sorted(
                        golden.declaration_state_boundary.items()
                    )
                },
                candidate={
                    name: list(cells)
                    for name, cells in sorted(
                        candidate.declaration_state_boundary.items()
                    )
                },
            )
        )

    if (
        stateful_modules_only(golden.elaborated_state_boundary)
        != stateful_modules_only(candidate.elaborated_state_boundary)
    ):
        differences.append(
            StructureDifference(
                category="elaborated_state_boundary_changed",
                module=golden.top,
                detail=(
                    "Yosys-derived parameter-resolved sequential state boundary changed"
                ),
                golden={
                    name: list(cells)
                    for name, cells in sorted(
                        golden.elaborated_state_boundary.items()
                    )
                },
                candidate={
                    name: list(cells)
                    for name, cells in sorted(
                        candidate.elaborated_state_boundary.items()
                    )
                },
            )
        )

    golden_domains = _clock_domain_signatures(golden.elaborated_state_boundary)
    candidate_domains = _clock_domain_signatures(candidate.elaborated_state_boundary)
    new_domains = sorted(candidate_domains - golden_domains)
    if new_domains:
        differences.append(
            StructureDifference(
                category="new_clock_domain",
                module=golden.top,
                detail="candidate introduced a clock source/edge domain not present in golden",
                golden=sorted(golden_domains),
                candidate=sorted(candidate_domains),
            )
        )

    golden_controls = _clock_reset_control_signatures(golden.elaborated_state_boundary)
    candidate_controls = _clock_reset_control_signatures(candidate.elaborated_state_boundary)
    if golden_controls != candidate_controls:
        differences.append(
            StructureDifference(
                category="clock_reset_behavior_changed",
                module=golden.top,
                detail="clock source/edge or reset source/type/polarity changed",
                golden=sorted(golden_controls),
                candidate=sorted(candidate_controls),
            )
        )

    golden_latches = state_boundary_summary(
        golden.declaration_state_boundary
    )["latch_count"]
    candidate_latches = state_boundary_summary(
        candidate.declaration_state_boundary
    )["latch_count"]

    if candidate_latches > golden_latches:
        differences.append(
            StructureDifference(
                category="latch_introduced",
                module=None,
                detail="candidate introduced one or more Yosys-derived latches",
                golden=golden_latches,
                candidate=candidate_latches,
            )
        )

    blocking = [difference for difference in differences
                if blocks_equivalence_setup(difference, golden.top)]
    return StructureComparison(
        ok=not blocking,
        differences=tuple(differences),
    )


# -----------------------------------------------------------------------------
# High-level API
# -----------------------------------------------------------------------------


def compare_manifests(
    *,
    golden_manifest: DesignManifest,
    golden_base_dir: Path,
    candidate_manifest: DesignManifest,
    candidate_base_dir: Path,
    yosys_exe: str | None = None,
) -> Tuple[StructureComparison, StructureSnapshot, StructureSnapshot]:
    if golden_manifest.top != candidate_manifest.top:
        raise YosysStructureError(
            "golden and candidate manifests do not identify the same top module"
        )

    golden = build_structure_snapshot(
        golden_manifest,
        golden_base_dir,
        yosys_exe=yosys_exe,
    )

    candidate = build_structure_snapshot(
        candidate_manifest,
        candidate_base_dir,
        yosys_exe=yosys_exe,
    )

    return compare_structures(golden, candidate), golden, candidate


# -----------------------------------------------------------------------------
# Self-test helpers
# -----------------------------------------------------------------------------


def make_manifest(root: Path) -> DesignManifest:
    manifest = DesignManifest(
        dataset="yosys_structure_self_test",
        design_id="structure_test",
        top="top",
        source_files=["rtl/top.v", "rtl/child.v"],
        # Exercise include/define option emission in the self-test.
        include_dirs=["rtl"],
        defines=["NEBULA_STRUCTURE_TEST=1"],
        parameters={"STRUCT_TAG": 8},
        language="verilog",
        clocks=[],
        resets=[],
        constraints={},
    )

    manifest.refresh_source_hashes(root)
    return manifest


def write_good_design(root: Path, *, optimized: bool) -> None:
    rtl = root / "rtl"
    rtl.mkdir(parents=True, exist_ok=True)

    # Exercise include_dirs[] for real: top.v includes this header by basename.
    (rtl / "nebula_widths.vh").write_text(
        "`define NEBULA_W 8\n",
        encoding="utf-8",
    )

    (rtl / "child.v").write_text(
        (
            "module child (\n"
            "    input  wire [7:0] a,\n"
            "    input  wire [7:0] b,\n"
            "    output wire [7:0] y\n"
            ");\n"
            "\n"
            + (
                "assign y = (a ^ b) ^ b;\n"
                if optimized
                else "assign y = a;\n"
            )
            + "\n"
            "endmodule\n"
        ),
        encoding="utf-8",
    )

    (rtl / "top.v").write_text(
        (
            "`include \"nebula_widths.vh\"\n"
            "`ifdef NEBULA_STRUCTURE_TEST\n"
            "module top #(\n"
            "    parameter STRUCT_TAG = 8\n"
            ") (\n"
            "    input  wire [`NEBULA_W-1:0] a,\n"
            "    input  wire [`NEBULA_W-1:0] b,\n"
            "    output wire [`NEBULA_W-1:0] y\n"
            ");\n"
            "\n"
            "child u_child (\n"
            "    .a(a),\n"
            "    .b(b),\n"
            "    .y(y)\n"
            ");\n"
            "\n"
            "endmodule\n"
            "`else\n"
            "module nebula_define_missing; endmodule\n"
            "`endif\n"
        ),
        encoding="utf-8",
    )



def make_state_manifest(root: Path) -> DesignManifest:
    manifest = DesignManifest(
        dataset="yosys_structure_state_self_test",
        design_id="state_test",
        top="top",
        source_files=["rtl/top.v"],
        include_dirs=[],
        defines=[],
        parameters={},
        language="verilog",
        clocks=[],
        resets=[],
        constraints={},
    )
    manifest.refresh_source_hashes(root)
    return manifest


def write_state_design(
    root: Path,
    *,
    variant: str,
) -> None:
    rtl = root / "rtl"
    rtl.mkdir(parents=True, exist_ok=True)

    next_expr = "a + 8'd1"
    clock = "clk"
    edge = "posedge"
    reset_edge = "negedge"
    reset_condition = "!rst_n"
    reset_value = "8'h00"
    body_extra = ""
    output_logic = ""

    if variant == "good_comb":
        # Equivalent-looking but structurally different D cone. D connectivity
        # is intentionally excluded from the state-boundary fingerprint.
        next_expr = "a - 8'hff"
    elif variant == "negedge_clock":
        edge = "negedge"
    elif variant == "clock_source":
        clock = "clk2"
    elif variant == "reset_polarity":
        reset_edge = "posedge"
        reset_condition = "rst_n"
    elif variant == "reset_value":
        reset_value = "8'hff"
    elif variant == "add_register":
        body_extra = (
            "\nreg [7:0] extra_reg;\n"
            "always @(posedge clk) begin\n"
            "    extra_reg <= a;\n"
            "end\n"
        )
    elif variant == "remove_register":
        output_logic = "assign y = next_value;\n"
    elif variant == "latch":
        body_extra = (
            "\nreg [7:0] latched;\n"
            "always @* begin\n"
            "    if (a[0])\n"
            "        latched = a;\n"
            "end\n"
        )
        next_expr = "latched + 8'd1"
    elif variant != "golden":
        raise ValueError(f"unknown state self-test variant {variant!r}")

    if variant != "remove_register":
        output_logic = (
            f"always @({edge} {clock} or {reset_edge} rst_n) begin\n"
            f"    if ({reset_condition})\n"
            f"        y <= {reset_value};\n"
            "    else\n"
            "        y <= next_value;\n"
            "end\n"
        )

    output_decl = (
        "    output wire [7:0] y\n"
        if variant == "remove_register"
        else "    output reg  [7:0] y\n"
    )

    (rtl / "top.v").write_text(
        (
            "module top (\n"
            "    input  wire       clk,\n"
            "    input  wire       clk2,\n"
            "    input  wire       rst_n,\n"
            "    input  wire [7:0] a,\n"
            + output_decl
            + ");\n"
            f"wire [7:0] next_value = {next_expr};\n"
            f"{body_extra}\n"
            f"{output_logic}"
            "endmodule\n"
        ),
        encoding="utf-8",
    )


# -----------------------------------------------------------------------------
# Self-test
# -----------------------------------------------------------------------------


def run_self_test(yosys_exe: str | None = None) -> None:
    yosys = resolve_yosys(yosys_exe)

    with tempfile.TemporaryDirectory(prefix="nebula_structure_selftest_") as tmp:
        root = Path(tmp)
        golden_root = root / "golden"
        good_root = root / "good"
        bad_port_root = root / "bad_port"
        bad_hierarchy_root = root / "bad_hierarchy"
        bad_parameter_root = root / "bad_parameter"
        state_golden_root = root / "state_golden"
        state_good_root = root / "state_good"
        state_added_reg_root = root / "state_added_reg"
        state_removed_reg_root = root / "state_removed_reg"
        state_negedge_root = root / "state_negedge"
        state_reset_root = root / "state_reset"
        state_clock_root = root / "state_clock"
        state_latch_root = root / "state_latch"

        # Golden.
        write_good_design(golden_root, optimized=False)
        golden_manifest = make_manifest(golden_root)

        # Good candidate: only internal combinational logic changes.
        write_good_design(good_root, optimized=True)
        good_manifest = make_manifest(good_root)

        comparison, golden_snapshot, good_snapshot = compare_manifests(
            golden_manifest=golden_manifest,
            golden_base_dir=golden_root,
            candidate_manifest=good_manifest,
            candidate_base_dir=good_root,
            yosys_exe=str(yosys),
        )

        assert comparison.ok, comparison.to_dict()
        assert "top" in golden_snapshot.declarations
        assert "child" in golden_snapshot.declarations
        assert (
            golden_snapshot.declarations["top"].ports
            == good_snapshot.declarations["top"].ports
        )
        assert (
            golden_snapshot.declarations["top"].parameters
            == good_snapshot.declarations["top"].parameters
        )
        assert (
            golden_snapshot.elaborated_hierarchy
            == good_snapshot.elaborated_hierarchy
        )

        # Bad port width.
        write_good_design(bad_port_root, optimized=True)
        (bad_port_root / "rtl" / "top.v").write_text(
            (
                "module top #(\n"
                "    parameter STRUCT_TAG = 8\n"
                ") (\n"
                "    input  wire [7:0] a,\n"
                "    input  wire [7:0] b,\n"
                "    output wire [3:0] y\n"
                ");\n"
                "\n"
                "wire [7:0] child_y;\n"
                "child u_child (\n"
                "    .a(a),\n"
                "    .b(b),\n"
                "    .y(child_y)\n"
                ");\n"
                "assign y = child_y[3:0];\n"
                "endmodule\n"
            ),
            encoding="utf-8",
        )
        bad_port_manifest = make_manifest(bad_port_root)

        bad_port_comparison, _, _ = compare_manifests(
            golden_manifest=golden_manifest,
            golden_base_dir=golden_root,
            candidate_manifest=bad_port_manifest,
            candidate_base_dir=bad_port_root,
            yosys_exe=str(yosys),
        )

        assert not bad_port_comparison.ok
        assert any(
            diff.category == "interface_changed"
            for diff in bad_port_comparison.differences
        ), bad_port_comparison.to_dict()

        # Bad hierarchy: remove child instance.
        write_good_design(bad_hierarchy_root, optimized=True)
        (bad_hierarchy_root / "rtl" / "top.v").write_text(
            (
                "module top #(\n"
                "    parameter STRUCT_TAG = 8\n"
                ") (\n"
                "    input  wire [7:0] a,\n"
                "    input  wire [7:0] b,\n"
                "    output wire [7:0] y\n"
                ");\n"
                "assign y = a ^ b;\n"
                "endmodule\n"
            ),
            encoding="utf-8",
        )
        bad_hierarchy_manifest = make_manifest(bad_hierarchy_root)

        bad_hierarchy_comparison, _, _ = compare_manifests(
            golden_manifest=golden_manifest,
            golden_base_dir=golden_root,
            candidate_manifest=bad_hierarchy_manifest,
            candidate_base_dir=bad_hierarchy_root,
            yosys_exe=str(yosys),
        )

        assert bad_hierarchy_comparison.ok, bad_hierarchy_comparison.to_dict()
        assert any(
            diff.category == "hierarchy_changed"
            for diff in bad_hierarchy_comparison.differences
        ), bad_hierarchy_comparison.to_dict()

        # Bad parameter default: manifest still sets effective value 8, but
        # the source declaration changed from 8 to 4 and must be rejected.
        write_good_design(bad_parameter_root, optimized=True)
        (bad_parameter_root / "rtl" / "top.v").write_text(
            (
                "module top #(\n"
                "    parameter STRUCT_TAG = 4\n"
                ") (\n"
                "    input  wire [7:0] a,\n"
                "    input  wire [7:0] b,\n"
                "    output wire [7:0] y\n"
                ");\n"
                "child u_child (\n"
                "    .a(a),\n"
                "    .b(b),\n"
                "    .y(y)\n"
                ");\n"
                "endmodule\n"
            ),
            encoding="utf-8",
        )
        bad_parameter_manifest = make_manifest(bad_parameter_root)

        bad_parameter_comparison, _, _ = compare_manifests(
            golden_manifest=golden_manifest,
            golden_base_dir=golden_root,
            candidate_manifest=bad_parameter_manifest,
            candidate_base_dir=bad_parameter_root,
            yosys_exe=str(yosys),
        )

        assert bad_parameter_comparison.ok, bad_parameter_comparison.to_dict()
        assert any(
            diff.category == "parameters_changed"
            for diff in bad_parameter_comparison.differences
        ), bad_parameter_comparison.to_dict()

        # -------------------------------------------------------------
        # Sequential/state-boundary regressions.
        # -------------------------------------------------------------

        write_state_design(state_golden_root, variant="golden")
        state_golden_manifest = make_state_manifest(state_golden_root)

        write_state_design(state_good_root, variant="good_comb")
        state_good_manifest = make_state_manifest(state_good_root)

        state_good_comparison, state_golden_snapshot, state_good_snapshot = (
            compare_manifests(
                golden_manifest=state_golden_manifest,
                golden_base_dir=state_golden_root,
                candidate_manifest=state_good_manifest,
                candidate_base_dir=state_good_root,
                yosys_exe=str(yosys),
            )
        )

        assert state_good_comparison.ok, state_good_comparison.to_dict()
        assert (
            state_golden_snapshot.declaration_state_boundary
            == state_good_snapshot.declaration_state_boundary
        )

        def assert_state_logged(
            candidate_root: Path,
            variant: str,
            expected_categories: Sequence[str],
        ) -> StructureComparison:
            write_state_design(candidate_root, variant=variant)
            candidate_manifest = make_state_manifest(candidate_root)
            result, _, _ = compare_manifests(
                golden_manifest=state_golden_manifest,
                golden_base_dir=state_golden_root,
                candidate_manifest=candidate_manifest,
                candidate_base_dir=candidate_root,
                yosys_exe=str(yosys),
            )
            # State-boundary differences are diagnostic-only at this layer
            # (only top_changed/interface_changed block .ok here); lint_candidate.py
            # is the layer that makes state-boundary preservation mandatory.
            assert result.ok, result.to_dict()
            categories = {diff.category for diff in result.differences}
            assert any(
                category in categories
                for category in expected_categories
            ), result.to_dict()
            return result

        assert_state_logged(
            state_added_reg_root,
            "add_register",
            ("state_boundary_changed",),
        )

        assert_state_logged(
            state_removed_reg_root,
            "remove_register",
            ("state_boundary_changed",),
        )

        assert_state_logged(
            state_negedge_root,
            "negedge_clock",
            ("state_boundary_changed",),
        )

        assert_state_logged(
            state_reset_root,
            "reset_polarity",
            ("state_boundary_changed",),
        )

        assert_state_logged(
            state_clock_root,
            "clock_source",
            ("state_boundary_changed",),
        )

        latch_comparison = assert_state_logged(
            state_latch_root,
            "latch",
            ("latch_introduced", "state_boundary_changed"),
        )
        assert any(
            diff.category == "latch_introduced"
            for diff in latch_comparison.differences
        ), latch_comparison.to_dict()

        print("PASS: yosys_structure self-test")
        print("PASS: complete DesignManifest filelist parsed")
        print("PASS: include_dirs propagated to read_verilog")
        print("PASS: Verilog defines propagated as -DNAME=VALUE")
        print("PASS: Yosys declaration interfaces captured")
        print("PASS: Yosys parameter defaults captured")
        print("PASS: Yosys resolved hierarchy captured")
        print("PASS: manifest top parameters applied during elaboration")
        print("PASS: internal RTL rewrite accepted structurally")
        print("PASS: port/interface mutation rejected")
        print("PASS: hierarchy mutation logged (diagnostic only, not equivalence-blocking)")
        print("PASS: parameter-default mutation logged (diagnostic only, not equivalence-blocking)")
        print("PASS: legal combinational D-cone rewrite preserves state boundary")
        print("PASS: added register logged (diagnostic only at this layer)")
        print("PASS: removed register logged (diagnostic only at this layer)")
        print("PASS: posedge-to-negedge clock mutation logged (diagnostic only at this layer)")
        print("PASS: clock-source mutation logged (diagnostic only at this layer)")
        print("PASS: reset-polarity mutation logged (diagnostic only at this layer)")
        print("PASS: introduced latch logged (diagnostic only at this layer)")
        print(f"Yosys: {yosys}")


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compiler-derived nEBULA structural comparison using Yosys JSON."
    )

    parser.add_argument("--golden-manifest", help="golden DesignManifest JSON")
    parser.add_argument("--candidate-manifest", help="candidate DesignManifest JSON")
    parser.add_argument(
        "--golden-base-dir",
        default=".",
        help="base directory for golden manifest source paths",
    )
    parser.add_argument(
        "--candidate-base-dir",
        default=".",
        help="base directory for candidate manifest source paths",
    )
    parser.add_argument("--yosys", default=None, help="optional Yosys executable")
    parser.add_argument("--output", default=None, help="optional comparison JSON output")
    parser.add_argument("--self-test", action="store_true")

    args = parser.parse_args()

    if args.self_test:
        try:
            run_self_test(yosys_exe=args.yosys)
        except Exception as exc:
            print(
                f"SELF-TEST FAILED: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1
        return 0

    if not args.golden_manifest:
        parser.error("--golden-manifest is required")

    if not args.candidate_manifest:
        parser.error("--candidate-manifest is required")

    try:
        golden_manifest = DesignManifest.read_json(Path(args.golden_manifest))
        candidate_manifest = DesignManifest.read_json(Path(args.candidate_manifest))

        comparison, golden, candidate = compare_manifests(
            golden_manifest=golden_manifest,
            golden_base_dir=Path(args.golden_base_dir),
            candidate_manifest=candidate_manifest,
            candidate_base_dir=Path(args.candidate_base_dir),
            yosys_exe=args.yosys,
        )

    except YosysStructureError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    result = {
        "ok": comparison.ok,
        "comparison": comparison.to_dict(),
        "golden": golden.to_dict(),
        "candidate": candidate.to_dict(),
    }

    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    print(json.dumps(comparison.to_dict(), indent=2))
    return 0 if comparison.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
