from app.ai_service import apply_deterministic_fields, choose_model
from app.config import get_settings
from app.legal_policy import SYSTEM_PROMPT
from app.schemas import AIPlan, ClarificationQuestion, FilledField, FormDraft, RiskLevel


def minimal_plan(**overrides):
    data = dict(
        state="ask",
        risk_level=RiskLevel.none,
        answer_type="free_text",
        draft_report="Concept",
        human_review_note="Controleer het concept",
        clarification_questions=[],
        form_drafts=[],
    )
    data.update(overrides)
    return AIPlan(**data)


def test_prompt_is_lean_and_has_one_question_strategy():
    prompt = SYSTEM_PROMPT.casefold()
    assert "alle noodzakelijke vragen" in prompt
    assert "maximaal 4" in prompt
    assert "vraag nooit naar" in prompt
    assert "voeg nooit" in prompt
    assert "menselijke eindcontrole" in prompt
    assert "maximaal één volgende vraag" not in prompt


def test_all_reports_use_configured_light_route_and_never_self_escalate_to_sol():
    settings = get_settings()
    allowed = {settings.openai_report_model, settings.openai_report_experiment_model}
    assert choose_model("De dienst verliep rustig", "user-1")[0] in allowed
    assert choose_model("Er was een medicatiefout", "user-1")[0] in allowed
    assert choose_model("Cliënt was ernstig benauwd en reageerde nauwelijks", "user-1")[0] in allowed
    assert choose_model("Cliënt is vermist", "user-1")[1] in {"reporting_control", "reporting_experiment"}


def test_daily_reporting_never_sends_the_extended_legal_prompt():
    import inspect
    from app.ai_service import next_plan

    source = inspect.getsource(next_plan)
    assert "LEGAL_POLICY_NL" not in source
    assert "SYSTEM_PROMPT" in source


def test_normal_pain_medication_stays_on_the_fast_routine_route():
    settings = get_settings()
    model, route = choose_model("Cliënt had pijn en kreeg volgens afspraak pijnmedicatie", "user-2")
    assert model in {settings.openai_report_model, settings.openai_report_experiment_model}
    assert route in {"reporting_control", "reporting_experiment"}
    assert choose_model("De medicatie was vergeten", "user-2")[0] in {settings.openai_report_model, settings.openai_report_experiment_model}


def test_model_experiment_assignment_is_stable_for_the_same_report():
    first = choose_model("Dezelfde rapportage", "dezelfde-gebruiker")
    assert choose_model("Dezelfde rapportage", "dezelfde-gebruiker") == first


def test_registration_fields_are_deterministic_and_not_questions():
    plan = minimal_plan(
        clarification_questions=[ClarificationQuestion(id="meta", field_ids=["client_name"], question="Welke cliënt?", why="Verplicht")],
        form_drafts=[FormDraft(form_type="daily", title="Dagrapportage", fields=[FilledField(field_id="client_name", label="Cliëntnaam", status="needs_input")])],
    )
    result = apply_deterministic_fields(plan, {"client_name": "Testcliënt"})
    assert result.form_drafts[0].fields[0].value == "Testcliënt"
    assert result.form_drafts[0].fields[0].status == "filled"
    assert result.clarification_questions == []
    assert result.state == "ready"


def test_content_fields_are_never_overwritten_as_registration_fields():
    plan = minimal_plan(
        form_drafts=[FormDraft(form_type="daily", title="Dagrapportage", fields=[
            FilledField(field_id="client_statement", label="Letterlijke cliëntuitspraak", value="Ik wil een ijsje", status="filled"),
            FilledField(field_id="changes", label="Veranderingen sinds vorige dienst", value="Henk was rustiger", status="filled"),
            FilledField(field_id="client_response", label="Reactie en effect", value="Henk trok later bij", status="filled"),
        ])],
    )
    result = apply_deterministic_fields(plan, {"client_name": "Marieke de Boer", "current_shift": "Dagdienst"})
    assert [field.value for field in result.form_drafts[0].fields] == ["Ik wil een ijsje", "Henk was rustiger", "Henk trok later bij"]


def test_required_unknown_fields_generate_questions():
    plan = minimal_plan(
        state="ready",
        form_drafts=[FormDraft(form_type="shift_handover", title="Dienstoverdracht", fields=[FilledField(field_id="changes", label="Belangrijke veranderingen", value="Niet beschreven.", status="unknown")])],
    )
    result = apply_deterministic_fields(plan, {}, required_fields={"shift_handover": {"changes": "Belangrijke veranderingen"}})
    assert result.state == "ask"
    assert result.form_drafts[0].fields[0].status == "needs_input"
    assert result.clarification_questions[0].field_ids == ["changes"]


def test_bot_placeholder_in_required_effect_field_becomes_a_human_question():
    plan = minimal_plan(
        state="ready",
        form_drafts=[FormDraft(form_type="daily_report", title="Dagrapportage", fields=[FilledField(field_id="client_response", label="Reactie en effect", value="Effect van de paracetamol is niet beschreven.", status="filled")])],
    )
    result = apply_deterministic_fields(plan, {}, required_fields={"daily_report": {"client_response": "Reactie en effect"}})
    assert result.state == "ask"
    assert result.form_drafts[0].fields[0].value == ""
    assert "Wat merkte je" in result.clarification_questions[0].question


def test_explicit_routine_closes_non_factual_required_gaps():
    plan = minimal_plan(
        clarification_questions=[ClarificationQuestion(id="support", field_ids=["support_given"], question="Welke ondersteuning?", why="Verplicht")],
        form_drafts=[FormDraft(form_type="daily", title="Dagrapportage", fields=[FilledField(field_id="support_given", label="Geboden ondersteuning", status="needs_input")])],
    )
    result = apply_deterministic_fields(plan, {}, "De dienst verliep rustig en normaal, zonder bijzonderheden.")
    assert result.state == "ready"
    assert result.clarification_questions == []
    assert result.form_drafts[0].fields[0].value == "Geen afwijkende ondersteuning gemeld."


def test_frontend_uses_structured_answers_and_readonly_preview():
    from pathlib import Path

    frontend = Path("static/app.js").read_text(encoding="utf-8")
    assert "question_id:q.id" in frontend
    assert "JSON.stringify({answers})" in frontend
    assert "formDraftsPreview(p)" in frontend
    assert "Bekijk het huidige AI-concept" in frontend
    assert 'api("/api/transcribe"' in frontend
    assert "MediaRecorder" in frontend
    assert "SpeechRecognition" not in frontend
    assert "DIRECT CONCEPT · GEWONE CODE" in frontend
    assert "submitNarrative(narrative)" in frontend


def test_only_relevant_incident_forms_are_preselected():
    from app.main import incident_form_relevant

    assert incident_form_relevant("12_medication_deviation", "De medicatie lag nog in het bakje")
    assert incident_form_relevant("11_wzd_resistance", "Cliënt zei nee en trok haar arm terug")
    assert not incident_form_relevant("12_medication_deviation", "De dienst verliep rustig")
    assert not incident_form_relevant("12_medication_deviation", "Cliënt kreeg volgens afspraak pijnmedicatie")


def test_incident_forms_are_suggestions_not_automatically_filled():
    import inspect
    from app.main import forms_to_fill

    source = inspect.getsource(forms_to_fill)
    assert 'form.cadence == "daily"' in source
    assert "incident_form_relevant(" not in source


def test_employee_selects_suggested_forms_without_an_ai_call():
    from pathlib import Path

    frontend = Path("static/app.js").read_text(encoding="utf-8")
    assert "data-suggest-form" in frontend
    assert "selectedSuggestedForms" in frontend
    assert "formFillPage(button.dataset.formId)" in frontend
    assert "earlyMinutes" in frontend
    assert "appManagedField" in frontend


def test_employee_home_and_targeted_ai_form_flow_are_available():
    from pathlib import Path

    frontend = Path("static/app.js").read_text(encoding="utf-8")
    assert "Nog te doen voor deze dag" in frontend
    assert "Cliënten onder je hoede" in frontend
    assert "Alle beschikbare formulieren" in frontend
    assert "chooseClientForForm" in frontend
    assert "targetedNarrativePage" in frontend
    assert "form_id:state.targetForm?.id||null" in frontend


def test_targeted_session_sends_only_the_employee_selected_form():
    import inspect
    from app.main import run_ai, start_session

    run_source = inspect.getsource(run_ai)
    start_source = inspect.getsource(start_session)
    assert 'item.get("kind") == "target_form"' in run_source
    assert "[compact_form(target_form)]" in run_source
    assert "form_catalog = [] if target_form" in run_source
    assert '"kind": "target_form"' in start_source


def test_every_library_form_has_specific_non_ai_preparation():
    import json
    from pathlib import Path
    from app.main import APP_MANAGED_FORM_FIELDS, FORM_PREPARATION_INTROS, form_preparation

    forms = json.loads(Path("app/demo_assets/forms_bundle.json").read_text(encoding="utf-8"))["forms"]
    assert len(forms) == 17
    assert {form["id"] for form in forms} == set(FORM_PREPARATION_INTROS)
    for form in forms:
        preparation = form_preparation(form["id"], form)
        assert preparation["intro"]
        assert preparation["groups"]
        exposed_labels = [label for group in preparation["groups"] for label in group["required"] + group["optional"]]
        managed_labels = [field.get("label") for section in form.get("sections", []) for field in section.get("fields", []) if field.get("id") in APP_MANAGED_FORM_FIELDS]
        assert not set(exposed_labels) & set(managed_labels)


def test_targeted_form_page_renders_simple_or_detailed_preparation_without_ai():
    from pathlib import Path

    frontend = Path("static/app.js").read_text(encoding="utf-8")
    assert "Wat moet je vertellen?" in frontend
    assert "Bekijk alle onderwerpen" in frontend
    assert "form.preparation" in frontend
