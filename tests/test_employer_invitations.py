from io import BytesIO
import uuid
import zipfile

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import Organization, User
from app.security import hash_password, issue_token


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

        letter = browser.get(f"/api/platform/employer-invitations/{invitation['id']}/letter", params={"token": invitation["token"]})
        assert letter.status_code == 200
        assert letter.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        with zipfile.ZipFile(BytesIO(letter.content)) as package:
            relationships = package.read("word/_rels/document.xml.rels").decode("utf-8")
            document_xml = package.read("word/document.xml").decode("utf-8")
            footer_xml = package.read("word/footer1.xml").decode("utf-8")
        assert f"employer_join={invitation['token']}" in relationships
        assert "Demo-Zorg" in document_xml
        assert "CommunityTools" in footer_xml

        info = browser.get(f"/api/employer-join/{invitation['token']}")
        assert info.status_code == 200
        assert info.json()["organization_name"].startswith("Zorggroep")

        browser.cookies.clear()
        joined = browser.post(f"/api/employer-join/{invitation['token']}", json={"name": "Eva de Vries", "email": employer_email, "password": "veilig-werkgever-2026"})
        assert joined.status_code == 200
        assert browser.get("/api/me").json()["role"] == "org_admin"
        seeded = browser.post("/api/organization/seed-demo")
        assert seeded.status_code == 200
        assert browser.get("/api/clients").json()
        assert browser.get(f"/api/employer-join/{invitation['token']}").status_code == 410
