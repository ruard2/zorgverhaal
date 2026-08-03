import io
import json
import re
import time
from typing import Any

import openai
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from openai import OpenAI
from pypdf import PdfReader

from .ai_service import AIUnavailable, privacy_safe_identifier
from .config import get_settings
from .schemas import FormImportProposal


settings = get_settings()

FORM_IMPORT_PROMPT = r"""
Je zet één Nederlands zorgformulier exact om naar een digitaal formulierschema.

ABSOLUTE EISEN
- Neem titel, sectievolgorde, veldvolgorde, zichtbare veldlabels en antwoordopties letterlijk over uit de bron.
- Voeg geen velden, vragen, antwoordopties of verplichtingen toe die niet in de bron staan.
- Sla geen invulveld over. Instructietekst is geen invulveld.
- source_quote is voor elk veld een letterlijk, aaneengesloten bronfragment waarin het veld herkenbaar staat.
- Leid het technisch veldtype af uit de vorm: vrije regels -> textarea; één keuze -> select; meerdere aankruisopties -> multiselect; ja/nee-vak -> boolean; datum/tijd/getal alleen als dat duidelijk is.
- required=true alleen bij een expliciet sterretje, 'verplicht' of een ondubbelzinnige broninstructie. Anders false en noteer twijfel in uncertainties.
- Maak alleen technische snake_case-id's; zichtbare labels blijven exact gelijk.
- suggested_cadence is een voorstel: daily, incident of on_demand.
- Meld iedere onleesbare passage, dubbelzinnige tabel, ontbrekende optie of mogelijke OCR-/extractiefout in uncertainties.
- fidelity_note beschrijft kort wat wel en niet betrouwbaar uit de bron kon worden overgenomen.

Dit is uitsluitend een concept. Een mens vergelijkt het met het origineel vóór activering.
"""


def extract_document_text(content: bytes, mime_type: str, file_name: str) -> str:
    suffix = file_name.casefold().rsplit(".", 1)[-1] if "." in file_name else ""
    if mime_type == "application/pdf" or suffix == "pdf":
        reader = PdfReader(io.BytesIO(content))
        pages = []
        for number, page in enumerate(reader.pages, start=1):
            pages.append(f"\n--- PAGINA {number} ---\n{page.extract_text() or ''}")
        text = "".join(pages)
    elif mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" or suffix == "docx":
        document = Document(io.BytesIO(content))
        blocks = []
        for child in document.element.body.iterchildren():
            if child.tag.endswith("}p"):
                value = Paragraph(child, document).text.strip()
                if value: blocks.append(value)
            elif child.tag.endswith("}tbl"):
                table = Table(child, document)
                for row in table.rows:
                    cells = [re.sub(r"\s+", " ", cell.text).strip() for cell in row.cells]
                    blocks.append(" | ".join(cells))
        text = "\n".join(blocks)
    else:
        text = content.decode("utf-8", errors="replace")
    text = text.replace("\x00", "").strip()
    if len(text) < 20:
        raise ValueError("Het document bevat te weinig uitleesbare tekst")
    return text[:120_000]


def analyze_form(source_text: str, *, user_id: str) -> tuple[FormImportProposal, dict[str, Any]]:
    started = time.perf_counter()
    try:
        with OpenAI(api_key=settings.openai_api_key, timeout=90.0, max_retries=0) as client:
            response = client.responses.parse(
                model=settings.openai_complex_model,
                store=False,
                reasoning={"effort": "medium"},
                safety_identifier=privacy_safe_identifier(user_id),
                input=[
                    {"role": "system", "content": FORM_IMPORT_PROMPT},
                    {"role": "user", "content": source_text},
                ],
                text_format=FormImportProposal,
            )
    except openai.APITimeoutError as exc:
        raise AIUnavailable("De formulieranalyse duurde te lang. Probeer het opnieuw.", code="timeout") from exc
    except openai.APIError as exc:
        raise AIUnavailable("Het formulier kon tijdelijk niet worden geanalyseerd.", code="form_import_api") from exc
    if not response.output_parsed:
        raise AIUnavailable("De formulieranalyse leverde geen bruikbaar concept op.", code="invalid_output")
    usage = response.usage.model_dump(mode="json") if response.usage else {}
    telemetry = {
        "model": settings.openai_complex_model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "response_id": response.id,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }
    return response.output_parsed, telemetry


def fidelity_errors(proposal: FormImportProposal, source_text: str) -> list[str]:
    normalized_source = re.sub(r"\s+", " ", source_text).casefold()
    errors = []
    normalized_title = re.sub(r"\s+", " ", proposal.title).casefold()
    if normalized_title not in normalized_source:
        errors.append(f"Formuliertitel niet letterlijk teruggevonden: {proposal.title}")
    seen_ids = set()
    for section in proposal.sections:
        normalized_section = re.sub(r"\s+", " ", section.title).casefold()
        if normalized_section and normalized_section not in normalized_source:
            errors.append(f"Sectietitel niet letterlijk teruggevonden: {section.title}")
        for field in section.fields:
            if field.id in seen_ids:
                errors.append(f"Dubbele veld-ID: {field.id}")
            seen_ids.add(field.id)
            quote = re.sub(r"\s+", " ", field.source_quote).casefold()
            label = re.sub(r"\s+", " ", field.label).casefold()
            if quote not in normalized_source:
                errors.append(f"Bronfragment niet letterlijk teruggevonden bij: {field.label}")
            if label not in normalized_source and label not in quote:
                errors.append(f"Veldlabel niet letterlijk teruggevonden: {field.label}")
            for option in field.options:
                normalized_option = re.sub(r"\s+", " ", option).casefold()
                if normalized_option not in normalized_source:
                    errors.append(f"Antwoordoptie niet letterlijk teruggevonden bij {field.label}: {option}")
    return errors


def proposal_to_schema(proposal: FormImportProposal) -> dict:
    return {
        "purpose": proposal.purpose,
        "import_fidelity_note": proposal.fidelity_note,
        "import_uncertainties": proposal.uncertainties,
        "sections": [
            {"title": section.title, "fields": [{"id": field.id, "label": field.label, "type": field.type, "required": field.required, "options": field.options} for field in section.fields]}
            for section in proposal.sections
        ],
    }
