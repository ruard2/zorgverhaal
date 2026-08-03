import json
from openai import OpenAI
from .config import get_settings
from .legal_policy import LEGAL_POLICY_NL, SYSTEM_PROMPT
from .schemas import AIPlan


settings = get_settings()


class AIUnavailable(RuntimeError):
    pass


def next_plan(*, narrative: str, conversation: list[dict], client_context: str, goals: list[dict], form_schema: dict, fill_forms: list[dict] | None = None, form_catalog: list[dict] | None = None) -> AIPlan:
    if not settings.openai_api_key:
        raise AIUnavailable("OPENAI_API_KEY ontbreekt")
    client = OpenAI(api_key=settings.openai_api_key)
    payload = {
        "bronverhaal": narrative,
        "eerdere_verheldering": conversation,
        "clientcontext": client_context,
        "actieve_zorgdoelen": goals,
        "organisatie_basisformulier": form_schema,
        "te_vullen_formulieren": fill_forms or [],
        "formulier_catalogus": form_catalog or [],
        "opdracht": "Vul via form_drafts de dagelijkse verplichte formulieren en de door de situatie opgeroepen formulieren uit te_vullen_formulieren in met uitsluitend feitelijke informatie. Doel is een kwalitatief, bruikbaar formulier met zo min mogelijk vragen. Stel via next_question alleen een vraag als een verplicht veld echt nodig is voor veiligheid of wettelijke verplichting en niet af te leiden valt; vraag nooit naar optionele velden. Bij een routinedienst zonder bijzonderheden: geen vragen en direct state='ready'. Zet state op 'ready' zodra alle verplichte velden 'filled' of 'unknown' zijn. Signaleer via suggested_forms overige relevante formulieren uit de catalogus.",
    }
    response = client.responses.parse(
        model=settings.openai_model,
        store=False,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + LEGAL_POLICY_NL},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        text_format=AIPlan,
    )
    if not response.output_parsed:
        raise AIUnavailable("AI gaf geen bruikbare gestructureerde uitvoer")
    return response.output_parsed
