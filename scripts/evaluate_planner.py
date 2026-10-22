"""Fixed synthetic Planner comparison. --live opts into the configured model API.

Run from any directory with backend/.venv/bin/python scripts/evaluate_planner.py
--live --repeats 2 --output docs/evaluations/planner-live-2026-10-21.json
No ChatAgent, specialist, database or repository tools are executed.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
from app.core.config import settings
from app.schemas.workflow import WorkflowSpec
from app.services.agents.planner_agent import PlannerAgent
from app.services.llm.prompts import PLANNER_AGENT_PROMPT, PLANNER_PROMPT_VERSION


def assess(spec_data: dict, case: dict, context: dict) -> dict:
    spec = WorkflowSpec.model_validate(spec_data)
    tasks = {task.id: task for task in spec.tasks}
    actual = set(tasks)
    expected = set(case["tasks"])
    edges_ok = all(before in tasks[after].dependencies for before, after in case.get("edges", []) if after in tasks)
    catalog = PlannerAgent(use_llm=False)._task_catalog(context)
    scope_ok = all(
        task.id in catalog
        and task.agent_name == catalog[task.id].agent_name
        and task.claim.target_type == catalog[task.id].claim.target_type
        and task.claim.target_id == catalog[task.id].claim.target_id
        and set(task.claim.allowed_sources) <= set(catalog[task.id].claim.allowed_sources)
        and task.claim.write_intent == catalog[task.id].claim.write_intent
        for task in spec.tasks
    )
    try:
        PlannerAgent._validate_graph(tasks)
        graph_ok = len(tasks) == len(spec.tasks)
    except ValueError:
        graph_ok = False
    safety_ok = not context.get("requires_safety") or (
        "safety_review" in tasks and spec.requires_human_confirmation
    )
    invariant_ok = scope_ok and graph_ok and safety_ok
    return {
        "passed": actual == expected and edges_ok and invariant_ok,
        "exact_tasks": actual == expected, "dependencies_match": edges_ok,
        "invariants_passed": invariant_ok, "extra_tasks": sorted(actual - expected),
        "missing_tasks": sorted(expected - actual),
    }


async def main(args) -> None:
    fixture_path = ROOT / "docs/evaluations/planner-cases-v1.json"
    fixture_bytes = fixture_path.read_bytes()
    fixture = json.loads(fixture_bytes)
    if args.live and not settings.llm_api_key:
        raise SystemExit("Configured model API key is missing; live comparison was not run.")
    cases = fixture["cases"][:args.limit] if args.limit else fixture["cases"]
    semaphore = asyncio.Semaphore(2)

    async def evaluate(case: dict, repeat: int) -> dict:
        context = {**fixture["context"], **case.get("context", {})}
        baseline = await PlannerAgent(use_llm=False).run({"message": case["message"]}, context)
        async with semaphore:
            candidate = await PlannerAgent(use_llm=args.live).run({"message": case["message"]}, context)
        result = {
            "case_id": case["id"], "repeat": repeat, "message": case["message"], "context": context,
            "expected_tasks": case["tasks"], "expected_edges": case.get("edges", []),
            "baseline": {"assessment": assess(baseline, case, context), "spec": baseline},
            "candidate": {"assessment": assess(candidate, case, context), "spec": candidate},
        }
        print(json.dumps({"case": case["id"], "repeat": repeat,
                          "baseline_pass": result["baseline"]["assessment"]["passed"],
                          "candidate_pass": result["candidate"]["assessment"]["passed"],
                          "trace": candidate["planner_trace"]}, ensure_ascii=False), flush=True)
        return result

    results = await asyncio.gather(*(evaluate(case, repeat) for repeat in range(1, args.repeats + 1) for case in cases))
    summary = {}
    for variant in ("baseline", "candidate"):
        traces = [result[variant]["spec"]["planner_trace"] for result in results]
        latencies = [trace["latency_ms"] for trace in traces]
        summary[variant] = {
            "runs": len(results),
            "passed": sum(result[variant]["assessment"]["passed"] for result in results),
            "invariants_passed": sum(result[variant]["assessment"]["invariants_passed"] for result in results),
            "llm_accepted": sum(trace["source"] == "llm_with_rules" for trace in traces),
            "fallbacks": sum(trace["source"] == "rules_fallback" for trace in traces),
            "latency_median_ms": statistics.median(latencies), "latency_max_ms": max(latencies),
            "provider_prompt_tokens": sum(trace.get("usage", {}).get("prompt_tokens", 0) for trace in traces),
            "provider_completion_tokens": sum(trace.get("usage", {}).get("completion_tokens", 0) for trace in traces),
        }
    output = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "live_model" if args.live else "rules_only_smoke",
        "model": settings.llm_model, "temperature": 0, "max_tokens": 4096,
        "thinking": "disabled_for_official_deepseek", "timeout_seconds": 20, "concurrency": 2,
        "prompt_version": PLANNER_PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(PLANNER_AGENT_PROMPT.encode()).hexdigest(),
        "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "working_tree_contains_implementation_changes": True,
        "scope": "Synthetic planning only; no diagnosis quality or general accuracy claim.",
        "summary": summary, "results": results,
    }
    output_path = ROOT / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(output_path), "summary": summary}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", default="docs/evaluations/planner-smoke.json")
    asyncio.run(main(parser.parse_args()))
