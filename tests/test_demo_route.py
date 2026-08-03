import uuid

from fastapi.testclient import TestClient

from app.ai_service import AIResult
from app.database import SessionLocal
from app.main import app
from app.models import Client, ClientAssignment, FormTemplate, Organization, User
from app.schemas import AIPlan, FilledField, FormDraft, FormSuggestion, RiskLevel
from app.security import encrypt_json, encrypt_text, hash_password, issue_token


STORIES = [
    "De dienst verliep rustig volgens het normale dagritme en er waren geen bijzonderheden.",
    "Samir kreeg volgens afspraak twee paracetamol. Daarna gaf hij aan dat de pijn afnam van 7 naar 2.",
    "De voorgeschreven medicatie lag nog in het bakje. Ik belde de verpleegkundige en gaf niets alsnog.",
]


def test_complete_demo_route_three_reports_and_linked_addendum(monkeypatch):
    suffix = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        organization = Organization(name=f"Demo {suffix}"); db.add(organization); db.flush()
        caregiver = User(organization_id=organization.id, email=f"zorg-{suffix}@example.nl", password_hash=hash_password("lang-demo-wachtwoord"), role="caregiver")
        admin = User(organization_id=organization.id, email=f"beheer-{suffix}@example.nl", password_hash=hash_password("lang-demo-wachtwoord"), role="org_admin")
        client = Client(organization_id=organization.id, display_name_enc=encrypt_text("Samir Demo"), context_enc=encrypt_text("Fictieve cliënt"))
        db.add_all([caregiver, admin, client]); db.flush()
        db.add(ClientAssignment(organization_id=organization.id, client_id=client.id, user_id=caregiver.id))
        template = FormTemplate(organization_id=organization.id, title="Dagrapportage", form_type="05_daily_report", version=1, cadence="daily", status="active", created_by=admin.id, schema_enc=encrypt_json({"sections": [{"title": "Rapportage", "fields": [{"id": "observations", "label": "Waarneembare feiten", "type": "textarea", "required": True}]}]}))
        db.add(template); db.commit()
        caregiver_token, admin_token, client_id = issue_token(caregiver), issue_token(admin), client.id

    def fake_plan(**kwargs):
        deviation = "bakje" in kwargs["narrative"]
        plan = AIPlan(state="ready", risk_level=RiskLevel.attention if deviation else RiskLevel.none, answer_type="free_text", draft_report=kwargs["narrative"], human_review_note="Controleer", form_drafts=[FormDraft(form_type="05_daily_report", title="Dagrapportage", complete=True, fields=[FilledField(field_id="observations", label="Waarneembare feiten", value=kwargs["narrative"], status="filled")])], suggested_forms=[FormSuggestion(form_type="12_medication_deviation", title="Medicatie-afwijking", reason="Medicatie bleef in het bakje") ] if deviation else [])
        return AIResult(plan=plan, telemetry={"model": "test", "route": "test", "latency_ms": 1, "total_tokens": 0})

    monkeypatch.setattr("app.main.next_plan", fake_plan)
    with TestClient(app) as browser:
        caregiver_headers = {"Cookie": f"zorg_session={caregiver_token}"}
        admin_headers = {"Cookie": f"zorg_session={admin_token}"}
        report_ids = []
        for story in STORIES:
            started = browser.post("/api/sessions", json={"client_id": client_id, "narrative": story}, headers=caregiver_headers)
            assert started.status_code == 200
            assert started.json()["plan"]["state"] == "ready"
            finalized = browser.post(f"/api/sessions/{started.json()['session_id']}/finalize", json={"report_text": story, "care_minutes": 30, "selected_goal_ids": [], "form_submissions": [{"form_type": "05_daily_report", "answers": {"observations": story}}], "human_review_confirmed": True}, headers=caregiver_headers)
            assert finalized.status_code == 200
            report_ids.append(finalized.json()["report_id"])

        workspace = browser.get("/api/organization/workspace", headers=admin_headers)
        assert workspace.status_code == 200
        assert {item["id"] for item in workspace.json()["recent_reports"]}.issuperset(report_ids)
        assert workspace.json()["metrics"]["reports_today"] >= 3
        assert workspace.json()["metrics"]["forms_today"] >= 3
        timeline = browser.get(f"/api/organization/clients/{client_id}/timeline", headers=admin_headers)
        assert sum(event["kind"] == "report" for event in timeline.json()["events"]) >= 3
        detail = browser.get(f"/api/organization/reports/{report_ids[-1]}", headers=admin_headers)
        assert detail.status_code == 200
        assert detail.json()["submissions"][0]["fields"][0]["label"] == "Waarneembare feiten"

        requested = browser.post(f"/api/organization/reports/{report_ids[-1]}/request-addition", json={"question": "Wat zag je na het contact met de verpleegkundige?"}, headers=admin_headers)
        assert requested.status_code == 200
        review_id = requested.json()["id"]
        tasks = browser.get("/api/my/review-requests", headers=caregiver_headers)
        assert review_id in {item["id"] for item in tasks.json()}
        answered = browser.post(f"/api/my/review-requests/{review_id}/addendum", json={"text": "Samir bleef rustig en ik zag geen zichtbare klachten."}, headers=caregiver_headers)
        assert answered.status_code == 200
        after_answer = browser.get("/api/organization/workspace", headers=admin_headers)
        assert after_answer.json()["metrics"]["answers_to_review"] >= 1
        reviewed_detail = browser.get(f"/api/organization/reports/{report_ids[-1]}", headers=admin_headers)
        assert reviewed_detail.json()["reviews"][0]["addendum"] == "Samir bleef rustig en ik zag geen zichtbare klachten."
        closed = browser.post(f"/api/organization/reviews/{review_id}/close", headers=admin_headers)
        assert closed.status_code == 200
        audit = browser.get("/api/organization/audit", headers=admin_headers)
        assert {item["action"] for item in audit.json()}.issuperset({"report.addition_requested", "report.addendum_submitted", "report.addition_closed"})
