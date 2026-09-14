# Steps 1–2: patches to existing files

These are small find/replace edits. Apply them to your real
`collect_metrics.py` and `build_optimizer_input.py`. Each block shows the exact
current text to FIND and what to REPLACE it with.

---

## 1. `collect_metrics.py` — add power, canonical schema, six-decimal normalization

### 1a. Add power keys to `METRIC_MAP`

FIND:
```python
    # Useful comparison against synthesized RTL
    "synth_area": "synth__design__instance__area__stdcell",
}
```
REPLACE:
```python
    # Useful comparison against synthesized RTL
    "synth_area": "synth__design__instance__area__stdcell",

    # Power (Watts in ORFS; converted to mW in the canonical block)
    "power_total_w": "finish__power__total",
    "power_internal_w": "finish__power__internal__total",
    "power_switching_w": "finish__power__switching__total",
    "power_leakage_w": "finish__power__leakage__total",
}
```

### 1b. Add the canonical builder (insert right after the `METRIC_MAP` block)

INSERT this block immediately after the closing `}` of `METRIC_MAP`:
```python
# =========================================================
# Canonical PPA metric schema (consumed by build_optimizer_input.py
# and by candidate comparison). One shape, six-decimal normalized,
# power in mW. This is the single metric contract for the whole loop.
# =========================================================

CANONICAL_SCHEMA = "ppa_metrics/1"


def round6(v):
    """Normalize a numeric to six decimals; pass through None/non-numeric."""
    try:
        return round(float(v), 6)
    except (TypeError, ValueError):
        return None


def w_to_mw(v):
    r = round6(v)
    return None if r is None else round6(r * 1000.0)


def build_canonical(metrics: dict) -> dict:
    return {
        "schema": CANONICAL_SCHEMA,
        "wns_ns": round6(metrics.get("wns")),
        "tns_ns": round6(metrics.get("tns")),
        "area_um2": round6(metrics.get("area")),
        "power_mw": w_to_mw(metrics.get("power_total_w")),
        "power_breakdown_mw": {
            "internal": w_to_mw(metrics.get("power_internal_w")),
            "switching": w_to_mw(metrics.get("power_switching_w")),
            "leakage": w_to_mw(metrics.get("power_leakage_w")),
        },
        "setup_violations": metrics.get("setup_violations"),
        "hold_violations": metrics.get("hold_violations"),
        "drc_errors": metrics.get("drc_errors"),
        "wirelength_um": round6(metrics.get("wirelength")),
    }
```

### 1c. Emit the canonical block in the result

FIND:
```python
        "flow_complete": flow_complete,

        "metrics": metrics,

        "clocks": clocks,
```
REPLACE:
```python
        "flow_complete": flow_complete,

        "metrics": metrics,

        "canonical": build_canonical(metrics),

        "clocks": clocks,
```

### 1d. (optional) show power in the summary

FIND:
```python
    print(f"Area             : {metrics['area']}")
    print(f"Synthesis Area   : {metrics['synth_area']}")
```
REPLACE:
```python
    print(f"Area             : {metrics['area']}")
    print(f"Power (mW)       : {result['canonical']['power_mw']}")
    print(f"Synthesis Area   : {metrics['synth_area']}")
```

---

## 2. `build_optimizer_input.py` — consume canonical metrics (+ power)

### 2a. Prefer the canonical block, add `power_mw`

FIND:
```python
def extract_baseline_metrics(metrics: Dict[str, Any]) -> Dict[str, Optional[float]]:
    wns = deep_find_numeric(
```
REPLACE:
```python
def extract_baseline_metrics(metrics: Dict[str, Any]) -> Dict[str, Optional[float]]:
    # Prefer the normalized canonical block emitted by collect_metrics.py
    # (ppa_metrics/1): already six-decimal normalized, power already in mW.
    canon = metrics.get("canonical")
    if isinstance(canon, dict) and str(canon.get("schema", "")).startswith("ppa_metrics/"):
        return {
            "wns_ns": canon.get("wns_ns"),
            "tns_ns": canon.get("tns_ns"),
            "area_um2": canon.get("area_um2"),
            "power_mw": canon.get("power_mw"),
        }

    wns = deep_find_numeric(
```

FIND (the fallback return of the same function):
```python
    return {
        "wns_ns": wns,
        "tns_ns": tns,
        "area_um2": area,
    }
```
REPLACE:
```python
    # Fallback path (no canonical block). Note: a raw ORFS power key is in
    # Watts here, not mW — the canonical path above is the correct source.
    power_mw = deep_find_numeric(
        metrics,
        preferred_keys=("power_mw", "power"),
        fuzzy_tokens=("power",),
    )
    return {
        "wns_ns": wns,
        "tns_ns": tns,
        "area_um2": area,
        "power_mw": power_mw,
    }
```

### 2b. Don't fatal on missing power (it's optional)

FIND:
```python
    baseline = extract_baseline_metrics(metrics)
    for key, value in baseline.items():
        if value is None:
            append_unique(fatal_errors, f"Required baseline metric is missing: {key}")
```
REPLACE:
```python
    baseline = extract_baseline_metrics(metrics)
    for key in ("wns_ns", "tns_ns", "area_um2"):
        if baseline.get(key) is None:
            append_unique(fatal_errors, f"Required baseline metric is missing: {key}")
    # power_mw is optional: absent only means report_power wasn't collected.
```

After these, `baseline_metrics` in `optimizer_input.json` gains `power_mw`, and
every candidate you evaluate reports the same `ppa_metrics/1` shape — so PPA
comparison is apples-to-apples.
