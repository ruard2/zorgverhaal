import json
from openai import OpenAI
from .config import get_settings
from .legal_policy import LEGAL_POLICY_NL, SYSTEM_PROMPT
from .schemas import AIPlan


settings = get_settings()


class AIUnavailable(RuntimeError):
    pass


def next_plan(*, narrative: str, conversation: list[dict], client_context: str, goals: list[dict], form_schema: dict) -> AIPlan:
    if not settings.openai_api_key:
        raise AIUnavailable("OPENAI_API_KEY ontbreekt")
    client = OpenAI(api_key=settings.openai_api_key)
    payload = {
        "bronverhaal": narrative,
        "eerdere_verheldering": conversation,
        "clientcontext": client_context,
        "actieve_zorgdoelen": goals,
        "organisatie_basisformulier": form_schema,
        "opdracht": "Beoordeel de huidige volledigheid en geef exact één volgende stap volgens het schema.",
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
