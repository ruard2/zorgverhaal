"""Drie-call acceptatietest voor de volledige ZorgVerhaal-demonstratieroute."""
import json
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ai_service import AIUnavailable, next_plan

BOT_MARKERS = ("true", "false", "niet beschreven", "niet vermeld", "wordt bij de eindcontrole ingevuld", "org_admin", "caregiver")
UUID_PATTERN = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.I)
MANAGED_IDS = {"client_id", "client_reference", "client_name", "event_datetime", "datetime", "date", "time", "author", "employee", "caregiver", "location", "shift", "service", "to_shift", "recipient_shift", "handover_to", "time_spent", "care_minutes", "review_confirmed", "human_confirmation"}


def inputs():
    scenarios = json.loads((ROOT / "evals" / "scenarios.json").read_text(encoding="utf-8"))
    forms = json.loads((ROOT / "app" / "demo_assets" / "forms_bundle.json").read_text(encoding="utf-8"))["forms"]
    by_id = {form["id"]: form for form in forms}
    daily = [by_id["05_daily_report"], by_id["06_shift_handover"]]
    catalog = [{"form_type": form["id"], "title": form["title"], "purpose": form.get("purpose", ""), "cadence": "incident" if form["id"] not in {"05_daily_report", "06_shift_handover"} else "daily", "safety_triggers": form.get("safety_triggers", [])} for form in forms]
    return scenarios, daily, catalog


def visible_text(plan) -> str:
    values = [plan.draft_report]
    values += [field.value for draft in plan.form_drafts for field in draft.fields if field.field_id not in MANAGED_IDS]
    values += [item.reason for item in plan.suggested_forms]
    return "\n".join(values)


def duplicate_ratio(plan) -> float:
    paragraphs = [field.value.casefold().strip() for draft in plan.form_drafts for field in draft.fields if field.field_id not in MANAGED_IDS and len(field.value.strip()) > 25]
    return 0 if not paragraphs else 1 - len(set(paragraphs)) / len(paragraphs)


def main() -> int:
    scenarios, daily, catalog = inputs()
    selected_ids = set(sys.argv[1:])
    if selected_ids:
        scenarios = [scenario for scenario in scenarios if scenario["id"] in selected_ids]
    failures, results = [], []
    for scenario in scenarios:  # bewust precies één betaalde call per verhaal
        try:
            result = next_plan(
                narrative=scenario["narrative"], conversation=[],
                client_context="Fictieve demo-cliënt; communiceert in korte zinnen.",
                goals=[{"goal_id": "goal-demo-1", "title": "Zelf keuzes aangeven", "description": ""}],
                form_schema={"formal_required": ["narrative"], "contextual_topics": []},
                fill_forms=daily, form_catalog=[form for form in catalog if any(key in form["form_type"] for key in ({"medication"} if "medicatie" in scenario["narrative"].casefold() and "bakje" in scenario["narrative"].casefold() else set()))],
                registration_context={"client_name": "Demo Cliënt", "client_reference": "intern-id-niet-tonen", "datetime": "2026-08-03T14:00+02:00", "author": "Diana Stolper", "author_role": "Zorgmedewerker", "location": "Leo Zorg", "current_shift": "Dagdienst", "next_shift": "Avonddienst"},
                user_id="demo-acceptatie",
            )
        except AIUnavailable as exc:
            failures.append(f"{scenario['id']}: API-fout {exc.code} ({exc})")
            continue
        plan, telemetry = result.plan, result.telemetry
        text = visible_text(plan); folded = text.casefold()
        suggestions = {item.form_type for item in plan.suggested_forms}
        checks = [
            (plan.risk_level.value in scenario["allowed_risks"], f"risico={plan.risk_level.value}"),
            (len(plan.clarification_questions) <= scenario["max_questions"], f"vragen={len(plan.clarification_questions)}"),
            (set(scenario["required_suggestions"]).issubset(suggestions), f"suggesties={sorted(suggestions)}"),
            (not suggestions.intersection(scenario["forbidden_suggestions"]), f"onterechte_suggesties={sorted(suggestions)}"),
            (not UUID_PATTERN.search(text), "technische UUID zichtbaar"),
            (not any(re.search(rf"\b{re.escape(marker)}\b", folded) for marker in BOT_MARKERS), "bot- of systeemtaal zichtbaar"),
            (duplicate_ratio(plan) <= 0.20, f"herhaling={duplicate_ratio(plan):.0%}"),
        ]
        failed = [message for ok, message in checks if not ok]
        if failed: failures.append(f"{scenario['id']}: " + ", ".join(failed))
        item = {"id": scenario["id"], "model": telemetry["model"], "latency_ms": telemetry["latency_ms"], "tokens": telemetry.get("total_tokens", 0), "state": plan.state, "questions": [{"question": q.question, "fields": q.field_ids} for q in plan.clarification_questions], "suggested": sorted(suggestions), "passed": not failed, "failures": failed}
        results.append(item); print(json.dumps(item, ensure_ascii=False))
    summary = {"paid_ai_calls": len(results), "scenarios": len(scenarios), "passed": sum(item["passed"] for item in results), "failures": failures, "median_latency_ms": round(statistics.median(item["latency_ms"] for item in results)) if results else None, "total_tokens": sum(item["tokens"] for item in results)}
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
