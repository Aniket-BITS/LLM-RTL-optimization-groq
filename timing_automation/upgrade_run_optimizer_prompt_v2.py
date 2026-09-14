#!/usr/bin/env python3
"""Safely upgrade the user's CURRENT run_optimizer.py for deferred prompt rendering.

This patcher intentionally edits only four areas:
  1) imports prompt_renderer + optimizer_history
  2) canonicalizes Groq transport to plain text mode with hidden reasoning
  3) replaces old prompt_system/prompt_user preflight with in-memory rendering + budget gate
  4) appends compact iteration history during transaction finalization

All formal, lint, ORFS, metrics, evaluation, and other locally newer changes are preserved.
"""
from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path


MARKER = "nEBULA_PROMPT_V2_PATCH"

IMPORT_BLOCK = f'''\n\n# {MARKER}: deferred compact prompt rendering + append-only history\ntry:\n    from prompt_renderer import (\n        ContextBudgetError,\n        PromptRenderError,\n        render_request_for_llm,\n    )\n    from optimizer_history import append_history_record_from_transaction\nexcept ImportError:\n    from timing_automation.prompt_renderer import (\n        ContextBudgetError,\n        PromptRenderError,\n        render_request_for_llm,\n    )\n    from timing_automation.optimizer_history import append_history_record_from_transaction\n'''

CALL_GROQ = '''def call_groq(\n    *,\n    system_prompt: str,\n    user_prompt: str,\n    model: str,\n    max_tokens: int,\n    reasoning_effort: str,\n    response_schema_path: Optional[Path] = None,\n) -> str:\n    \"\"\"Generate one candidate. Local extraction/linting is authoritative.\"\"\"\n    try:\n        from groq import Groq\n    except ImportError as exc:\n        raise OptimizerError(\n            \"Groq SDK is missing. Install it in the active environment \"\n            \"with `python -m pip install groq`.\"\n        ) from exc\n\n    if not os.environ.get(\"GROQ_API_KEY\"):\n        raise OptimizerError(\"GROQ_API_KEY is not set\")\n\n    client = Groq()\n    response = client.chat.completions.create(\n        model=model,\n        messages=[\n            {\"role\": \"system\", \"content\": system_prompt},\n            {\"role\": \"user\", \"content\": user_prompt},\n        ],\n        reasoning_effort=reasoning_effort,\n        reasoning_format=\"hidden\",\n        max_completion_tokens=max_tokens,\n    )\n\n    content = response.choices[0].message.content\n    if not content or not content.strip():\n        raise OptimizerError(\"Groq returned an empty candidate response\")\n    return content.strip()\n'''

PREFLIGHT_BLOCK = '''        rendered_prompt = None\n        if candidate_path is None:\n            if not Path(response_schema_path).is_file():\n                raise OptimizerError(\n                    f\"response schema not found: {response_schema_path}\"\n                )\n\n            try:\n                rendered_prompt = render_request_for_llm(\n                    Path(request_path),\n                    response_schema_override=Path(response_schema_path),\n                    completion_reserve_tokens=max_tokens,\n                )\n            except ContextBudgetError as exc:\n                raise OptimizerError(str(exc)) from exc\n            except PromptRenderError as exc:\n                raise OptimizerError(\n                    f\"prompt rendering failed: {exc}\"\n                ) from exc\n\n            transaction[\"context_budget\"] = dict(\n                rendered_prompt.context_budget\n            )\n            cb = transaction[\"context_budget\"]\n            print(\n                \"    Context      : \"\n                f\"input~{cb['estimated_input_tokens']} + \"\n                f\"reserve={cb['completion_reserve_tokens']} / \"\n                f\"budget={cb['service_model_token_budget']} tokens\"\n            )\n            print(\n                \"    Compaction   : \"\n                + (\"applied\" if rendered_prompt.compaction_applied else \"not needed\")\n            )\n\n        transaction[\"unit_id\"] = request.get(\"unit_id\")\n        rendering_inputs = request.get(\"rendering_inputs\") or {}\n        current_iteration = rendering_inputs.get(\"current_iteration\") or {}\n        history_ref = rendering_inputs.get(\"iteration_history\") or {}\n        if isinstance(history_ref.get(\"path\"), str):\n            transaction[\"iteration_history\"] = {\n                \"path\": history_ref.get(\"path\"),\n                \"iteration_number\": current_iteration.get(\"iteration_number\"),\n                \"append_only\": True,\n            }\n'''

GROQ_CALL = '''            if rendered_prompt is None:\n                raise OptimizerError(\n                    \"internal error: compact prompt was not rendered during preflight\"\n                )\n\n            raw = call_groq(\n                system_prompt=rendered_prompt.system_prompt,\n                user_prompt=rendered_prompt.user_prompt,\n                model=model,\n                max_tokens=max_tokens,\n                reasoning_effort=reasoning_effort,\n                response_schema_path=Path(response_schema_path),\n            )\n'''

HISTORY_FINALIZE = '''    # nEBULA prompt-v2 append-only history.  History logging is compact and\n    # must not alter an already-determined optimization verdict.\n    try:\n        appended = append_history_record_from_transaction(transaction)\n        if isinstance(transaction.get(\"iteration_history\"), dict):\n            transaction[\"iteration_history\"][\"appended\"] = bool(appended)\n    except Exception as exc:\n        if isinstance(transaction.get(\"iteration_history\"), dict):\n            transaction[\"iteration_history\"][\"append_error\"] = str(exc)\n\n'''


def patch(path: Path, *, backup: bool = True) -> Path:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"run_optimizer.py not found: {path}")
    source = path.read_text(encoding="utf-8")
    if MARKER in source:
        print(f"Already patched: {path}")
        return path

    # Import block after local import try/except and before constants/helpers.
    anchor = "\n\n# =============================================================================\n# Paths / constants"
    if anchor not in source:
        # Older/current variants may use a different next section title.
        m = re.search(r"\n\n# ={50,}\n# (?:Paths / constants|Constants|Exceptions|Generic helpers)", source)
        if not m:
            raise RuntimeError("could not locate insertion point after local imports")
        source = source[:m.start()] + IMPORT_BLOCK + source[m.start():]
    else:
        source = source.replace(anchor, IMPORT_BLOCK + anchor, 1)

    # Canonical Groq function; ends immediately before Artifact lifecycle.
    groq_pattern = re.compile(
        r"def call_groq\([\s\S]*?\n\n(?=# ={50,}\n# Artifact lifecycle)",
        re.MULTILINE,
    )
    source, n = groq_pattern.subn(CALL_GROQ + "\n\n", source, count=1)
    if n != 1:
        raise RuntimeError("could not replace call_groq() safely")

    # Replace old generated-candidate preflight block only.
    preflight_pattern = re.compile(
        r"        if candidate_path is None:\n[\s\S]*?\n        transaction\[\"design\"\] = \{",
        re.MULTILINE,
    )
    m = preflight_pattern.search(source)
    if not m:
        raise RuntimeError("could not locate candidate prompt preflight block")
    source = source[:m.start()] + PREFLIGHT_BLOCK + "\n        transaction[\"design\"] = {" + source[m.end():]

    # Replace the raw call immediately under the Groq generation branch.
    call_pattern = re.compile(
        r"            raw = call_groq\(\n[\s\S]*?\n            \)\n\n            candidate_artifact\.write_text",
        re.MULTILINE,
    )
    m = call_pattern.search(source)
    if not m:
        raise RuntimeError("could not locate Groq candidate call")
    source = source[:m.start()] + GROQ_CALL + "\n            candidate_artifact.write_text" + source[m.end():]

    # Append history after verdict/note are fixed but before transaction is written.
    hist_anchor = '''    transaction["finished_utc"] = utc_now()\n    transaction["verdict"] = verdict\n    transaction["note"] = note\n\n'''
    if hist_anchor not in source:
        raise RuntimeError("could not locate finish_transaction verdict block")
    source = source.replace(hist_anchor, hist_anchor + HISTORY_FINALIZE, 1)

    # Safe default if the file still carries the old 5000-token completion default.
    source = source.replace(
        '''    parser.add_argument(\n        "--max-tokens",\n        type=int,\n        default=5000,\n    )''',
        '''    parser.add_argument(\n        "--max-tokens",\n        type=int,\n        default=2048,\n    )''',
        1,
    )

    if backup:
        backup_path = path.with_suffix(path.suffix + ".pre_prompt_v2")
        if not backup_path.exists():
            shutil.copy2(path, backup_path)
            print(f"Backup: {backup_path}")

    path.write_text(source, encoding="utf-8")
    print(f"Patched: {path}")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="Patch current nEBULA run_optimizer.py for prompt v2")
    ap.add_argument(
        "--file",
        default="timing_automation/run_optimizer.py",
        help="Current local run_optimizer.py to patch in place",
    )
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()
    try:
        patch(Path(args.file), backup=not args.no_backup)
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
