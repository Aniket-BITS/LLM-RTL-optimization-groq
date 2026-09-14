from __future__ import annotations

import json
from pathlib import Path

from timing_automation.map_to_rtl import (
    RTLGraphIndex,
    apply_witness_directory,
    derive_witness_requests,
)
from timing_automation.classify_path import effective_operator_counts


def _graph():
    return {
        "cells": {
            "ra": {"sequential": True, "type": "$_DFF_P_", "connections": {"Q": [1], "D": [11]}, "port_directions": {"Q": "output", "D": "input"}, "input_bits": [11], "output_bits": [1]},
            "rb": {"sequential": True, "type": "$_DFF_P_", "connections": {"Q": [2], "D": [12]}, "port_directions": {"Q": "output", "D": "input"}, "input_bits": [12], "output_bits": [2]},
            "xor": {"sequential": False, "type": "$xor", "connections": {"A": [1], "B": [1], "Y": [3]}, "port_directions": {"A": "input", "B": "input", "Y": "output"}, "input_bits": [1], "output_bits": [3]},
            "add": {"sequential": False, "type": "$add", "connections": {"A": [2], "B": [2], "Y": [4]}, "port_directions": {"A": "input", "B": "input", "Y": "output"}, "input_bits": [2], "output_bits": [4]},
            "mux": {"sequential": False, "type": "$mux", "connections": {"A": [3], "B": [4], "S": [9], "Y": [5]}, "port_directions": {"A": "input", "B": "input", "S": "input", "Y": "output"}, "input_bits": [3, 4, 9], "output_bits": [5]},
        },
        "signal_bits": {"a_reg[0]": [1], "b_reg[0]": [2]},
        "bit_names": {"1": ["a_reg[0]"], "2": ["b_reg[0]"]},
    }


def test_two_pass_witness_roundtrip(tmp_path: Path):
    idx = RTLGraphIndex(_graph(), "demo")
    mapped = [{
        "startpoint": "shared_reg[0]$_DFF_P_/Q",
        "endpoint": "y[0]$_DFF_P_/D",
        "slack_ns": -0.1,
        "cells": [{"instance": "u_mux", "type": "MUX2_X1", "input_pin": "A2", "output_pin": "Z"}],
        "rtl_mapping": {
            "remaining_logical_reconvergence": True,
            "operator_counts": {"$xor": 1, "$add": 1, "$mux": 1},
            "logical_branch_points": [{"cell": "mux", "ambiguous_outputs": [{"output_index": 0, "output_bit": 5, "a_bit": 3, "b_bit": 4}]}],
        },
    }]

    request = derive_witness_requests(mapped, idx, design="demo", platform="nangate45", variant="run_demo")
    ready = [r for r in request["requests"] if r.get("derivation_status") == "ready"]
    assert len(ready) == 2

    # Physical evidence says the mux_b witness used the same final mux input
    # as the ambiguous path, while mux_a used the other input.
    for req in ready:
        pin = "A2" if req["branch"] == "mux_b" else "A1"
        data = {"paths": [{
            "startpoint": f"{req['source_signal']}$_DFF_P_/Q",
            "endpoint": "y[0]$_DFF_P_/D",
            "slack_ns": -0.2,
            "cells": [{"instance": "u_mux", "type": "MUX2_X1", "input_pin": pin, "output_pin": "Z"}],
        }]}
        (tmp_path / f"witness_{req['witness_id']}_timing.json").write_text(json.dumps(data), encoding="utf-8")

    resolved = apply_witness_directory(mapped, request, tmp_path)
    assert resolved == 1
    narrowing = mapped[0]["rtl_mapping"]["physical_narrowing"]
    assert narrowing["resolved"] is True
    assert narrowing["selected_rtl_branch"] == "mux_b"
    assert narrowing["selected_rtl_operator"] == "$add"

    counts, resolution = effective_operator_counts(mapped[0]["rtl_mapping"], None)
    assert resolution["status"] == "resolved_physically"
    assert counts == {"$add": 1, "$mux": 1}
