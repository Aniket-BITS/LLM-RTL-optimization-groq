import json
from collections import Counter
from pathlib import Path


BASE_DIR = Path(__file__).parent

INPUT_FILE = BASE_DIR / "timing_summary.json"
OUTPUT_FILE = BASE_DIR / "path_analysis.json"


# =========================================================
# Cell classification
# =========================================================

def classify_cell(cell_name):

    cell = cell_name.upper()

    if "DFF" in cell or "SDFF" in cell:
        return "sequential"

    if cell.startswith("FA"):
        return "full_adder"

    if cell.startswith("HA"):
        return "half_adder"

    if "MUX" in cell:
        return "mux"

    if cell.startswith("BUF"):
        return "buffer"

    if cell.startswith("INV"):
        return "inverter"

    if "XNOR" in cell:
        return "xnor"

    if "XOR" in cell:
        return "xor"

    if (
        cell.startswith("AOI")
        or cell.startswith("OAI")
    ):
        return "complex_logic"

    if (
        cell.startswith("NAND")
        or cell.startswith("NOR")
        or cell.startswith("AND")
        or cell.startswith("OR")
    ):
        return "basic_logic"

    return "other"


# =========================================================
# Utility functions
# =========================================================

def longest_run(categories, target_categories):

    longest = 0
    current = 0

    for category in categories:

        if category in target_categories:
            current += 1
            longest = max(longest, current)

        else:
            current = 0

    return longest


def safe_percent(part, total):

    if total is None or total <= 0:
        return 0.0

    return part / total * 100


# =========================================================
# Main analysis
# =========================================================

def analyze_path(summary):

    path = summary["worst_path"]

    stages = path["timing_stages"]

    analyzed_stages = []
    categories = []

    # -----------------------------------------------------
    # Accumulators
    # -----------------------------------------------------

    category_delay = Counter()
    category_count = Counter()

    arithmetic_delay = 0.0
    mux_delay = 0.0
    complex_logic_delay = 0.0

    max_slew = 0.0
    max_capacitance = 0.0

    high_slew_stages = []
    high_capacitance_stages = []

    # Heuristic thresholds
    HIGH_SLEW_NS = 0.05
    HIGH_CAP_FF = 10.0

    # -----------------------------------------------------
    # Analyze every timing stage
    # -----------------------------------------------------

    for stage in stages:

        cell = stage["cell"]
        category = classify_cell(cell)

        categories.append(category)
        category_count[category] += 1

        cell_delay = stage.get("cell_delay_ns") or 0.0
        net_delay = stage.get("incoming_net_delay_ns") or 0.0
        output_slew = stage.get("output_slew_ns") or 0.0
        capacitance = stage.get("output_capacitance_ff") or 0.0

        category_delay[category] += cell_delay

        if category in {
            "full_adder",
            "half_adder"
        }:
            arithmetic_delay += cell_delay

        if category == "mux":
            mux_delay += cell_delay

        if category == "complex_logic":
            complex_logic_delay += cell_delay

        max_slew = max(
            max_slew,
            output_slew
        )

        max_capacitance = max(
            max_capacitance,
            capacitance
        )

        if output_slew >= HIGH_SLEW_NS:

            high_slew_stages.append({
                "instance": stage["instance"],
                "cell": cell,
                "slew_ns": output_slew
            })

        if capacitance >= HIGH_CAP_FF:

            high_capacitance_stages.append({
                "instance": stage["instance"],
                "cell": cell,
                "capacitance_ff": capacitance
            })

        analyzed_stages.append({

            "instance": stage["instance"],
            "cell": cell,
            "category": category,

            "cell_delay_ns": cell_delay,
            "net_delay_ns": net_delay,

            "output_slew_ns": output_slew,
            "output_capacitance_ff": capacitance
        })

    # -----------------------------------------------------
    # Basic structural information
    # -----------------------------------------------------

    logic_depth = len(stages)

    arithmetic_count = (
        category_count["full_adder"]
        + category_count["half_adder"]
    )

    mux_count = category_count["mux"]

    arithmetic_chain = longest_run(
        categories,
        {
            "full_adder",
            "half_adder"
        }
    )

    mux_chain = longest_run(
        categories,
        {"mux"}
    )

    # -----------------------------------------------------
    # Delay information
    # -----------------------------------------------------

    total_cell_delay = (
        path["total_cell_delay_ns"]
    )

    total_net_delay = (
        path["total_net_delay_ns"]
    )

    total_structural_delay = (
        total_cell_delay
        + total_net_delay
    )

    cell_delay_percent = safe_percent(
        total_cell_delay,
        total_structural_delay
    )

    net_delay_percent = safe_percent(
        total_net_delay,
        total_structural_delay
    )

    arithmetic_delay_percent = safe_percent(
        arithmetic_delay,
        total_cell_delay
    )

    mux_delay_percent = safe_percent(
        mux_delay,
        total_cell_delay
    )

    complex_logic_delay_percent = safe_percent(
        complex_logic_delay,
        total_cell_delay
    )

    # -----------------------------------------------------
    # Logic vs routing dominated
    # -----------------------------------------------------

    if cell_delay_percent >= 70:
        delay_domain = "logic_dominated"

    elif net_delay_percent >= 50:
        delay_domain = "routing_dominated"

    else:
        delay_domain = "mixed"

    # -----------------------------------------------------
    # Bottleneck scores
    # -----------------------------------------------------

    scores = {

        "arithmetic_chain": 0,
        "mux_chain": 0,
        "deep_logic": 0,
        "routing": 0,
        "load_or_slew": 0
    }

    # Arithmetic
    if arithmetic_count >= 2:
        scores["arithmetic_chain"] += 2

    if arithmetic_chain >= 2:
        scores["arithmetic_chain"] += 2

    if arithmetic_delay_percent >= 25:
        scores["arithmetic_chain"] += 2

    if arithmetic_delay_percent >= 40:
        scores["arithmetic_chain"] += 2

    # MUX
    if mux_count >= 2:
        scores["mux_chain"] += 2

    if mux_chain >= 2:
        scores["mux_chain"] += 2

    if mux_delay_percent >= 25:
        scores["mux_chain"] += 2

    # Deep logic
    if logic_depth >= 6:
        scores["deep_logic"] += 2

    if logic_depth >= 10:
        scores["deep_logic"] += 3

    if logic_depth >= 15:
        scores["deep_logic"] += 2

    # Routing
    if net_delay_percent >= 20:
        scores["routing"] += 2

    if net_delay_percent >= 40:
        scores["routing"] += 3

    if net_delay_percent >= 60:
        scores["routing"] += 2

    # Slew / capacitance
    if len(high_slew_stages) >= 1:
        scores["load_or_slew"] += 2

    if len(high_capacitance_stages) >= 1:
        scores["load_or_slew"] += 2

    if len(high_slew_stages) >= 3:
        scores["load_or_slew"] += 2

    # -----------------------------------------------------
    # Determine bottleneck
    # -----------------------------------------------------

    max_score = max(scores.values())

    if max_score == 0:
        primary_bottleneck = "unknown"

    else:
        primary_bottleneck = max(
            scores,
            key=scores.get
        )

    # -----------------------------------------------------
    # Dominant cells
    # -----------------------------------------------------

    dominant_cells = sorted(
        analyzed_stages,
        key=lambda x: x["cell_delay_ns"],
        reverse=True
    )[:5]

    # -----------------------------------------------------
    # Recommendations
    # -----------------------------------------------------

    recommendations = []

    if delay_domain == "logic_dominated":
        recommendations.append(
            "Focus on RTL restructuring rather than physical routing optimization."
        )

    elif delay_domain == "routing_dominated":
        recommendations.append(
            "Investigate placement, fanout, buffering, and routing before changing RTL."
        )

    if primary_bottleneck == "arithmetic_chain":

        recommendations.extend([
            "Inspect the RTL for serial arithmetic operations.",
            "Consider pipelining the arithmetic path.",
            "Consider restructuring arithmetic into a balanced tree.",
            "Check whether carry propagation can be reduced."
        ])

    elif primary_bottleneck == "mux_chain":

        recommendations.extend([
            "Inspect nested if/else or case logic.",
            "Reduce cascaded mux depth.",
            "Restructure selection logic."
        ])

    elif primary_bottleneck == "deep_logic":

        recommendations.extend([
            "Reduce combinational logic depth.",
            "Consider inserting a pipeline stage.",
            "Restructure large combinational expressions."
        ])

    elif primary_bottleneck == "routing":

        recommendations.extend([
            "Inspect high-fanout nets.",
            "Consider register duplication or buffering.",
            "Check placement distance along the critical path."
        ])

    elif primary_bottleneck == "load_or_slew":

        recommendations.extend([
            "Inspect high-capacitance outputs.",
            "Inspect stages with poor slew.",
            "Consider buffering or reducing fanout."
        ])

    else:

        recommendations.append(
            "Inspect the critical RTL manually."
        )

    # -----------------------------------------------------
    # Final result
    # -----------------------------------------------------

    result = {

        "startpoint": path["startpoint"],
        "endpoint": path["endpoint"],
        "slack_ns": path["slack_ns"],

        "delay_domain": delay_domain,

        "logic_depth": logic_depth,

        "cell_delay_ns": total_cell_delay,
        "net_delay_ns": total_net_delay,

        "cell_delay_percent":
            cell_delay_percent,

        "net_delay_percent":
            net_delay_percent,

        "cell_counts":
            dict(category_count),

        "delay_by_category_ns":
            dict(category_delay),

        "arithmetic_cells":
            arithmetic_count,

        "arithmetic_delay_ns":
            arithmetic_delay,

        "arithmetic_delay_percent":
            arithmetic_delay_percent,

        "longest_arithmetic_chain":
            arithmetic_chain,

        "mux_cells":
            mux_count,

        "mux_delay_ns":
            mux_delay,

        "mux_delay_percent":
            mux_delay_percent,

        "longest_mux_chain":
            mux_chain,

        "complex_logic_delay_percent":
            complex_logic_delay_percent,

        "max_output_slew_ns":
            max_slew,

        "max_output_capacitance_ff":
            max_capacitance,

        "high_slew_stages":
            high_slew_stages,

        "high_capacitance_stages":
            high_capacitance_stages,

        "scores":
            scores,

        "primary_bottleneck":
            primary_bottleneck,

        "dominant_cells":
            dominant_cells,

        "recommendations":
            recommendations,

        "analyzed_stages":
            analyzed_stages
    }

    return result


# =========================================================
# Pretty printing
# =========================================================

def print_analysis(result):

    print(
        "\n========== CRITICAL PATH ANALYSIS ==========\n"
    )

    print(
        "Startpoint :",
        result["startpoint"]
    )

    print(
        "Endpoint   :",
        result["endpoint"]
    )

    print(
        f"Slack      : "
        f"{result['slack_ns']:.4f} ns"
    )

    print(
        f"Logic depth: "
        f"{result['logic_depth']}"
    )

    print(
        "\nDelay type :",
        result["delay_domain"]
    )

    print(
        f"Cell delay : "
        f"{result['cell_delay_percent']:.1f}%"
    )

    print(
        f"Net delay  : "
        f"{result['net_delay_percent']:.1f}%"
    )

    # -----------------------------------------------------

    print("\n========== STRUCTURE ==========\n")

    print(
        "Arithmetic cells :",
        result["arithmetic_cells"]
    )

    print(
        f"Arithmetic delay : "
        f"{result['arithmetic_delay_ns']:.4f} ns "
        f"({result['arithmetic_delay_percent']:.1f}%)"
    )

    print(
        "Longest arithmetic chain :",
        result["longest_arithmetic_chain"]
    )

    print(
        "MUX cells :",
        result["mux_cells"]
    )

    print(
        f"MUX delay : "
        f"{result['mux_delay_ns']:.4f} ns "
        f"({result['mux_delay_percent']:.1f}%)"
    )

    print(
        "Longest MUX chain :",
        result["longest_mux_chain"]
    )

    # -----------------------------------------------------

    print("\n========== BOTTLENECK SCORES ==========\n")

    for name, score in result["scores"].items():

        print(
            f"{name:20s}: {score}"
        )

    print(
        "\nPRIMARY BOTTLENECK:",
        result["primary_bottleneck"]
    )

    # -----------------------------------------------------

    print("\n========== DOMINANT CELLS ==========\n")

    for stage in result["dominant_cells"]:

        print(
            f"{stage['instance']:20s} "
            f"{stage['cell']:15s} "
            f"{stage['cell_delay_ns']:.4f} ns"
        )

    # -----------------------------------------------------

    if result["high_slew_stages"]:

        print("\n========== HIGH SLEW ==========\n")

        for stage in result["high_slew_stages"]:

            print(
                f"{stage['instance']:20s} "
                f"{stage['cell']:15s} "
                f"{stage['slew_ns']:.4f} ns"
            )

    # -----------------------------------------------------

    if result["high_capacitance_stages"]:

        print(
            "\n========== HIGH CAPACITANCE ==========\n"
        )

        for stage in result[
            "high_capacitance_stages"
        ]:

            print(
                f"{stage['instance']:20s} "
                f"{stage['cell']:15s} "
                f"{stage['capacitance_ff']:.2f} fF"
            )

    # -----------------------------------------------------

    print("\n========== RECOMMENDATIONS ==========\n")

    for recommendation in result["recommendations"]:

        print(
            f"- {recommendation}"
        )

    print(
        "\n============================================"
    )


# =========================================================
# Main
# =========================================================

def main():

    if not INPUT_FILE.exists():

        raise FileNotFoundError(
            f"{INPUT_FILE} not found.\n"
            "Run parse_timing.py first."
        )

    with open(INPUT_FILE, "r") as file:
        summary = json.load(file)

    result = analyze_path(summary)

    print_analysis(result)

    with open(OUTPUT_FILE, "w") as file:

        json.dump(
            result,
            file,
            indent=4
        )

    print(
        f"\nAnalysis written to:\n"
        f"{OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()