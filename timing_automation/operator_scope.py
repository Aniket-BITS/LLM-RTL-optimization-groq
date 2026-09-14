"""Advisory diagnostic focus derived from compiler source attribution.

These ranges are recommendations for the LLM, never mutation permissions.
The hard mutation boundary is the exact SourceBundle plus deterministic lint/formal policy.
"""
import hashlib
import json
from pathlib import Path


def derive_focus_scopes(unit, target, pattern_ids):
    locations = unit.get("source", {}).get("source_locations", [])
    result = {}
    for pattern_id in pattern_ids:
        if not isinstance(pattern_id, str) or Path(pattern_id).name != pattern_id:
            raise ValueError("invalid pattern identity")
        pattern = json.loads((Path(__file__).parent / "patterns" / (pattern_id + ".json")).read_text())
        roles = pattern.get("target_operator_roles")
        if not isinstance(roles, list) or not roles:
            raise ValueError(f"pattern {pattern_id} has no target_operator_roles")
        allowed = set(roles)
        ranges = []
        for loc in locations:
            observed = set(loc.get("operator_roles", []))
            if (loc.get("file") != target["file"] or
                    loc.get("module") != target["module"] or
                    loc.get("sha256") != target["original_sha256"]):
                continue
            if not observed or not observed <= allowed:
                continue
            lo, hi = loc.get("line_start"), loc.get("line_end")
            if type(lo) is not int or type(hi) is not int or not 1 <= lo <= hi:
                raise ValueError("invalid operator source interval")
            conflicts = [other for other in locations
                         if other.get("file") == target["file"]
                         and other.get("line_start", hi + 1) <= hi
                         and other.get("line_end", 0) >= lo
                         and (not other.get("operator_roles") or
                              not set(other["operator_roles"]) <= allowed)]
            if not conflicts:
                ranges.append((lo, hi))
        result[pattern_id] = [list(pair) for pair in sorted(set(ranges))]
    return result


def verified_scopes(request):
    ref = request.get("operator_scope_evidence", {})
    data = Path(ref.get("path", "")).read_bytes()
    if hashlib.sha256(data).hexdigest() != ref.get("sha256"):
        raise ValueError("operator evidence hash mismatch")
    opt = json.loads(data)
    units = [u for u in opt.get("optimization_units", []) if u.get("unit_id") == request["unit_id"]]
    if len(units) != 1:
        raise ValueError("operator evidence unit is ambiguous or absent")
    scopes = derive_focus_scopes(units[0], request["target"], request.get("recommended_pattern_ids", request.get("eligible_pattern_ids", [])))
    if scopes != request.get("recommended_pattern_focus_ranges", request.get("pattern_editable_line_ranges")):
        raise ValueError("pattern focus does not match independently derived diagnostic evidence")
    return scopes


# Backward-compatible name; callers must treat the result as advisory only.
derive_scopes = derive_focus_scopes
