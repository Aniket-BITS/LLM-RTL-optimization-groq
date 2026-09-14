#!/usr/bin/env python3
"""
formal_backend.py

Backend contract and registry for nEBULA formal verification.

Purpose
-------
This file separates "which formal backend should handle this SECBundle?"
from the proof engine that actually runs Yosys / SMT / commercial tools.

It is intentionally a small policy/dispatch layer.

Current implementation state
----------------------------
Implemented:
    - yosys

Known/planned:
    - yosys_smtbmc
    - jaspergold
    - formality

Supported verification modes
----------------------------
    - combinational_cec
    - sequential_equivalence
    - backend_unsupported

The special mode backend_unsupported is a design-policy outcome, not a proof
backend. It always fails closed without executing any backend.

This module does NOT execute a proof. verify_rtl.py remains the proof engine.
The next integration step will make verify_rtl.py consult this registry instead
of hard-coding backend selection.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple


try:
    from equivalence_terms import EquivalenceVerdict
except ImportError:
    from timing_automation.equivalence_terms import EquivalenceVerdict

# =============================================================================
# Constants
# =============================================================================

MODE_COMBINATIONAL_CEC = "combinational_cec"
MODE_SEQUENTIAL_EQUIVALENCE = "sequential_equivalence"
MODE_BACKEND_UNSUPPORTED = "backend_unsupported"

SUPPORTED_MODES = {
    MODE_COMBINATIONAL_CEC,
    MODE_SEQUENTIAL_EQUIVALENCE,
    MODE_BACKEND_UNSUPPORTED,
}

STATUS_READY = "ready"
STATUS_BACKEND_UNAVAILABLE = "backend_unavailable"
STATUS_MODE_UNSUPPORTED = "mode_unsupported"
STATUS_DESIGN_UNSUPPORTED = "design_unsupported"
STATUS_UNKNOWN_BACKEND = "unknown_backend"

KNOWN_DECISION_STATUSES = {
    STATUS_READY,
    STATUS_BACKEND_UNAVAILABLE,
    STATUS_MODE_UNSUPPORTED,
    STATUS_DESIGN_UNSUPPORTED,
    STATUS_UNKNOWN_BACKEND,
}

_BACKEND_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


# =============================================================================
# Exceptions
# =============================================================================


class FormalBackendError(RuntimeError):
    """Base backend-registry error."""


class BackendRegistrationError(FormalBackendError):
    """Raised when a backend specification is invalid or duplicated."""


class BackendResolutionError(FormalBackendError):
    """Raised when backend-selection inputs are malformed."""


# =============================================================================
# Backend specification
# =============================================================================


@dataclass(frozen=True)
class BackendSpec:
    """
    Declarative description of one formal backend.

    implemented:
        True means nEBULA currently contains an execution adapter for this
        backend. It does NOT mean the external executable is installed on the
        current machine; tool availability is checked by the execution layer.

    supported_modes:
        Modes this backend adapter is intended to support.
    """

    backend_id: str
    display_name: str
    supported_modes: Tuple[str, ...]
    implemented: bool
    aliases: Tuple[str, ...] = ()
    notes: str = ""

    def validate(self) -> None:
        if not isinstance(self.backend_id, str) or not _BACKEND_ID_RE.fullmatch(
            self.backend_id
        ):
            raise BackendRegistrationError(
                "backend_id must match [a-z][a-z0-9_]*; "
                f"got {self.backend_id!r}"
            )

        if not isinstance(self.display_name, str) or not self.display_name.strip():
            raise BackendRegistrationError(
                f"backend {self.backend_id!r} has an empty display_name"
            )

        if not isinstance(self.implemented, bool):
            raise BackendRegistrationError(
                f"backend {self.backend_id!r} implemented must be boolean"
            )

        if not self.supported_modes:
            raise BackendRegistrationError(
                f"backend {self.backend_id!r} must support at least one mode"
            )

        seen_modes = set()
        for mode in self.supported_modes:
            if mode == MODE_BACKEND_UNSUPPORTED:
                raise BackendRegistrationError(
                    f"backend {self.backend_id!r} must not list "
                    "backend_unsupported as an executable mode"
                )

            if mode not in SUPPORTED_MODES:
                raise BackendRegistrationError(
                    f"backend {self.backend_id!r} declares unknown mode {mode!r}"
                )

            if mode in seen_modes:
                raise BackendRegistrationError(
                    f"backend {self.backend_id!r} repeats mode {mode!r}"
                )
            seen_modes.add(mode)

        seen_aliases = set()
        for raw_alias in self.aliases:
            alias = normalize_backend_token(raw_alias)

            if alias == self.backend_id:
                raise BackendRegistrationError(
                    f"backend {self.backend_id!r} repeats its own id as an alias"
                )

            if alias in seen_aliases:
                raise BackendRegistrationError(
                    f"backend {self.backend_id!r} repeats alias {alias!r}"
                )
            seen_aliases.add(alias)

        if not isinstance(self.notes, str):
            raise BackendRegistrationError(
                f"backend {self.backend_id!r} notes must be a string"
            )

    def supports(self, mode: str) -> bool:
        return mode in self.supported_modes

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "display_name": self.display_name,
            "supported_modes": list(self.supported_modes),
            "implemented": self.implemented,
            "aliases": list(self.aliases),
            "notes": self.notes,
        }


# =============================================================================
# Resolution result
# =============================================================================


@dataclass(frozen=True)
class BackendDecision:
    status: str
    backend_id: str | None
    requested_backend: str
    mode: str
    execution_ready: bool
    reason: str
    spec: BackendSpec | None = None

    def validate(self) -> None:
        if self.status not in KNOWN_DECISION_STATUSES:
            raise BackendResolutionError(
                f"unknown backend decision status {self.status!r}"
            )

        if not isinstance(self.requested_backend, str):
            raise BackendResolutionError("requested_backend must be a string")

        if self.mode not in SUPPORTED_MODES:
            raise BackendResolutionError(
                f"unknown verification mode {self.mode!r}"
            )

        if not isinstance(self.execution_ready, bool):
            raise BackendResolutionError("execution_ready must be boolean")

        if not isinstance(self.reason, str) or not self.reason:
            raise BackendResolutionError("backend decision reason must be non-empty")

        if self.status == STATUS_READY:
            if not self.execution_ready:
                raise BackendResolutionError(
                    "ready backend decision must be execution_ready"
                )
            if self.spec is None or not self.spec.implemented:
                raise BackendResolutionError(
                    "ready backend decision requires an implemented backend spec"
                )
        else:
            if self.execution_ready:
                raise BackendResolutionError(
                    f"{self.status} backend decision cannot be execution_ready"
                )

    @property
    def ok(self) -> bool:
        return self.status == STATUS_READY

    @property
    def equivalence_verdict(self) -> str | None:
        """Return the canonical verdict implied by a non-executed decision.

        A ready backend has not run yet, so no verdict exists. Any non-ready
        backend/design decision means verification was not attempted; it must
        never be reported as functional failure.
        """
        if self.status == STATUS_READY:
            return None
        return EquivalenceVerdict.NOT_ATTEMPTED.value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "backend_id": self.backend_id,
            "requested_backend": self.requested_backend,
            "mode": self.mode,
            "execution_ready": self.execution_ready,
            "reason": self.reason,
            "equivalence_verdict": self.equivalence_verdict,
            "backend": self.spec.to_dict() if self.spec is not None else None,
        }


# =============================================================================
# Registry
# =============================================================================


def normalize_backend_token(value: str) -> str:
    """
    Normalize a backend id/alias for lookup.

    Examples:
        Yosys-SMTBMC -> yosys_smtbmc
        Jasper Gold  -> jasper_gold
    """

    token = str(value).strip().lower()
    token = token.replace("-", "_").replace(" ", "_")
    token = re.sub(r"_+", "_", token)
    return token


class BackendRegistry:
    """Deterministic backend-id/alias registry."""

    def __init__(self) -> None:
        self._specs: Dict[str, BackendSpec] = {}
        self._aliases: Dict[str, str] = {}

    def register(self, spec: BackendSpec) -> None:
        spec.validate()

        backend_id = normalize_backend_token(spec.backend_id)

        if backend_id != spec.backend_id:
            raise BackendRegistrationError(
                f"backend id must already be normalized: {spec.backend_id!r}"
            )

        if backend_id in self._specs or backend_id in self._aliases:
            raise BackendRegistrationError(
                f"backend id {backend_id!r} is already registered"
            )

        normalized_aliases = [
            normalize_backend_token(alias)
            for alias in spec.aliases
        ]

        for alias in normalized_aliases:
            if alias in self._specs or alias in self._aliases:
                raise BackendRegistrationError(
                    f"backend alias {alias!r} is already registered"
                )

        self._specs[backend_id] = spec

        for alias in normalized_aliases:
            self._aliases[alias] = backend_id

    def canonical_id(self, backend: str) -> str | None:
        token = normalize_backend_token(backend)

        if token in self._specs:
            return token

        return self._aliases.get(token)

    def get(self, backend: str) -> BackendSpec | None:
        canonical = self.canonical_id(backend)

        if canonical is None:
            return None

        return self._specs[canonical]

    def require(self, backend: str) -> BackendSpec:
        spec = self.get(backend)

        if spec is None:
            raise BackendResolutionError(
                f"unknown formal verification backend {backend!r}"
            )

        return spec

    def specs(self) -> Tuple[BackendSpec, ...]:
        return tuple(
            self._specs[backend_id]
            for backend_id in sorted(self._specs)
        )

    def implemented_specs(self) -> Tuple[BackendSpec, ...]:
        return tuple(
            spec
            for spec in self.specs()
            if spec.implemented
        )

    def decision(
        self,
        *,
        backend: str,
        mode: str,
        bundle_execution_ready: bool = True,
        unsupported_reason: str | None = None,
    ) -> BackendDecision:
        """
        Resolve one backend request without executing external tools.
        """

        requested_backend = str(backend)

        if mode not in SUPPORTED_MODES:
            raise BackendResolutionError(
                f"unknown verification mode {mode!r}; expected one of "
                f"{sorted(SUPPORTED_MODES)}"
            )

        if mode == MODE_BACKEND_UNSUPPORTED:
            reason = (
                unsupported_reason.strip()
                if isinstance(unsupported_reason, str) and unsupported_reason.strip()
                else "design is explicitly marked unsupported for formal execution"
            )

            decision = BackendDecision(
                status=STATUS_DESIGN_UNSUPPORTED,
                backend_id=self.canonical_id(requested_backend),
                requested_backend=requested_backend,
                mode=mode,
                execution_ready=False,
                reason=reason,
                spec=self.get(requested_backend),
            )
            decision.validate()
            return decision

        if not bundle_execution_ready:
            decision = BackendDecision(
                status=STATUS_DESIGN_UNSUPPORTED,
                backend_id=self.canonical_id(requested_backend),
                requested_backend=requested_backend,
                mode=mode,
                execution_ready=False,
                reason="SECBundle is not marked execution_ready",
                spec=self.get(requested_backend),
            )
            decision.validate()
            return decision

        spec = self.get(requested_backend)

        if spec is None:
            decision = BackendDecision(
                status=STATUS_UNKNOWN_BACKEND,
                backend_id=None,
                requested_backend=requested_backend,
                mode=mode,
                execution_ready=False,
                reason=f"unknown formal verification backend {requested_backend!r}",
                spec=None,
            )
            decision.validate()
            return decision

        if not spec.supports(mode):
            decision = BackendDecision(
                status=STATUS_MODE_UNSUPPORTED,
                backend_id=spec.backend_id,
                requested_backend=requested_backend,
                mode=mode,
                execution_ready=False,
                reason=(
                    f"backend {spec.backend_id!r} does not support "
                    f"verification mode {mode!r}"
                ),
                spec=spec,
            )
            decision.validate()
            return decision

        if not spec.implemented:
            decision = BackendDecision(
                status=STATUS_BACKEND_UNAVAILABLE,
                backend_id=spec.backend_id,
                requested_backend=requested_backend,
                mode=mode,
                execution_ready=False,
                reason=(
                    f"backend {spec.backend_id!r} is known to nEBULA but its "
                    "execution adapter is not implemented in this build"
                ),
                spec=spec,
            )
            decision.validate()
            return decision

        decision = BackendDecision(
            status=STATUS_READY,
            backend_id=spec.backend_id,
            requested_backend=requested_backend,
            mode=mode,
            execution_ready=True,
            reason=(
                f"backend {spec.backend_id!r} is registered and supports "
                f"verification mode {mode!r}"
            ),
            spec=spec,
        )
        decision.validate()
        return decision


# =============================================================================
# Default registry
# =============================================================================


def build_default_registry() -> BackendRegistry:
    registry = BackendRegistry()

    # ------------------------------------------------------------------
    # Current default implementation.
    # ------------------------------------------------------------------

    registry.register(
        BackendSpec(
            backend_id="yosys",
            display_name="Yosys Equivalence",
            supported_modes=(
                MODE_COMBINATIONAL_CEC,
                MODE_SEQUENTIAL_EQUIVALENCE,
            ),
            implemented=True,
            aliases=(
                "yosys_equiv",
            ),
            notes=(
                "Current built-in nEBULA backend. Tool availability is checked "
                "later by verify_rtl.py."
            ),
        )
    )

    # ------------------------------------------------------------------
    # Planned optional open-source SMT path.
    #
    # Keep this registered as KNOWN but not IMPLEMENTED so the system never
    # silently falls back to Yosys or pretends an SMT proof ran.
    # ------------------------------------------------------------------

    registry.register(
        BackendSpec(
            backend_id="yosys_smtbmc",
            display_name="Yosys SMTBMC",
            supported_modes=(
                MODE_SEQUENTIAL_EQUIVALENCE,
            ),
            implemented=False,
            aliases=(
                "smt",
                "smtbmc",
            ),
            notes=(
                "Planned optional SMT backend. Requires a dedicated adapter and "
                "solver policy before it may execute."
            ),
        )
    )

    # ------------------------------------------------------------------
    # Future commercial adapters.
    # ------------------------------------------------------------------

    registry.register(
        BackendSpec(
            backend_id="jaspergold",
            display_name="Cadence JasperGold",
            supported_modes=(
                MODE_COMBINATIONAL_CEC,
                MODE_SEQUENTIAL_EQUIVALENCE,
            ),
            implemented=False,
            aliases=(
                "jasper",
                "jg",
            ),
            notes="Future commercial formal-verification adapter.",
        )
    )

    registry.register(
        BackendSpec(
            backend_id="formality",
            display_name="Synopsys Formality",
            supported_modes=(
                MODE_COMBINATIONAL_CEC,
                MODE_SEQUENTIAL_EQUIVALENCE,
            ),
            implemented=False,
            aliases=(
                "fm",
                "synopsys_formality",
            ),
            notes="Future commercial equivalence-checking adapter.",
        )
    )

    return registry


DEFAULT_REGISTRY = build_default_registry()


# =============================================================================
# SECBundle-compatible resolution
# =============================================================================


def resolve_bundle_backend(
    bundle: Any,
    *,
    registry: BackendRegistry | None = None,
) -> BackendDecision:
    """
    Resolve a backend from any SECBundle-compatible object.

    Required attributes:
        backend
        mode
        execution_ready
        unsupported_reason

    Using an attribute contract here avoids a circular import between
    sec_bundle.py, formal_backend.py and verify_rtl.py.
    """

    active_registry = registry or DEFAULT_REGISTRY

    missing = [
        name
        for name in (
            "backend",
            "mode",
            "execution_ready",
            "unsupported_reason",
        )
        if not hasattr(bundle, name)
    ]

    if missing:
        raise BackendResolutionError(
            "object is not SECBundle-compatible; missing attribute(s): "
            + ", ".join(missing)
        )

    return active_registry.decision(
        backend=str(bundle.backend),
        mode=str(bundle.mode),
        bundle_execution_ready=bool(bundle.execution_ready),
        unsupported_reason=(
            str(bundle.unsupported_reason)
            if bundle.unsupported_reason is not None
            else None
        ),
    )


def decision_from_bundle_json(
    payload: Mapping[str, Any],
    *,
    registry: BackendRegistry | None = None,
) -> BackendDecision:
    """Resolve backend policy directly from serialized SECBundle JSON."""

    verification = payload.get("verification")

    if not isinstance(verification, Mapping):
        raise BackendResolutionError(
            "SECBundle JSON has no verification object"
        )

    backend = verification.get("backend")
    mode = verification.get("mode")
    execution_ready = verification.get("execution_ready")
    unsupported_reason = verification.get("unsupported_reason")

    if not isinstance(backend, str) or not backend:
        raise BackendResolutionError(
            "SECBundle verification.backend must be a non-empty string"
        )

    if not isinstance(mode, str) or not mode:
        raise BackendResolutionError(
            "SECBundle verification.mode must be a non-empty string"
        )

    if not isinstance(execution_ready, bool):
        raise BackendResolutionError(
            "SECBundle verification.execution_ready must be boolean"
        )

    return (registry or DEFAULT_REGISTRY).decision(
        backend=backend,
        mode=mode,
        bundle_execution_ready=execution_ready,
        unsupported_reason=(
            str(unsupported_reason)
            if unsupported_reason is not None
            else None
        ),
    )


# =============================================================================
# Self-test
# =============================================================================


@dataclass(frozen=True)
class _FakeBundle:
    backend: str
    mode: str
    execution_ready: bool
    unsupported_reason: str | None = None


def run_self_test() -> None:
    registry = build_default_registry()

    # 1. Current built-in Yosys backend is executable for both P0 modes.
    cec = registry.decision(
        backend="yosys",
        mode=MODE_COMBINATIONAL_CEC,
    )
    assert cec.ok
    assert cec.backend_id == "yosys"

    seq = registry.decision(
        backend="yosys_equiv",
        mode=MODE_SEQUENTIAL_EQUIVALENCE,
    )
    assert seq.ok
    assert seq.backend_id == "yosys"

    # 2. Alias normalization is deterministic.
    smt = registry.get("Yosys-SMTBMC")
    assert smt is not None
    assert smt.backend_id == "yosys_smtbmc"

    # 3. Known-but-unimplemented backend fails closed.
    smt_decision = registry.decision(
        backend="smtbmc",
        mode=MODE_SEQUENTIAL_EQUIVALENCE,
    )
    assert smt_decision.status == STATUS_BACKEND_UNAVAILABLE
    assert smt_decision.execution_ready is False

    # 4. Unsupported mode is distinct from unavailable implementation.
    smt_cec = registry.decision(
        backend="smt",
        mode=MODE_COMBINATIONAL_CEC,
    )
    assert smt_cec.status == STATUS_MODE_UNSUPPORTED

    # 5. Future commercial backends are visible but not executable.
    jasper = registry.decision(
        backend="jasper",
        mode=MODE_SEQUENTIAL_EQUIVALENCE,
    )
    assert jasper.status == STATUS_BACKEND_UNAVAILABLE
    assert jasper.backend_id == "jaspergold"

    formality = registry.decision(
        backend="fm",
        mode=MODE_COMBINATIONAL_CEC,
    )
    assert formality.status == STATUS_BACKEND_UNAVAILABLE
    assert formality.backend_id == "formality"

    # 6. Unknown backend is not silently substituted.
    unknown = registry.decision(
        backend="mystery_formal",
        mode=MODE_COMBINATIONAL_CEC,
    )
    assert unknown.status == STATUS_UNKNOWN_BACKEND
    assert unknown.backend_id is None

    # 7. Explicit design/backend unsupported mode never executes.
    unsupported = resolve_bundle_backend(
        _FakeBundle(
            backend="yosys",
            mode=MODE_BACKEND_UNSUPPORTED,
            execution_ready=False,
            unsupported_reason="memory abstraction unavailable",
        ),
        registry=registry,
    )
    assert unsupported.status == STATUS_DESIGN_UNSUPPORTED
    assert unsupported.execution_ready is False
    assert unsupported.reason == "memory abstraction unavailable"
    assert unsupported.equivalence_verdict == EquivalenceVerdict.NOT_ATTEMPTED.value

    # 8. Non-ready normal bundle fails closed.
    not_ready = resolve_bundle_backend(
        _FakeBundle(
            backend="yosys",
            mode=MODE_SEQUENTIAL_EQUIVALENCE,
            execution_ready=False,
        ),
        registry=registry,
    )
    assert not_ready.status == STATUS_DESIGN_UNSUPPORTED

    # 9. Serialized SECBundle verification shape resolves the same way.
    serialized = {
        "schema": "sec_bundle/1",
        "verification": {
            "backend": "yosys",
            "mode": "combinational_cec",
            "execution_ready": True,
            "unsupported_reason": None,
            "options": {},
        },
    }
    serialized_decision = decision_from_bundle_json(
        serialized,
        registry=registry,
    )
    assert serialized_decision.ok
    assert serialized_decision.backend_id == "yosys"

    # 10. Registry exposes only one currently implemented backend.
    implemented_ids = [
        spec.backend_id
        for spec in registry.implemented_specs()
    ]
    assert implemented_ids == ["yosys"]

    print("PASS: formal_backend self-test")
    print("PASS: Yosys registered as current implementation")
    print("PASS: combinational_cec capability resolved")
    print("PASS: sequential_equivalence capability resolved")
    print("PASS: backend aliases normalized deterministically")
    print("PASS: SMTBMC represented without pretending implementation exists")
    print("PASS: JasperGold/Formality represented as future adapters")
    print("PASS: unsupported mode distinguished from unavailable backend")
    print("PASS: unknown backend rejected without silent fallback")
    print("PASS: backend_unsupported maps to NOT_ATTEMPTED without a false failure claim")
    print("PASS: SECBundle-compatible backend resolution works")


# =============================================================================
# CLI
# =============================================================================


def load_json(path: Path) -> Dict[str, Any]:
    path = Path(path)

    if not path.is_file():
        raise BackendResolutionError(f"JSON file not found: {path}")

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BackendResolutionError(
            f"invalid JSON in {path}: {exc}"
        ) from exc

    if not isinstance(value, dict):
        raise BackendResolutionError(
            f"JSON root must be an object: {path}"
        )

    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="nEBULA formal-verification backend registry."
    )

    parser.add_argument(
        "--list",
        action="store_true",
        help="List known formal backends.",
    )

    parser.add_argument(
        "--backend",
        default=None,
        help="Backend id or alias to resolve.",
    )

    parser.add_argument(
        "--mode",
        choices=sorted(SUPPORTED_MODES),
        default=None,
        help="Verification mode to resolve.",
    )

    parser.add_argument(
        "--bundle",
        default=None,
        help="Optional serialized SECBundle JSON to resolve.",
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run standalone regression tests.",
    )

    args = parser.parse_args()

    try:
        if args.self_test:
            run_self_test()
            return 0

        if args.list:
            print(
                json.dumps(
                    {
                        "implemented_backends": [
                            spec.to_dict()
                            for spec in DEFAULT_REGISTRY.implemented_specs()
                        ],
                        "known_backends": [
                            spec.to_dict()
                            for spec in DEFAULT_REGISTRY.specs()
                        ],
                    },
                    indent=2,
                )
            )
            return 0

        if args.bundle:
            decision = decision_from_bundle_json(
                load_json(Path(args.bundle))
            )
            print(json.dumps(decision.to_dict(), indent=2))
            return 0 if decision.ok else 1

        if args.backend is not None or args.mode is not None:
            if not args.backend or not args.mode:
                parser.error("--backend and --mode must be supplied together")

            decision = DEFAULT_REGISTRY.decision(
                backend=args.backend,
                mode=args.mode,
            )
            print(json.dumps(decision.to_dict(), indent=2))
            return 0 if decision.ok else 1

        parser.error(
            "choose one of --self-test, --list, --bundle, "
            "or --backend <id> --mode <mode>"
        )

    except FormalBackendError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
