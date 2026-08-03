"""Run the fictional ZorgVerhaal AI quality/latency evaluation set."""
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ai_service import AIUnavailable, next_plan
from app.main import incident_form_relevant


def load_inputs():
    scenarios = json.loads((ROOT / "evals" / "scenarios.json").read_text(encoding="utf-8"))
    bundle = json.loads((ROOT / "app" / "demo_assets" / "forms_bundle.json").read_text(encoding="utf-8"))["forms"]
    by_id = {form["id"]: form for form in bundle}
    daily = [by_id["05_daily_report"], by_id["06_shift_handover"]]
    catalog = [{"form_type": form["id"], "title": form["title"], "purpose": form.get("purpose", ""), "cadence": "incident", "safety_triggers": form.get("safety_triggers", [])} for form in bundle]
    return scenarios, daily, catalog, bundle


def main() -> int:
    scenarios, daily, catalog, bundle = load_inputs()
    failures, latencies, tokens = [], [], []
    for scenario in scenarios:
        try:
            result = next_plan(
                narrative=scenario["narrative"], conversation=[],
                client_context="Fictieve cliënt; communiceert met korte zinnen.",
                goals=[{"goal_id": "goal-1", "title": "Zelf keuzes aangeven", "description": ""}],
                form_schema={"formal_required": ["narrative"], "contextual_topics": []},
                fill_forms=daily + [form for form in bundle if incident_form_relevant(form["id"], scenario["narrative"])], form_catalog=catalog,
                registration_context={"client_name": "Testcliënt", "client_reference": "test-1", "datetime": "2026-08-03T14:00+02:00", "author": "test@example.nl", "author_role": "caregiver", "location": "Testlocatie", "current_shift": "Dagdienst", "next_shift": "Avonddienst"},
                user_id="eval-user",
            )
        except AIUnavailable as exc:
            failures.append(f"{scenario['id']}: API-fout {exc.code}")
            continue
        plan, telemetry = result.plan, result.telemetry
        suggested = {item.form_type for item in plan.suggested_forms}
        checks = [
            (telemetry["route"] == scenario["expected_route"], f"route={telemetry['route']}"),
            (plan.risk_level.value in scenario["allowed_risks"], f"risk={plan.risk_level.value}"),
            (len(plan.clarification_questions) <= scenario["max_questions"], f"questions={len(plan.clarification_questions)}"),
            (set(scenario["required_suggestions"]).issubset(suggested | {draft.form_type for draft in plan.form_drafts}), f"suggested_or_filled={sorted(suggested | {draft.form_type for draft in plan.form_drafts})}"),
        ]
        failed = [message for ok, message in checks if not ok]
        if failed: failures.append(f"{scenario['id']}: " + ", ".join(failed))
        latencies.append(telemetry["latency_ms"])
        tokens.append(telemetry.get("total_tokens", 0))
        print(json.dumps({"id": scenario["id"], "model": telemetry["model"], "latency_ms": telemetry["latency_ms"], "tokens": telemetry.get("total_tokens"), "state": plan.state, "risk": plan.risk_level.value, "questions": len(plan.clarification_questions), "suggested": sorted(suggested), "passed": not failed}, ensure_ascii=False))

    print(json.dumps({"scenarios": len(scenarios), "failures": failures, "median_latency_ms": round(statistics.median(latencies)) if latencies else None, "total_tokens": sum(tokens)}, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
