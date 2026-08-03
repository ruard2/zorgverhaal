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


def test_routine_and_standard_incident_use_terra_but_acute_signal_uses_sol():
    settings = get_settings()
    assert choose_model("De dienst verliep rustig")[0] == settings.openai_model
    assert choose_model("Er was een medicatiefout")[0] == settings.openai_model
    assert choose_model("Cliënt was ernstig benauwd en reageerde nauwelijks")[0] == settings.openai_complex_model
    assert choose_model("Cliënt is vermist")[1] == "complex_signal"


def test_normal_pain_medication_stays_on_the_fast_routine_route():
    settings = get_settings()
    model, route = choose_model("Cliënt had pijn en kreeg volgens afspraak pijnmedicatie")
    assert model == settings.openai_model
    assert route == "routine"
    assert choose_model("De medicatie was vergeten")[1] == "complex_signal"


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


def test_only_relevant_incident_forms_are_preselected():
    from app.main import incident_form_relevant

    assert incident_form_relevant("12_medication_deviation", "De medicatie lag nog in het bakje")
    assert incident_form_relevant("11_wzd_resistance", "Cliënt zei nee en trok haar arm terug")
    assert not incident_form_relevant("12_medication_deviation", "De dienst verliep rustig")
    assert not incident_form_relevant("12_medication_deviation", "Cliënt kreeg volgens afspraak pijnmedicatie")
