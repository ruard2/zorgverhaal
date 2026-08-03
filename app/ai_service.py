import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import openai
from openai import OpenAI

from .config import get_settings
from .legal_policy import SYSTEM_PROMPT
from .schemas import AIPlan, ClarificationQuestion


settings = get_settings()
logger = logging.getLogger(__name__)


class AIUnavailable(RuntimeError):
    def __init__(self, message: str, *, code: str = "unavailable"):
        super().__init__(message)
        self.code = code


@dataclass
class AIResult:
    plan: AIPlan
    telemetry: dict[str, Any]


def choose_model(narrative: str, user_id: str = "anonymous") -> tuple[str, str]:
    # Rapportages blijven op het lichte model. De AI mag risico's en extra
    # formulieren signaleren, maar schaalt zichzelf niet op naar Sol.
    percentage = max(0, min(100, settings.openai_report_experiment_percent))
    bucket = int(hashlib.sha256(f"{user_id}:{narrative}".encode("utf-8")).hexdigest()[:8], 16) % 100
    if settings.openai_report_experiment_model and bucket < percentage:
        return settings.openai_report_experiment_model, "reporting_experiment"
    return settings.openai_report_model, "reporting_control"


def privacy_safe_identifier(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]


def transcribe_audio(content: bytes, *, file_name: str, mime_type: str, user_id: str) -> tuple[str, dict[str, Any]]:
    if not settings.openai_api_key:
        raise AIUnavailable("Spraakherkenning is niet geconfigureerd", code="not_configured")
    started = time.perf_counter()
    try:
        with OpenAI(api_key=settings.openai_api_key, timeout=45.0, max_retries=0) as client:
            transcript = client.audio.transcriptions.create(
                model=settings.openai_transcription_model,
                file=(file_name or "spraak.webm", content, mime_type or "audio/webm"),
                language="nl",
                prompt="Nederlandse zorgrapportage. Behoud cliëntnamen, medicatienamen en letterlijke observaties zo nauwkeurig mogelijk.",
            )
    except openai.APITimeoutError as exc:
        raise AIUnavailable("Het verwerken van de opname duurde te lang. Probeer opnieuw of typ de tekst.", code="transcription_timeout") from exc
    except openai.APIError as exc:
        raise AIUnavailable("De opname kon tijdelijk niet worden verwerkt.", code="transcription_api") from exc
    text_value = getattr(transcript, "text", "").strip()
    if not text_value:
        raise AIUnavailable("Er werd geen verstaanbare spraak gevonden.", code="empty_transcript")
    return text_value, {"model": settings.openai_transcription_model, "latency_ms": round((time.perf_counter() - started) * 1000), "user_hash": privacy_safe_identifier(user_id)}


def _known_registration_value(field_id: str, label: str, context: dict) -> str | None:
    normalized_id = field_id.casefold()
    if normalized_id in {"client_id", "client_reference", "client_name"}:
        return str(context.get("client_reference") or context.get("client_name") or "")
    if normalized_id in {"event_datetime", "datetime", "date", "time"}:
        return str(context.get("datetime") or "")
    if normalized_id in {"author", "employee", "caregiver"}:
        return " · ".join(filter(None, (str(context.get("author") or ""), str(context.get("author_role") or ""))))
    if normalized_id in {"location", "shift", "service"}:
        return " · ".join(filter(None, (str(context.get("location") or ""), str(context.get("current_shift") or ""))))
    if normalized_id in {"to_shift", "recipient_shift", "handover_to"}:
        return str(context.get("next_shift") or "")
    if normalized_id in {"time_spent", "care_minutes", "review_confirmed", "human_confirmation"}:
        return "Wordt bij de eindcontrole ingevuld"
    return None


def _apply_explicit_routine_defaults(plan: AIPlan, narrative: str) -> None:
    text = narrative.casefold()
    explicit_routine = any(term in text for term in ("geen bijzonderheden", "zonder bijzonderheden")) and any(term in text for term in ("rustig", "normaal", "gebruikelijk", "volgens het normale"))
    if not explicit_routine or plan.risk_level != "none" or plan.red_flags:
        return
    for draft in plan.form_drafts:
        for field in draft.fields:
            if field.status != "needs_input":
                continue
            haystack = f"{field.field_id} {field.label}".casefold()
            if "actie" in haystack or "risico" in haystack:
                field.value = "Geen afwijking, risico of vervolgactie gemeld."
            elif "ondersteun" in haystack or "intervent" in haystack:
                field.value = "Geen afwijkende ondersteuning gemeld."
            elif "reactie" in haystack or "effect" in haystack:
                field.value = "Geen afwijkende reactie of bijzonder effect gemeld."
            else:
                field.value = "Geen bijzonderheid of afwijking gemeld."
            field.status = "filled"


def _consolidate_questions(plan: AIPlan, maximum: int = 4) -> None:
    if len(plan.clarification_questions) <= maximum:
        return
    kept = plan.clarification_questions[: maximum - 1]
    overflow = plan.clarification_questions[maximum - 1 :]
    combined = overflow[0]
    combined.id = "combined_remaining"
    combined.question = "Kun je ook deze ontbrekende punten toelichten? " + " ".join(f"{index + 1}. {question.question}" for index, question in enumerate(overflow))
    combined.why = "Deze informatie is nodig om de resterende verplichte velden in één keer compleet te maken."
    combined.answer_type = "free_text"
    combined.answer_options = []
    combined.field_ids = list(dict.fromkeys(field_id for question in overflow for field_id in question.field_ids))
    plan.clarification_questions = kept + [combined]


def apply_deterministic_fields(plan: AIPlan, registration_context: dict, narrative: str = "", required_fields: dict[str, dict[str, str]] | None = None, confirmed_unknown_ids: set[str] | None = None) -> AIPlan:
    required_fields = required_fields or {}
    confirmed_unknown_ids = confirmed_unknown_ids or set()
    app_managed_ids: set[str] = set()
    for draft in plan.form_drafts:
        for field in draft.fields:
            value = _known_registration_value(field.field_id, field.label, registration_context)
            if value is not None:
                app_managed_ids.add(field.field_id)
                field.value = value
                field.status = "filled"
        draft.complete = all(field.status != "needs_input" for field in draft.fields)

    existing_question_fields = {field_id for question in plan.clarification_questions for field_id in question.field_ids}
    for draft in plan.form_drafts:
        required = required_fields.get(draft.form_type, {})
        for field in draft.fields:
            if field.field_id not in required or field.field_id in app_managed_ids or field.field_id in confirmed_unknown_ids:
                continue
            if field.status == "unknown" or not field.value.strip():
                field.status = "needs_input"
                field.value = ""
                if field.field_id not in existing_question_fields:
                    plan.clarification_questions.append(ClarificationQuestion(id=f"required_{draft.form_type}_{field.field_id}", field_ids=[field.field_id], question=f"Wat moet worden vastgelegd bij ‘{field.label}’ ?", why=f"Dit is een verplicht veld in {draft.title}."))
                    existing_question_fields.add(field.field_id)
        draft.complete = all(field.status != "needs_input" for field in draft.fields)

    if app_managed_ids:
        plan.missing_information = [item for item in plan.missing_information if not any(field_id.casefold() in item.casefold() for field_id in app_managed_ids)]
        retained_questions = []
        for question in plan.clarification_questions:
            had_field_ids = bool(question.field_ids)
            question.field_ids = [field_id for field_id in question.field_ids if field_id not in app_managed_ids]
            if had_field_ids and not question.field_ids:
                continue
            if not question.field_ids and any(term in question.question.casefold() for term in ("zorgmin", "menselijke bevestiging", "cliënt", "datum", "tijdstip", "welke dienst")):
                continue
            retained_questions.append(question)
        plan.clarification_questions = retained_questions

    _apply_explicit_routine_defaults(plan, narrative)
    needs_input = any(field.status == "needs_input" for draft in plan.form_drafts for field in draft.fields)
    if not needs_input and plan.state != "urgent":
        plan.clarification_questions = []
    _consolidate_questions(plan)
    if not needs_input and not plan.clarification_questions and plan.state != "urgent":
        plan.state = "ready"
        plan.next_question = None
        plan.why_this_question = None
        plan.answer_options = []
    elif plan.clarification_questions:
        if plan.state != "urgent":
            plan.state = "ask"
        first = plan.clarification_questions[0]
        plan.next_question = first.question
        plan.why_this_question = first.why
        plan.answer_type = first.answer_type
        plan.answer_options = first.answer_options
    elif needs_input:
        raise AIUnavailable("De AI leverde een onvolledig concept zonder verhelderingsvragen.", code="inconsistent_output")
    return plan


def _usage_dict(response) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    if not usage:
        return {}
    data = usage.model_dump(mode="json") if hasattr(usage, "model_dump") else {}
    return {
        "input_tokens": data.get("input_tokens", 0),
        "output_tokens": data.get("output_tokens", 0),
        "total_tokens": data.get("total_tokens", 0),
        "input_tokens_details": data.get("input_tokens_details", {}),
        "output_tokens_details": data.get("output_tokens_details", {}),
    }


def next_plan(*, narrative: str, conversation: list[dict], client_context: str, goals: list[dict], form_schema: dict, fill_forms: list[dict] | None = None, form_catalog: list[dict] | None = None, registration_context: dict | None = None, user_id: str = "anonymous") -> AIResult:
    if not settings.openai_api_key:
        raise AIUnavailable("AI is niet geconfigureerd", code="not_configured")

    registration_context = registration_context or {}
    model, route = choose_model(narrative, user_id)
    payload = {
        "bronverhaal": narrative,
        "registratie_context": registration_context,
        "organisatie_basisformulier": form_schema,
    }
    if conversation:
        payload["eerdere_verheldering"] = conversation
    if client_context:
        payload["clientcontext"] = client_context
    if goals:
        payload["actieve_zorgdoelen"] = goals
    if fill_forms:
        payload["te_vullen_formulieren"] = fill_forms
    if form_catalog:
        payload["formulier_catalogus"] = form_catalog
    started = time.perf_counter()
    try:
        with OpenAI(api_key=settings.openai_api_key, timeout=settings.openai_timeout_seconds, max_retries=settings.openai_max_retries) as client:
            response = client.responses.parse(
                model=model,
                store=False,
                reasoning={"effort": settings.openai_report_reasoning_effort},
                safety_identifier=privacy_safe_identifier(user_id),
                input=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
                ],
                text_format=AIPlan,
            )
    except openai.APITimeoutError as exc:
        raise AIUnavailable("De AI deed er te lang over. Je invoer is bewaard; probeer het opnieuw.", code="timeout") from exc
    except openai.RateLimitError as exc:
        raise AIUnavailable("De AI is tijdelijk te druk. Probeer het over een ogenblik opnieuw.", code="rate_limit") from exc
    except openai.AuthenticationError as exc:
        raise AIUnavailable("De AI-configuratie is ongeldig.", code="authentication") from exc
    except openai.APIConnectionError as exc:
        raise AIUnavailable("De AI is tijdelijk niet bereikbaar. Je invoer is bewaard.", code="connection") from exc
    except openai.APIStatusError as exc:
        logger.warning(
            "OpenAI reporting request rejected: status=%s code=%s type=%s param=%s model=%s",
            exc.status_code,
            getattr(exc, "code", None),
            getattr(exc, "type", None),
            getattr(exc, "param", None),
            model,
        )
        raise AIUnavailable("De AI kon deze rapportage tijdelijk niet verwerken.", code=f"api_{exc.status_code}") from exc

    if not response.output_parsed:
        raise AIUnavailable("De AI gaf geen bruikbaar concept. Probeer het opnieuw.", code="invalid_output")

    required_fields = {form.get("form_type", ""): {field.get("id", ""): field.get("label", field.get("id", "")) for section in form.get("sections", []) for field in section.get("fields", []) if field.get("required")} for form in (fill_forms or [])}
    confirmed_unknown_ids = {field_id for item in conversation if str(item.get("answer", "")).casefold().strip() == "niet bekend" for field_id in item.get("field_ids", [])}
    plan = apply_deterministic_fields(response.output_parsed, registration_context, narrative, required_fields, confirmed_unknown_ids)
    telemetry = {
        "model": model,
        "route": route,
        "reasoning_effort": settings.openai_report_reasoning_effort,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "response_id": getattr(response, "id", None),
        **_usage_dict(response),
    }
    return AIResult(plan=plan, telemetry=telemetry)
