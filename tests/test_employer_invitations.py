from io import BytesIO
import uuid
import zipfile

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import FormTemplate, Organization, User
from app.security import encrypt_json, hash_password, issue_token, verify_password


def test_platform_owner_invites_employer_downloads_letter_and_employer_seeds_demo():
    suffix = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        platform_org = Organization(name=f"CommunityTools {suffix}")
        db.add(platform_org); db.flush()
        owner = User(organization_id=platform_org.id, email=f"owner-{suffix}@example.nl", password_hash=hash_password("lang-demo-wachtwoord"), role="platform_admin", display_name="Ruard")
        db.add(owner); db.commit(); db.refresh(owner)
        owner_token = issue_token(owner)

    employer_email = f"werkgever-{suffix}@example.nl"
    with TestClient(app) as browser:
        browser.cookies.set("zorg_session", owner_token)
        created = browser.post("/api/platform/employer-invitations", json={"organization_name": f"Zorggroep {suffix}", "contact_name": "Eva de Vries", "email": employer_email})
        assert created.status_code == 200
        invitation = created.json()
        assert invitation["email"] == employer_email
        with SessionLocal() as db:
            employer = db.query(User).filter(User.email == employer_email).one()
            assert employer.role == "org_admin"
            assert employer.must_change_password is True
            assert verify_password("verandermij", employer.password_hash)

        letter = browser.get(f"/api/platform/employer-invitations/{invitation['id']}/letter", params={"token": invitation["token"]})
        assert letter.status_code == 200
        assert letter.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        with zipfile.ZipFile(BytesIO(letter.content)) as package:
            relationships = package.read("word/_rels/document.xml.rels").decode("utf-8")
            document_xml = package.read("word/document.xml").decode("utf-8")
            footer_xml = package.read("word/footer1.xml").decode("utf-8")
        assert f"employer_join={invitation['token']}" in relationships
        assert "ZorgVlot" in document_xml
        assert employer_email in document_xml
        assert "verandermij" in document_xml
        assert "https://www.zorgvlot.nl" in document_xml
        assert "CommunityTools" in footer_xml

        info = browser.get(f"/api/employer-join/{invitation['token']}")
        assert info.status_code == 200
        assert info.json()["organization_name"].startswith("Zorggroep")

        browser.cookies.clear()
        joined = browser.post(f"/api/employer-join/{invitation['token']}", json={"email": employer_email, "password": "verandermij"})
        assert joined.status_code == 200
        assert joined.json()["must_change_password"] is True
        assert browser.get("/api/me").json()["must_change_password"] is True
        assert browser.get("/api/dashboard").status_code == 428
        changed = browser.post("/api/me/password", json={"password": "veilig-werkgever-2026"})
        assert changed.status_code == 200
        assert browser.get("/api/me").json()["must_change_password"] is False
        seeded = browser.post("/api/organization/seed-demo")
        assert seeded.status_code == 200
        assert browser.get("/api/clients").json()
        assert browser.get(f"/api/employer-join/{invitation['token']}").status_code == 410


def test_employer_can_use_homepage_login_and_must_replace_temporary_password():
    suffix = uuid.uuid4().hex[:8]
    employer_email = f"homepage-{suffix}@example.nl"
    with SessionLocal() as db:
        organization = Organization(name=f"Homepage Zorg {suffix}")
        db.add(organization); db.flush()
        db.add(User(organization_id=organization.id, email=employer_email, password_hash=hash_password("verandermij"), role="org_admin", display_name="Homepage Werkgever", must_change_password=True))
        db.commit()

    with TestClient(app) as browser:
        logged_in = browser.post("/api/login", json={"email": employer_email, "password": "verandermij"})
        assert logged_in.status_code == 200
        assert logged_in.json()["must_change_password"] is True
        assert browser.get("/api/dashboard").status_code == 428
        assert browser.post("/api/me/password", json={"password": "verandermij"}).status_code == 422
        assert browser.post("/api/me/password", json={"password": "nieuw-veilig-wachtwoord"}).status_code == 200
        browser.post("/api/logout")
        assert browser.post("/api/login", json={"email": employer_email, "password": "verandermij"}).status_code == 401
        assert browser.post("/api/login", json={"email": employer_email, "password": "nieuw-veilig-wachtwoord"}).status_code == 200


def test_company_can_self_register_from_public_homepage():
    suffix = uuid.uuid4().hex[:8]
    email = f"registratie-{suffix}@example.nl"
    payload = {
        "organization_name": f"Zelfzorg {suffix}",
        "contact_name": "Nieuwe Werkgever",
        "email": email,
        "password": "zelfgekozen-wachtwoord",
    }
    with TestClient(app) as browser:
        registered = browser.post("/api/register-company", json=payload)
        assert registered.status_code == 200
        assert registered.json()["must_change_password"] is False
        me = browser.get("/api/me")
        assert me.status_code == 200
        assert me.json()["email"] == email
        assert me.json()["role"] == "org_admin"
        assert browser.get("/api/dashboard").status_code == 200
        browser.post("/api/logout")
        assert browser.post("/api/login", json={"email": email, "password": payload["password"]}).status_code == 200
        assert browser.post("/api/register-company", json=payload).status_code == 409


def test_employer_can_prepare_uploaded_form_demo_with_fictive_client():
    suffix = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        organization = Organization(name=f"Formulierdemo {suffix}")
        db.add(organization); db.flush()
        employer = User(organization_id=organization.id, email=f"demo-{suffix}@example.nl", password_hash=hash_password("veilig-demo-wachtwoord"), role="org_admin")
        db.add(employer); db.flush()
        schema = {"purpose": "Demonstratie", "sections": [{"title": "Observatie", "fields": [{"id": "observatie", "label": "Observatie", "type": "textarea", "required": True, "options": []}]}]}
        form = FormTemplate(organization_id=organization.id, title="Geüpload observatieformulier", form_type=f"upload_{suffix}", version=1, schema_enc=encrypt_json(schema), cadence="on_demand", status="active", created_by=employer.id)
        db.add(form); db.commit(); db.refresh(employer); db.refresh(form)
        token = issue_token(employer); form_id = form.id

    with TestClient(app) as browser:
        browser.cookies.set("zorg_session", token)
        first = browser.post(f"/api/organization/forms/{form_id}/prepare-demo")
        assert first.status_code == 200
        assert first.json()["client_created"] is True
        assert first.json()["client_name"] == "Noor de Vries"
        assert "Noor" in first.json()["example_narrative"]
        second = browser.post(f"/api/organization/forms/{form_id}/prepare-demo")
        assert second.status_code == 200
        assert second.json()["client_created"] is False
        assert second.json()["client_id"] == first.json()["client_id"]
