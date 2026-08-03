import json
from openai import OpenAI
from .config import get_settings
from .legal_policy import LEGAL_POLICY_NL, SYSTEM_PROMPT
from .schemas import AIPlan


settings = get_settings()


class AIUnavailable(RuntimeError):
    pass


def next_plan(*, narrative: str, conversation: list[dict], client_context: str, goals: list[dict], form_schema: dict, fill_forms: list[dict] | None = None, form_catalog: list[dict] | None = None, registration_context: dict | None = None) -> AIPlan:
    if not settings.openai_api_key:
        raise AIUnavailable("OPENAI_API_KEY ontbreekt")
    client = OpenAI(api_key=settings.openai_api_key)
    payload = {
        "bronverhaal": narrative,
        "eerdere_verheldering": conversation,
        "clientcontext": client_context,
        "actieve_zorgdoelen": goals,
        "registratie_context": registration_context or {},
        "organisatie_basisformulier": form_schema,
        "te_vullen_formulieren": fill_forms or [],
        "formulier_catalogus": form_catalog or [],
        "opdracht": "Vul via form_drafts de dagelijkse verplichte formulieren en de door de situatie opgeroepen formulieren in namens de medewerker, in de eerste/directe persoon en met uitsluitend feitelijke informatie. Vul registratie-/metavelden (cliënt, datum en tijd, naam en functie medewerker, locatie/dienst) automatisch uit registratie_context; markeer die nooit als onbekend of needs_input. Doel is een kwalitatief, compleet formulier met zo min mogelijk vragen. Zet een verplicht inhoudelijk veld dat echt ontbreekt en niet af te leiden is op status 'needs_input' en stel daarover via next_question één vraag; state mag niet 'ready' zijn zolang er needs_input-velden zijn. Gebruik 'unknown' alleen als de medewerker desgevraagd aangeeft het niet te weten. Bij een routinedienst zonder bijzonderheden: vul de inhoud met het feitelijke normaalbeeld en stel geen vragen. Signaleer via suggested_forms overige relevante formulieren uit de catalogus.",
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
