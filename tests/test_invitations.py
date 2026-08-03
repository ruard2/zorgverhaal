import uuid

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import Organization, User
from app.security import hash_password, issue_token


def test_personal_invitation_creates_number_and_account():
    suffix = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        organization = Organization(name=f"Uitnodiging {suffix}")
        db.add(organization); db.flush()
        admin = User(organization_id=organization.id, email=f"beheer-{suffix}@example.nl", password_hash=hash_password("lang-demo-wachtwoord"), role="org_admin")
        db.add(admin); db.commit(); db.refresh(admin)
        admin_token = issue_token(admin)

    with TestClient(app) as browser:
        browser.cookies.set("zorg_session", admin_token)
        created = browser.post(
            "/api/organization/invitations",
            json={"employee_name": "Sanne de Vries", "email": f"sanne-{suffix}@example.nl"},
        )
        assert created.status_code == 200
        invitation = created.json()
        assert invitation["employee_number"].startswith("MW-")

        info = browser.get(f"/api/join/{invitation['token']}")
        assert info.status_code == 200
        assert info.json()["employee_name"] == "Sanne de Vries"
        assert info.json()["employee_number"] == invitation["employee_number"]

        joined = browser.post(f"/api/join/{invitation['token']}", json={"name": "Sanne de Vries", "email": f"sanne-{suffix}@example.nl", "password": "veilig-wachtwoord-2026"})
        assert joined.status_code == 200
        me = browser.get("/api/me")
        assert me.status_code == 200
        assert me.json()["name"] == "Sanne de Vries"
        assert me.json()["employee_number"] == invitation["employee_number"]

        assert browser.get(f"/api/join/{invitation['token']}").status_code == 410
