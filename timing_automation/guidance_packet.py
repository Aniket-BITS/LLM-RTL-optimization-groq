"""Optional timing-to-guidance adapter. All diagnostic failures fail open."""
from __future__ import annotations
import copy
import hashlib
import json
import math
from pathlib import Path
from build_optimizer_input import operator_signature, normalize_transformation_family


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def normalize_mapping(mapped, trusted_sources):
    """Only exact, hash-verified user-source regions become prompt locations."""
    mapping = mapped.get("rtl_mapping", {})
    status = mapping.get("status", "unavailable")
    if status != "mapped":
        return {"status": status, "confidence": "low", "reason": "mapping_" + str(status), "regions": []}
    regions = []
    for region in mapping.get("rtl_regions", []):
        source = trusted_sources.get(str(Path(region["file"]).resolve()))
        if not source or region.get("sha256") != source["sha256"]:
            continue
        start, end = region.get("start_line"), region.get("end_line")
        lines = source["text"].splitlines()
        if type(start) is int and type(end) is int and 1 <= start <= end <= len(lines):
            regions.append({"file": source["name"], "sha256": source["sha256"],
                            "start_line": start, "end_line": end,
                            "excerpt": "\n".join(lines[start - 1:end])})
    # Bit-precise mapping proves a cone, not physical branch attribution.
    # Reconvergence therefore lowers confidence until witnessed by the existing mapper.
    reliable = bool(regions) and len(regions) == len(mapping.get("rtl_regions", [])) and not mapping.get("remaining_logical_reconvergence", False)
    if mapping.get("confidence") in ("low", "medium"):
        reliable = False
    return {"status": "mapped", "confidence": "high" if reliable else "low",
            "reason": None if reliable else "untrusted_or_ambiguous_mapping",
            "regions": regions if reliable else []}


def normalize_classification(value):
    c = value.get("classification", {})
    reliable = c.get("status") == "classified" and c.get("confidence") in ("high", "medium")
    family = normalize_transformation_family(c.get("structural_pattern"))
    if family in (None, "mixed_or_unclassified_logic", "direct_or_empty_logic", "unknown", "unclassified"):
        reliable = False
    return {"status": "classified" if reliable else "unclassified",
            "confidence": c.get("confidence", "low"),
            "family": family if reliable else None,
            "structural_class": c.get("structural_class") if reliable else None}


def build_packet(paths, mapper, classifier, trusted_sources, *, mode="auto", history=(),
                 max_groups=3, max_excerpts=2, evidence=None):
    packet = {"schema": "guidance_packet/1", "mode_requested": mode,
              "mode_used": "exploratory", "mapping_status": "unavailable",
              "mapping_confidence": "low", "classification_status": "skipped",
              "fallback_reason": "mapping_unavailable", "groups": [], "recommendations": [],
              "prior_attempt_outcomes": list(history), "evidence": evidence or {},
              "path_groups_considered": 0, "path_groups_sent_to_llm": 0,
              "prompt_compaction": {}}
    try:
        if not isinstance(paths, list) or not paths:
            raise ValueError("no parsed timing paths")
        grouped = {}
        failures = []
        mapping_statuses = []
        for path in paths:
            if not isinstance(path, dict) or not isinstance(path.get("slack_ns"), (int, float)) or not math.isfinite(path["slack_ns"]):
                raise ValueError("malformed timing path/slack")
            mapped = mapper(path)
            mapping = normalize_mapping(mapped, trusted_sources)
            mapping_statuses.append(mapping["status"])
            if mapping["confidence"] != "high":
                failures.append(mapping["reason"])
                continue
            try:
                classification = normalize_classification(classifier(mapped))
            except Exception as exc:
                packet["classification_status"] = "failed"
                raise RuntimeError(f"classification_failed: {type(exc).__name__}: {exc}") from exc
            regions = mapping["regions"]
            key = fingerprint({"locations": sorted([{k: r[k] for k in ("file", "sha256", "start_line", "end_line")} for r in regions],
                                                    key=lambda r: (r["file"], r["start_line"], r["end_line"])),
                               "class": classification.get("structural_class"), "pattern": classification.get("family"),
                               "operators": operator_signature(mapped.get("rtl_mapping", {}).get("operator_counts", {})),
                               "path_type": path.get("path_type"), "path_group": path.get("path_group")})
            item = {"group_id": key[:16], "startpoint": path.get("startpoint"),
                    "endpoint": path.get("endpoint"), "slack_ns": path["slack_ns"],
                    "path_type": path.get("path_type"), "path_ids": [path.get("path_id")],
                    "timing": {k: path.get(k) for k in ("logic_depth", "cell_delay_sum_ns", "net_delay_sum_ns", "cell_type_counts", "max_fanout")},
                    "mapping_confidence": "high", "classification": classification,
                    "source_excerpts": regions[:max_excerpts]}
            if key not in grouped:
                grouped[key] = item
            else:
                old = grouped[key]
                ids = old["path_ids"] + item["path_ids"]
                grouped[key] = min((old, item), key=lambda x: x["slack_ns"])
                grouped[key]["path_ids"] = ids
        packet["path_groups_considered"] = len(grouped)
        packet["mapping_failure_reasons"] = sorted(set(failures))
        if mapping_statuses and len(set(mapping_statuses)) == 1:
            packet["mapping_status"] = mapping_statuses[0]
        if not grouped and failures:
            packet["fallback_reason"] = failures[0]
        groups = sorted(grouped.values(), key=lambda x: (x["slack_ns"], x["group_id"]))[:max_groups]
        if groups:
            packet.update(mapping_status="mapped", mapping_confidence="high", groups=groups)
            classified = [g for g in groups if g["classification"]["status"] == "classified"]
            packet["classification_status"] = "classified" if classified else (
                "failed" if any(g["classification"]["status"] == "failed" for g in groups) else "unclassified")
            packet["recommendations"] = [{"group_id": g["group_id"], "transformation_family": g["classification"]["family"],
                                           "instruction": "Consider an equivalent alternative for this evidence-backed structural pattern."}
                                          for g in classified if g["classification"].get("family")]
            packet["mode_used"] = "guided" if classified else "guided_evidence_only"
            packet["fallback_reason"] = None if classified else "No reliable classification was identified."
        if mode == "exploratory":
            packet.update(mode_used="exploratory", fallback_reason="explicit_exploratory")
        if packet["mode_used"] == "exploratory":
            packet.update(groups=[], recommendations=[])
        packet["path_groups_sent_to_llm"] = len(packet["groups"])
        packet["evidence_fingerprint"] = fingerprint({"paths": paths, "evidence": evidence or {}})
    except Exception as exc:
        packet.update(mode_used="exploratory", mapping_status="failed", mapping_confidence="low",
                      fallback_reason="guidance_error",
                      guidance_error=f"{type(exc).__name__}: {exc}", groups=[], recommendations=[], path_groups_sent_to_llm=0)
    return packet




def _physical_source_for_run(run, authoritative_source):
    """Resolve the exact Verilog file used by the physical run.

    The Current-Best snapshot is the authoritative RTL object exposed to the
    optimizer, but Yosys source provenance inside the physical checkpoint keeps
    the path that was actually passed through VERILOG_FILES.  Those paths may
    differ even when the bytes are identical.  Mapping must use the physical-run
    path while trusting it only when its SHA-256 matches the authoritative
    Current-Best source and run metadata.
    """
    authoritative_source = Path(authoritative_source)
    authoritative_sha = hashlib.sha256(authoritative_source.read_bytes()).hexdigest()
    expected_sha = run.get("source_sha256")
    if expected_sha != authoritative_sha:
        raise ValueError("timing_source_identity_unverified")

    candidates = []
    for arg in run.get("command", []) or []:
        if isinstance(arg, str) and arg.startswith("VERILOG_FILES="):
            value = arg.split("=", 1)[1].strip()
            # Current optimizer physical runs use one materialized Verilog file.
            # Keep this conservative: only accept an existing exact path whose
            # bytes match the authoritative source.
            if value:
                candidates.append(Path(value))

    for candidate in candidates:
        if candidate.is_file():
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            if digest == authoritative_sha:
                return candidate

    # Backward-compatible case: the snapshot itself was the physical source.
    return authoritative_source

def packet_from_run(run, source, top, directory, flow_root, *, mode="auto", history=(),
                    max_groups=3, max_excerpts=2):
    """Use the timing report and canonical RTL graph from the same physical run."""
    from parse_timing import parse_report
    from map_to_rtl import RTLGraphIndex, SourceHashCache, map_path
    from classify_path import classify_one_path
    from build_rtl_graph import build_rtl_graph
    kwargs = dict(mode=mode, history=history, max_groups=max_groups, max_excerpts=max_excerpts)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        relative = Path(run["metrics_path"]).parent.relative_to(flow_root / "logs")
        report = flow_root / "reports" / relative / "5_global_route.rpt"
        source = Path(source)
        authoritative_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        physical_source = _physical_source_for_run(run, source)
        physical_sha = hashlib.sha256(physical_source.read_bytes()).hexdigest()
        evidence = {"report": str(report), "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                    "source_sha256": authoritative_sha,
                    "physical_source": str(physical_source),
                    "physical_source_sha256": physical_sha,
                    "metrics": run.get("metrics", {}), "source_role": "current_diagnosis"}
        parsed = parse_report(report)
        (directory / "parsed_timing.json").write_text(json.dumps(parsed, indent=2, sort_keys=True) + "\n")
        evidence["parsed_timing_artifact"] = str(directory / "parsed_timing.json")
        paths = parsed["paths"]
        # Explicit exploratory arms do not spend work on mapping/classification.
        if mode == "exploratory":
            return build_packet(paths, lambda p: {}, lambda p: {}, {}, evidence=evidence, **kwargs)
        checkpoint = flow_root / "results" / relative / "1_1_yosys_canonicalize.rtlil"
        evidence["checkpoint_sha256"] = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
        _, graph = build_rtl_graph(flow_dir=flow_root, platform="nangate45", design=relative.parts[1],
                                  top=top, variant=relative.parts[2], checkpoint=checkpoint,
                                  output_file=directory / "rtl_graph.json")
        index, hasher = RTLGraphIndex(graph, top), SourceHashCache()
        trusted = {str(physical_source.resolve()): {"name": source.name, "sha256": evidence["source_sha256"],
                                                   "text": source.read_text()}}
        def mapper(p):
            mapped = map_path(p, index, flow_root, [physical_source.parent], hasher)
            (directory / f"mapped_path_{p['path_id']}.json").write_text(json.dumps(mapped, indent=2, sort_keys=True) + "\n")
            return mapped
        def classifier(p):
            classified = classify_one_path(p, None)
            (directory / f"classified_path_{p['path_id']}.json").write_text(json.dumps(classified, indent=2, sort_keys=True) + "\n")
            return classified
        return build_packet(paths, mapper, classifier, trusted, evidence=evidence, **kwargs)
    except Exception as exc:
        packet = build_packet([], None, None, {}, **kwargs)
        packet["guidance_error"] = f"{type(exc).__name__}: {exc}"
        return packet


def compact_packet(packet, max_chars):
    """Trim advisory payload only. Source/contract/budgets live outside this cap."""
    p = copy.deepcopy(packet)
    decisions = []
    def size():
        return len(json.dumps(p, sort_keys=True))
    while size() > max_chars and len(p.get("groups", [])) > 1:
        removed = p["groups"].pop()
        p["recommendations"] = [r for r in p.get("recommendations", []) if r["group_id"] != removed["group_id"]]
        decisions.append("removed_lower_ranked_group")
    for g in reversed(p.get("groups", [])):
        while size() > max_chars and g.get("source_excerpts"):
            g["source_excerpts"].pop()
            decisions.append("removed_source_excerpt")
    while size() > max_chars and p.get("prior_attempt_outcomes"):
        p["prior_attempt_outcomes"].pop(0)
        decisions.append("removed_old_feedback")
    if size() > max_chars:
        p = {"schema": "guidance_packet/1", "mode_used": "exploratory", "groups": [],
             "recommendations": [], "fallback_reason": "guidance_budget_exhausted"}
        decisions.append("exploratory_due_to_budget")
    p["path_groups_sent_to_llm"] = len(p["groups"])
    return p, {"limit_chars": max_chars, "rendered_chars": len(json.dumps(p, sort_keys=True)),
               "decisions": decisions, "protected_sections": ["contract", "budgets", "source", "schema"]}
