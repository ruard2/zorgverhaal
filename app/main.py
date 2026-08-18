import base64
import hashlib
import json
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from .ai_service import AIUnavailable, apply_simple_answers_without_ai, next_plan, transcribe_audio
from .config import get_settings
from .database import Base, SessionLocal, engine
from .form_import_service import analyze_form, extract_document_text, fidelity_errors, proposal_to_schema
from .employer_invitation_document import build_employer_invitation_document
from .models import AuditLog, CareGoal, Client, ClientAssignment, DocumentUpload, EmployerInvitation, FormImportDraft, FormSubmission, FormTemplate, Invitation, Organization, OrganizationSettings, Reminder, Report, ReportAddendum, ReportReview, ReportingSession, User
from .schemas import AIPlan, AddendumIn, AnswerIn, AssignmentIn, ClientIn, CompanyRegistrationIn, DocumentStatusIn, EmployerInvitationIn, FinalizeIn, FormCadenceIn, FormImportActivateIn, FormModeIn, FormSubmitIn, InvitationIn, JoinIn, LoginIn, OrganizationIn, PasswordChangeIn, ReminderIn, ReviewRequestIn, ShiftSettingsIn, StartSessionIn
from .security import current_user, decrypt_json, decrypt_text, encrypt_json, encrypt_text, get_db, hash_password, issue_token, verify_password


settings = get_settings()
EMPLOYER_TEMPORARY_PASSWORD = "verandermij"
PUBLIC_HOMEPAGE_URL = "https://www.zorgvlot.nl"
DEFAULT_SHIFTS = [
    {"name": "Dagdienst", "starts_at": "07:00", "minimum_handover": "Bijzonderheden, afspraken en aandachtspunten voor de volgende dienst."},
    {"name": "Avonddienst", "starts_at": "15:00", "minimum_handover": "Bijzonderheden, afspraken en aandachtspunten voor de volgende dienst."},
    {"name": "Nachtdienst", "starts_at": "23:00", "minimum_handover": "Bijzonderheden, afspraken en aandachtspunten voor de volgende dienst."},
]


def professional_user_label(user: User | None) -> str:
    if not user:
        return "Onbekende medewerker"
    name = user.email.split("@", 1)[0].replace(".", " ").replace("_", " ").replace("-", " ").title()
    role = {"caregiver": "Zorgmedewerker", "org_admin": "Organisatiebeheerder", "platform_admin": "Platformbeheerder"}.get(user.role, "Medewerker")
    return f"{name} · {role}"


def organization_shifts(db: Session, organization_id: str) -> list[dict]:
    row = db.scalar(select(OrganizationSettings).where(OrganizationSettings.organization_id == organization_id))
    branding = decrypt_json(row.branding_enc) if row else {}
    shifts = branding.get("shifts") if isinstance(branding, dict) else None
    return shifts if isinstance(shifts, list) and shifts else DEFAULT_SHIFTS


def shift_context(db: Session, organization_id: str) -> dict:
    shifts = organization_shifts(db, organization_id)
    now_local = datetime.now(ZoneInfo("Europe/Amsterdam"))
    minute = now_local.hour * 60 + now_local.minute
    ordered = sorted(shifts, key=lambda s: tuple(map(int, s["starts_at"].split(":"))))
    current_index = max((i for i, s in enumerate(ordered) if tuple(map(int, s["starts_at"].split(":")))[0] * 60 + tuple(map(int, s["starts_at"].split(":")))[1] <= minute), default=len(ordered) - 1)
    current = ordered[current_index]
    following = ordered[(current_index + 1) % len(ordered)]
    return {"current_shift": current["name"], "next_shift": following["name"], "minimum_handover": current.get("minimum_handover", ""), "configured_shifts": ordered}


def seed() -> None:
    with SessionLocal() as db:
        existing = db.scalar(select(User).where(User.email == settings.bootstrap_admin_email.lower()))
        if existing:
            if existing.role == "admin":
                existing.role = "platform_admin"
                db.commit()
            return
        org = Organization(name=settings.bootstrap_org_name)
        db.add(org); db.flush()
        db.add(User(organization_id=org.id, email=settings.bootstrap_admin_email.lower(), password_hash=hash_password(settings.bootstrap_admin_password), role="platform_admin"))
        db.commit()


def migrate() -> None:
    # create_all voegt geen kolcommen toe aan bestaande tabellen; deze kolom kan op een
    # eerder aangemaakte form_templates-tabel nog ontbreken.
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            # Gunicorn start meerdere workers tegelijk. PostgreSQL serialiseert deze
            # kleine startupmigratie zodat workers niet dezelfde DDL uitvoeren.
            conn.execute(text("SELECT pg_advisory_xact_lock(828821473)"))
            Base.metadata.create_all(bind=conn)
            conn.execute(text("ALTER TABLE form_templates ADD COLUMN IF NOT EXISTS cadence VARCHAR(20) DEFAULT 'on_demand'"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS form_mode VARCHAR(20) DEFAULT 'ask'"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name VARCHAR(120) DEFAULT ''"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS employee_number VARCHAR(30) DEFAULT ''"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN DEFAULT FALSE"))
            conn.execute(text("ALTER TABLE invitations ADD COLUMN IF NOT EXISTS employee_name VARCHAR(120) DEFAULT ''"))
            conn.execute(text("ALTER TABLE invitations ADD COLUMN IF NOT EXISTS employee_number VARCHAR(30) DEFAULT ''"))
            conn.execute(text("ALTER TABLE invitations ADD COLUMN IF NOT EXISTS intended_email VARCHAR(320) DEFAULT ''"))
            conn.execute(text("ALTER TABLE form_submissions ADD COLUMN IF NOT EXISTS session_id VARCHAR(36) REFERENCES reporting_sessions(id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_form_submissions_session_id ON form_submissions (session_id)"))
        else:
            Base.metadata.create_all(bind=conn)
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(form_templates)"))}
            if "cadence" not in columns:
                conn.execute(text("ALTER TABLE form_templates ADD COLUMN cadence VARCHAR(20) DEFAULT 'on_demand'"))
            user_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(users)"))}
            if "form_mode" not in user_columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN form_mode VARCHAR(20) DEFAULT 'ask'"))
            for column, definition in (("display_name", "VARCHAR(120) DEFAULT ''"), ("employee_number", "VARCHAR(30) DEFAULT ''"), ("must_change_password", "BOOLEAN DEFAULT 0")):
                if column not in user_columns:
                    conn.execute(text(f"ALTER TABLE users ADD COLUMN {column} {definition}"))
            invitation_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(invitations)"))}
            for column, definition in (("employee_name", "VARCHAR(120) DEFAULT ''"), ("employee_number", "VARCHAR(30) DEFAULT ''"), ("intended_email", "VARCHAR(320) DEFAULT ''")):
                if column not in invitation_columns:
                    conn.execute(text(f"ALTER TABLE invitations ADD COLUMN {column} {definition}"))
            submission_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(form_submissions)"))}
            if "session_id" not in submission_columns:
                conn.execute(text("ALTER TABLE form_submissions ADD COLUMN session_id VARCHAR(36) REFERENCES reporting_sessions(id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_form_submissions_session_id ON form_submissions (session_id)"))
        # Bestaande definitieve demoformulieren zijn via het auditspoor al aan hun
        # rapportagesessie te herleiden. Vul die koppeling eenmalig brongetrouw aan.
        audit_rows = conn.execute(text("SELECT target_id, details FROM audit_logs WHERE action = 'form.submitted'"))
        for target_id, details in audit_rows:
            try:
                session_id = json.loads(details or "{}").get("session")
            except (TypeError, json.JSONDecodeError):
                session_id = None
            if session_id:
                conn.execute(text("UPDATE form_submissions SET session_id = :session_id WHERE id = :submission_id AND session_id IS NULL"), {"session_id": session_id, "submission_id": target_id})


@asynccontextmanager
async def lifespan(_: FastAPI):
    migrate()
    seed()
    yield


app = FastAPI(title="Demo-Zorg", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in settings.allowed_origins.split(",")], allow_credentials=True, allow_methods=["GET", "POST"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index(): return FileResponse("static/index.html")


@app.get("/overzicht")
@app.get("/clienten")
@app.get("/controle")
@app.get("/beheer")
@app.get("/beheer/formulier-uploaden")
@app.get("/audit")
def employer_portal_page(): return FileResponse("static/index.html")


@app.get("/demo/formulier/{form_id}")
def guided_form_demo_page(form_id: str): return FileResponse("static/index.html")


@app.get("/health")
def health(): return {"ok": True}


@app.post("/api/login")
def login(data: LoginIn, response: Response, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == data.email.lower()))
    if not user or not user.active or not verify_password(data.password, user.password_hash):
        raise HTTPException(401, "E-mailadres of wachtwoord klopt niet")
    pending_invitation = db.scalar(select(EmployerInvitation).where(EmployerInvitation.intended_email == user.email, EmployerInvitation.used_at.is_(None), EmployerInvitation.revoked.is_(False)).order_by(EmployerInvitation.created_at.desc()))
    if pending_invitation:
        pending_invitation.used_at = datetime.now(timezone.utc)
        db.commit()
    response.set_cookie("zorg_session", issue_token(user), httponly=True, secure=settings.cookie_secure, samesite="lax", max_age=43200)
    return {"ok": True, "role": user.role, "must_change_password": bool(user.must_change_password)}


@app.post("/api/register-company")
def register_company(data: CompanyRegistrationIn, response: Response, db: Session = Depends(get_db)):
    email = data.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "Dit e-mailadres bestaat al")
    organization = Organization(name=data.organization_name.strip())
    db.add(organization); db.flush()
    employer = User(
        organization_id=organization.id,
        email=email,
        password_hash=hash_password(data.password),
        role="org_admin",
        display_name=data.contact_name.strip(),
        must_change_password=False,
    )
    db.add(employer); db.flush()
    db.add(OrganizationSettings(organization_id=organization.id, care_types_enc=encrypt_json([]), branding_enc=encrypt_json({"product": "ZorgVlot", "developer": "CommunityTools"})))
    db.add(AuditLog(organization_id=organization.id, user_id=employer.id, action="company.self_registered", target_type="organization", target_id=organization.id))
    db.commit()
    response.set_cookie("zorg_session", issue_token(employer), httponly=True, secure=settings.cookie_secure, samesite="lax", max_age=43200)
    return {"ok": True, "role": employer.role, "must_change_password": False}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("zorg_session")
    return {"ok": True}


@app.get("/api/me")
def me(user: User = Depends(current_user)): return {"email": user.email, "name": user.display_name or user.email.split("@")[0], "employee_number": user.employee_number or "", "role": user.role, "form_mode": user.form_mode or "ask", "must_change_password": bool(user.must_change_password)}


@app.post("/api/me/password")
def change_my_password(data: PasswordChangeIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if data.password == EMPLOYER_TEMPORARY_PASSWORD:
        raise HTTPException(400, "Kies een ander wachtwoord dan het tijdelijke wachtwoord")
    user.password_hash = hash_password(data.password)
    user.must_change_password = False
    audit(db, user, "user.password_changed", "user", user.id, {"first_login": True})
    db.commit()
    return {"ok": True}


@app.post("/api/me/form-mode")
def set_my_form_mode(data: FormModeIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    user.form_mode = data.form_mode
    audit(db, user, "user.form_mode_changed", "user", user.id, {"form_mode": data.form_mode})
    db.commit()
    return {"form_mode": user.form_mode}


def require_role(user: User, *roles: str) -> None:
    if user.role not in roles:
        raise HTTPException(403, "Geen toegang tot dit onderdeel")


def audit(db: Session, user: User, action: str, target_type: str, target_id: str, details: dict | None = None):
    db.add(AuditLog(organization_id=user.organization_id, user_id=user.id, action=action, target_type=target_type, target_id=target_id, details=json.dumps(details or {}, ensure_ascii=False)))


def invitation_expired(invitation: Invitation) -> bool:
    expires = invitation.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires < datetime.now(timezone.utc)


@app.get("/api/dashboard")
def dashboard(db: Session = Depends(get_db), user: User = Depends(current_user)):
    org = db.get(Organization, user.organization_id)
    if user.role == "platform_admin":
        return {"role": user.role, "organization": org.name, "metrics": {
            "organizations": len(db.scalars(select(Organization)).all()) - 1,
            "documents_waiting": len(db.scalars(select(DocumentUpload).where(DocumentUpload.status.in_(["uploaded", "reviewing"]))).all()),
        }}
    return {"role": user.role, "organization": org.name, "metrics": {
        "clients": len(db.scalars(select(Client).where(Client.organization_id == user.organization_id, Client.active.is_(True))).all()),
        "staff": len(db.scalars(select(User).where(User.organization_id == user.organization_id, User.active.is_(True))).all()),
        "reminders": len(db.scalars(select(Reminder).where(Reminder.organization_id == user.organization_id, Reminder.status == "open")).all()),
    }}


@app.get("/api/platform/organizations")
def platform_organizations(db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "platform_admin")
    orgs = db.scalars(select(Organization).where(Organization.id != user.organization_id)).all()
    return [{"id": o.id, "name": o.name, "staff": len(db.scalars(select(User).where(User.organization_id == o.id)).all()), "documents": len(db.scalars(select(DocumentUpload).where(DocumentUpload.organization_id == o.id)).all())} for o in orgs]


@app.post("/api/platform/organizations")
def create_organization(data: OrganizationIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "platform_admin")
    if db.scalar(select(User).where(User.email == data.admin_email.lower())):
        raise HTTPException(409, "Dit e-mailadres bestaat al")
    org = Organization(name=data.name)
    db.add(org); db.flush()
    admin = User(organization_id=org.id, email=data.admin_email.lower(), password_hash=hash_password(data.admin_password), role="org_admin")
    db.add(admin); db.flush()
    db.add(OrganizationSettings(organization_id=org.id, care_types_enc=encrypt_json(["verstandelijke beperking", "dementiezorg"]), branding_enc=encrypt_json({})))
    audit(db, user, "organization.created", "organization", org.id, {"admin_email": data.admin_email.lower()})
    db.commit()
    return {"id": org.id, "name": org.name}


@app.post("/api/platform/employer-invitations")
def create_employer_invitation(data: EmployerInvitationIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "platform_admin")
    email = str(data.email).lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "Dit e-mailadres bestaat al")
    organization = Organization(name=data.organization_name.strip())
    db.add(organization); db.flush()
    employer = User(
        organization_id=organization.id,
        email=email,
        password_hash=hash_password(EMPLOYER_TEMPORARY_PASSWORD),
        role="org_admin",
        display_name=data.contact_name.strip(),
        must_change_password=True,
    )
    db.add(employer); db.flush()
    db.add(OrganizationSettings(organization_id=organization.id, care_types_enc=encrypt_json([]), branding_enc=encrypt_json({"product": "Demo-Zorg", "developer": "CommunityTools"})))
    token = secrets.token_urlsafe(32)
    invitation = EmployerInvitation(
        created_by=user.id,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        organization_name=data.organization_name.strip(),
        contact_name=data.contact_name.strip(),
        intended_email=email,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db.add(invitation); db.flush()
    audit(db, user, "employer_invitation.created", "employer_invitation", invitation.id, {"organization_name": invitation.organization_name, "employer_user_id": employer.id})
    db.commit()
    return {"id": invitation.id, "token": token, "organization_name": invitation.organization_name, "contact_name": invitation.contact_name, "email": email, "expires_at": invitation.expires_at.isoformat()}


@app.get("/api/platform/employer-invitations/{invitation_id}/letter")
def employer_invitation_letter(invitation_id: str, token: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "platform_admin")
    invitation = db.get(EmployerInvitation, invitation_id)
    if not invitation or invitation.created_by != user.id or invitation.token_hash != hashlib.sha256(token.encode()).hexdigest():
        raise HTTPException(404, "Uitnodiging niet gevonden")
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",", 1)[0].strip()
    forwarded_host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc)).split(",", 1)[0].strip()
    base_url = f"{forwarded_proto}://{forwarded_host}".rstrip("/")
    invite_url = f"{base_url}/?employer_join={token}"
    content = build_employer_invitation_document(invitation.organization_name, invitation.contact_name, invitation.intended_email, invite_url, PUBLIC_HOMEPAGE_URL)
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", invitation.organization_name).strip("-") or "werkgever"
    return Response(content=content, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers={"Content-Disposition": f'attachment; filename="ZorgVlot-uitnodiging-{safe_name}.docx"'})


@app.get("/api/employer-join/{token}")
def employer_invitation_info(token: str, db: Session = Depends(get_db)):
    invitation = db.scalar(select(EmployerInvitation).where(EmployerInvitation.token_hash == hashlib.sha256(token.encode()).hexdigest()))
    if not invitation or invitation.revoked or invitation.used_at or invitation_expired(invitation):
        raise HTTPException(410, "Uitnodiging is verlopen, gebruikt of ingetrokken")
    return {"organization_name": invitation.organization_name, "contact_name": invitation.contact_name, "intended_email": invitation.intended_email}


@app.post("/api/employer-join/{token}")
def join_as_employer(token: str, data: LoginIn, response: Response, db: Session = Depends(get_db)):
    invitation = db.scalar(select(EmployerInvitation).where(EmployerInvitation.token_hash == hashlib.sha256(token.encode()).hexdigest()))
    if not invitation or invitation.revoked or invitation.used_at or invitation_expired(invitation):
        raise HTTPException(410, "Uitnodiging is verlopen, gebruikt of ingetrokken")
    email = data.email.lower()
    if invitation.intended_email != email:
        raise HTTPException(409, "Gebruik het e-mailadres waarvoor deze uitnodiging is gemaakt")
    employer = db.scalar(select(User).where(User.email == email))
    if not employer or not employer.active or employer.role != "org_admin" or not verify_password(data.password, employer.password_hash):
        raise HTTPException(401, "E-mailadres of wachtwoord klopt niet")
    invitation.used_at = datetime.now(timezone.utc)
    db.add(AuditLog(organization_id=employer.organization_id, user_id=employer.id, action="employer_invitation.accepted", target_type="organization", target_id=employer.organization_id))
    db.commit()
    response.set_cookie("zorg_session", issue_token(employer), httponly=True, secure=settings.cookie_secure, samesite="lax", max_age=43200)
    return {"ok": True, "must_change_password": bool(employer.must_change_password)}


@app.get("/api/platform/documents")
def platform_documents(db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "platform_admin")
    docs = db.scalars(select(DocumentUpload).order_by(DocumentUpload.uploaded_at.desc())).all()
    result = []
    for d in docs:
        org = db.get(Organization, d.organization_id)
        result.append({"id": d.id, "organization": org.name, "file_name": decrypt_text(d.file_name_enc), "note": decrypt_text(d.note_enc), "status": d.status, "uploaded_at": d.uploaded_at.isoformat(), "admin_note": decrypt_text(d.admin_note_enc)})
    return result


@app.post("/api/platform/documents/{document_id}/status")
def set_document_status(document_id: str, data: DocumentStatusIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "platform_admin")
    doc = db.get(DocumentUpload, document_id)
    if not doc: raise HTTPException(404, "Document niet gevonden")
    doc.status = data.status; doc.admin_note_enc = encrypt_text(data.admin_note)
    audit(db, user, "document.status_changed", "document", doc.id, {"status": data.status})
    db.commit(); return {"ok": True}


@app.get("/api/platform/documents/{document_id}/download")
def download_document(document_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "platform_admin")
    doc = db.get(DocumentUpload, document_id)
    if not doc: raise HTTPException(404, "Document niet gevonden")
    raw = base64.b64decode(decrypt_text(doc.content_enc))
    return Response(content=raw, media_type=doc.mime_type, headers={"Content-Disposition": f'attachment; filename="{decrypt_text(doc.file_name_enc).replace(chr(34), "")}"'})


@app.get("/api/clients")
def clients(db: Session = Depends(get_db), user: User = Depends(current_user)):
    query = select(Client).where(Client.organization_id == user.organization_id, Client.active.is_(True))
    if user.role == "caregiver":
        assigned_ids = db.scalars(select(ClientAssignment.client_id).where(ClientAssignment.user_id == user.id)).all()
        query = query.where(Client.id.in_(assigned_ids))
    rows = db.scalars(query).all()
    return [{"id": c.id, "display_name": decrypt_text(c.display_name_enc), "context": decrypt_text(c.context_enc), "goals": [{"id": g.id, "title": decrypt_text(g.title_enc)} for g in c.goals if g.active]} for c in rows]


@app.post("/api/clients")
def create_client(data: ClientIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    client = Client(organization_id=user.organization_id, display_name_enc=encrypt_text(data.display_name), context_enc=encrypt_text(data.context))
    db.add(client); db.flush()
    for goal in data.goals: db.add(CareGoal(client_id=client.id, title_enc=encrypt_text(goal), description_enc=encrypt_text("")))
    db.add(AuditLog(organization_id=user.organization_id, user_id=user.id, action="client.created", target_type="client", target_id=client.id))
    db.commit()
    return {"id": client.id}


@app.get("/api/organization/staff")
def staff(db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    rows = db.scalars(select(User).where(User.organization_id == user.organization_id, User.active.is_(True))).all()
    return [{"id": u.id, "email": u.email, "name": u.display_name or u.email.split("@")[0], "employee_number": u.employee_number or "", "role": u.role, "assigned_client_ids": db.scalars(select(ClientAssignment.client_id).where(ClientAssignment.user_id == u.id)).all()} for u in rows]


@app.post("/api/organization/assignments")
def assign_client(data: AssignmentIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    worker = db.get(User, data.user_id); client = db.get(Client, data.client_id)
    if not worker or not client or worker.organization_id != user.organization_id or client.organization_id != user.organization_id:
        raise HTTPException(404, "Medewerker of cliënt niet gevonden")
    existing = db.scalar(select(ClientAssignment).where(ClientAssignment.client_id == data.client_id, ClientAssignment.user_id == data.user_id))
    if not existing: db.add(ClientAssignment(organization_id=user.organization_id, client_id=data.client_id, user_id=data.user_id))
    audit(db, user, "client.assigned", "client", data.client_id, {"user_id": data.user_id}); db.commit()
    return {"ok": True}


def next_employee_number(db: Session, organization_id: str) -> str:
    values = [value for value in db.scalars(select(User.employee_number).where(User.organization_id == organization_id)).all() if value]
    values += [value for value in db.scalars(select(Invitation.employee_number).where(Invitation.organization_id == organization_id)).all() if value]
    sequence = max([int(value.rsplit("-", 1)[-1]) for value in values if value.rsplit("-", 1)[-1].isdigit()] or [0]) + 1
    return f"MW-{sequence:04d}"


@app.post("/api/organization/invitations")
def create_invitation(data: InvitationIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    token = secrets.token_urlsafe(32)
    number = next_employee_number(db, user.organization_id)
    invitation = Invitation(organization_id=user.organization_id, created_by=user.id, token_hash=hashlib.sha256(token.encode()).hexdigest(), role="caregiver", expires_at=datetime.now(timezone.utc) + timedelta(days=7), max_uses=1, employee_name=data.employee_name.strip(), employee_number=number, intended_email=str(data.email or "").lower())
    db.add(invitation); db.flush(); audit(db, user, "invitation.created", "invitation", invitation.id, {"employee_number": number}); db.commit()
    return {"id": invitation.id, "token": token, "employee_name": invitation.employee_name, "employee_number": number, "expires_at": invitation.expires_at.isoformat()}


@app.get("/api/organization/invitations")
def list_invitations(db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    rows = db.scalars(select(Invitation).where(Invitation.organization_id == user.organization_id).order_by(Invitation.created_at.desc())).all()
    return [{"id": i.id, "employee_name": i.employee_name or "Nieuwe medewerker", "employee_number": i.employee_number or "", "intended_email": i.intended_email or "", "expires_at": i.expires_at.isoformat(), "uses": i.uses, "max_uses": i.max_uses, "revoked": i.revoked, "expired": invitation_expired(i)} for i in rows]


@app.post("/api/organization/invitations/{invitation_id}/revoke")
def revoke_invitation(invitation_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    invitation = db.get(Invitation, invitation_id)
    if not invitation or invitation.organization_id != user.organization_id: raise HTTPException(404, "Uitnodiging niet gevonden")
    invitation.revoked = True; audit(db, user, "invitation.revoked", "invitation", invitation.id); db.commit(); return {"ok": True}


@app.get("/api/join/{token}")
def invitation_info(token: str, db: Session = Depends(get_db)):
    invitation = db.scalar(select(Invitation).where(Invitation.token_hash == hashlib.sha256(token.encode()).hexdigest()))
    if not invitation or invitation.revoked or invitation.uses >= invitation.max_uses or invitation_expired(invitation): raise HTTPException(410, "Uitnodiging is verlopen of ingetrokken")
    org = db.get(Organization, invitation.organization_id)
    return {"organization": org.name, "role": invitation.role, "employee_name": invitation.employee_name, "employee_number": invitation.employee_number, "intended_email": invitation.intended_email}


@app.post("/api/join/{token}")
def join_organization(token: str, data: JoinIn, response: Response, db: Session = Depends(get_db)):
    invitation = db.scalar(select(Invitation).where(Invitation.token_hash == hashlib.sha256(token.encode()).hexdigest()))
    if not invitation or invitation.revoked or invitation.uses >= invitation.max_uses or invitation_expired(invitation): raise HTTPException(410, "Uitnodiging is verlopen of ingetrokken")
    if db.scalar(select(User).where(User.email == data.email.lower())): raise HTTPException(409, "Dit e-mailadres bestaat al")
    if invitation.intended_email and invitation.intended_email != data.email.lower(): raise HTTPException(409, "Gebruik het e-mailadres waarvoor deze uitnodiging is gemaakt")
    worker = User(organization_id=invitation.organization_id, email=data.email.lower(), password_hash=hash_password(data.password), role=invitation.role, display_name=data.name.strip() or invitation.employee_name, employee_number=invitation.employee_number)
    db.add(worker); db.flush(); invitation.uses += 1
    db.add(AuditLog(organization_id=invitation.organization_id, user_id=worker.id, action="invitation.accepted", target_type="user", target_id=worker.id))
    db.commit(); response.set_cookie("zorg_session", issue_token(worker), httponly=True, secure=settings.cookie_secure, samesite="lax", max_age=43200)
    return {"ok": True}


@app.post("/api/organization/documents")
async def upload_document(file: UploadFile = File(...), note: str = Form(default=""), db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    content = await file.read()
    if len(content) > 5 * 1024 * 1024: raise HTTPException(413, "Document is groter dan 5 MB")
    allowed = {"application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "text/plain", "application/json"}
    if file.content_type not in allowed: raise HTTPException(415, "Gebruik PDF, DOCX, TXT of JSON")
    doc = DocumentUpload(organization_id=user.organization_id, uploaded_by=user.id, file_name_enc=encrypt_text(file.filename or "document"), mime_type=file.content_type or "application/octet-stream", content_enc=encrypt_text(base64.b64encode(content).decode()), note_enc=encrypt_text(note), admin_note_enc=encrypt_text(""))
    db.add(doc); db.flush(); audit(db, user, "document.uploaded", "document", doc.id); db.commit()
    return {"id": doc.id, "status": doc.status}


@app.get("/api/organization/documents")
def organization_documents(db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    docs = db.scalars(select(DocumentUpload).where(DocumentUpload.organization_id == user.organization_id).order_by(DocumentUpload.uploaded_at.desc())).all()
    return [{"id": d.id, "file_name": decrypt_text(d.file_name_enc), "note": decrypt_text(d.note_enc), "status": d.status, "admin_note": decrypt_text(d.admin_note_enc), "uploaded_at": d.uploaded_at.isoformat()} for d in docs]


@app.post("/api/organization/documents/{document_id}/analyze-form")
def analyze_uploaded_form(document_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    document = db.get(DocumentUpload, document_id)
    if not document or document.organization_id != user.organization_id:
        raise HTTPException(404, "Document niet gevonden")
    existing = db.scalar(select(FormImportDraft).where(FormImportDraft.document_id == document.id))
    if existing and existing.status == "concept":
        return {"import_id": existing.id, "proposal": decrypt_json(existing.proposal_enc), "telemetry": decrypt_json(existing.telemetry_enc)}
    if existing:
        raise HTTPException(409, "Dit document is al als formulier verwerkt")
    try:
        content = base64.b64decode(decrypt_text(document.content_enc))
        source_text = extract_document_text(content, document.mime_type, decrypt_text(document.file_name_enc))
        proposal, telemetry = analyze_form(source_text, user_id=user.id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except AIUnavailable as exc:
        raise HTTPException(503, {"message": str(exc), "code": exc.code}) from exc
    draft = FormImportDraft(organization_id=user.organization_id, document_id=document.id, created_by=user.id, source_text_enc=encrypt_text(source_text), proposal_enc=encrypt_json(proposal.model_dump(mode="json")), telemetry_enc=encrypt_json(telemetry), status="concept")
    db.add(draft); db.flush(); document.status = "concept_ready"
    audit(db, user, "form_import.analyzed", "form_import", draft.id, telemetry)
    db.commit()
    return {"import_id": draft.id, "proposal": proposal, "telemetry": telemetry}


@app.get("/api/organization/form-imports")
def list_form_imports(db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    rows = db.scalars(select(FormImportDraft).where(FormImportDraft.organization_id == user.organization_id).order_by(FormImportDraft.created_at.desc())).all()
    return [{"id": row.id, "document_id": row.document_id, "status": row.status, "proposal": decrypt_json(row.proposal_enc), "telemetry": decrypt_json(row.telemetry_enc), "created_at": row.created_at.isoformat()} for row in rows]


@app.post("/api/organization/form-imports/{import_id}/activate")
def activate_form_import(import_id: str, data: FormImportActivateIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    draft = db.get(FormImportDraft, import_id)
    if not draft or draft.organization_id != user.organization_id:
        raise HTTPException(404, "Formulierconcept niet gevonden")
    if draft.status == "active":
        saved_proposal = decrypt_json(draft.proposal_enc)
        form_type = saved_proposal.get("suggested_form_type") if isinstance(saved_proposal, dict) else None
        active_form = db.scalar(select(FormTemplate).where(FormTemplate.organization_id == user.organization_id, FormTemplate.form_type == form_type, FormTemplate.status == "active").order_by(FormTemplate.version.desc()))
        if not active_form:
            raise HTTPException(409, "Dit formulier is al verwerkt maar niet meer actief")
        return {"ok": True, "already_active": True, "form": form_payload(active_form)}
    if not data.human_review_confirmed:
        raise HTTPException(422, "Vergelijking met het originele formulier is verplicht")
    errors = fidelity_errors(data.proposal, decrypt_text(draft.source_text_enc))
    if errors:
        raise HTTPException(422, {"message": "Het concept is niet identiek genoeg aan de uitgelezen bron.", "errors": errors[:20]})
    form_type = data.proposal.suggested_form_type
    active_forms = db.scalars(select(FormTemplate).where(FormTemplate.organization_id == user.organization_id, FormTemplate.form_type == form_type, FormTemplate.status == "active")).all()
    prior_forms = db.scalars(select(FormTemplate).where(FormTemplate.organization_id == user.organization_id, FormTemplate.form_type == form_type)).all()
    for active in active_forms: active.status = "archived"
    template = FormTemplate(organization_id=user.organization_id, title=data.proposal.title, form_type=form_type, version=len(prior_forms) + 1, schema_enc=encrypt_json(proposal_to_schema(data.proposal)), cadence=data.proposal.suggested_cadence, status="active", created_by=user.id)
    db.add(template); db.flush()
    draft.proposal_enc = encrypt_json(data.proposal.model_dump(mode="json")); draft.status = "active"
    audit(db, user, "form_import.activated", "form", template.id, {"import_id": draft.id, "form_type": form_type, "version": template.version, "cadence": template.cadence})
    db.commit()
    return {"ok": True, "form": form_payload(template)}


@app.post("/api/organization/forms/{form_id}/prepare-demo")
def prepare_form_demo(form_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    form = db.get(FormTemplate, form_id)
    if not form or form.organization_id != user.organization_id or form.status != "active" or form.cadence == "disabled":
        raise HTTPException(404, "Actief formulier niet gevonden")
    active_clients = db.scalars(select(Client).where(Client.organization_id == user.organization_id, Client.active.is_(True)).order_by(Client.id)).all()
    client = next((item for item in active_clients if decrypt_text(item.context_enc).startswith("Fictieve democliënt.")), None)
    created = False
    if not client:
        client = Client(
            organization_id=user.organization_id,
            display_name_enc=encrypt_text("Noor de Vries"),
            context_enc=encrypt_text("Fictieve democliënt. Noor ontvangt ondersteuning bij dagelijkse structuur en persoonlijke verzorging. Gebruik uitsluitend verzonnen gegevens."),
        )
        db.add(client); db.flush()
        db.add(CareGoal(client_id=client.id, title_enc=encrypt_text("Dagelijkse structuur behouden"), description_enc=encrypt_text("Fictief demonstratiedoel")))
        db.add(CareGoal(client_id=client.id, title_enc=encrypt_text("Zelfstandigheid bij persoonlijke verzorging"), description_enc=encrypt_text("Fictief demonstratiedoel")))
        created = True
    client_name = decrypt_text(client.display_name_enc)
    example = f"{client_name} was vanmorgen rustig maar had extra uitleg nodig bij de persoonlijke verzorging. Met korte aanwijzingen heeft {client_name.split()[0]} twee stappen zelfstandig uitgevoerd. Daarna heeft {client_name.split()[0]} goed ontbeten en meegedaan aan de geplande activiteit. Er waren geen incidenten of bijzonderheden."
    audit(db, user, "form.demo_prepared", "form", form.id, {"client_id": client.id, "client_created": created})
    db.commit()
    return {"form": form_payload(form), "client_id": client.id, "client_name": client_name, "client_created": created, "example_narrative": example}


@app.get("/api/reminders")
def reminders(db: Session = Depends(get_db), user: User = Depends(current_user)):
    query = select(Reminder).where(Reminder.organization_id == user.organization_id, Reminder.status == "open").order_by(Reminder.due_at)
    if user.role == "caregiver": query = query.where((Reminder.assigned_user_id == user.id) | (Reminder.assigned_user_id.is_(None)))
    rows = db.scalars(query).all(); result = []
    for r in rows:
        client = db.get(Client, r.client_id) if r.client_id else None
        result.append({"id": r.id, "title": decrypt_text(r.title_enc), "detail": decrypt_text(r.detail_enc), "due_at": r.due_at.isoformat(), "priority": r.priority, "client_id": r.client_id, "client_name": decrypt_text(client.display_name_enc) if client else None})
    return result


@app.post("/api/reminders")
def create_reminder(data: ReminderIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    try: due = datetime.fromisoformat(data.due_at.replace("Z", "+00:00"))
    except ValueError as exc: raise HTTPException(422, "Ongeldige datum") from exc
    row = Reminder(organization_id=user.organization_id, client_id=data.client_id, assigned_user_id=data.assigned_user_id, title_enc=encrypt_text(data.title), detail_enc=encrypt_text(data.detail), due_at=due, priority=data.priority, created_by=user.id)
    db.add(row); db.flush(); audit(db, user, "reminder.created", "reminder", row.id); db.commit(); return {"id": row.id}


@app.post("/api/reminders/{reminder_id}/complete")
def complete_reminder(reminder_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    row = db.get(Reminder, reminder_id)
    if not row or row.organization_id != user.organization_id: raise HTTPException(404, "Reminder niet gevonden")
    row.status = "completed"; audit(db, user, "reminder.completed", "reminder", row.id); db.commit(); return {"ok": True}


DEMO_ASSETS_DIR = Path(__file__).parent / "demo_assets"
DEMO_DOCUMENTS = [
    ("Demo-Zorg_Formulierenbibliotheek.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "Fictieve formulierenbibliotheek (Word) — demo, niet gebruiken zonder organisatievalidatie."),
    ("Demo-Zorg_Formulierenbibliotheek.pdf", "application/pdf", "Fictieve formulierenbibliotheek (PDF) — demo, niet gebruiken zonder organisatievalidatie."),
    ("forms_bundle.json", "application/json", "Fictieve formulierenbibliotheek (JSON-bundel) — demo, niet gebruiken zonder organisatievalidatie."),
]

# Standaard-cadans per formulier: welke verschijnt dagelijks, welke bij een incident, en welke op aanvraag.
# De werkgever (org_admin) kan dit per formulier aanpassen.
CADENCE_DAILY = ("daily_report", "shift_handover")
CADENCE_INCIDENT = ("incident", "wkkgz", "wzd", "medication_deviation", "acute_crisis", "missing", "meldcode")


def default_cadence(form_id: str) -> str:
    if any(k in form_id for k in CADENCE_DAILY):
        return "daily"
    if any(k in form_id for k in CADENCE_INCIDENT):
        return "incident"
    return "on_demand"


def load_form_library() -> list[dict]:
    path = DEMO_ASSETS_DIR / "forms_bundle.json"
    if not path.exists():
        return []
    bundle = json.loads(path.read_text(encoding="utf-8"))
    return bundle.get("forms", [])


def compact_form(f: FormTemplate) -> dict:
    schema = decrypt_json(f.schema_enc)
    schema = schema if isinstance(schema, dict) else {}
    sections = []
    for sec in schema.get("sections", []):
        fields = [{"id": fl.get("id"), "label": fl.get("label"), "type": fl.get("type", "text"), "required": bool(fl.get("required")), "options": fl.get("options", [])} for fl in sec.get("fields", [])]
        sections.append({"title": sec.get("title", ""), "fields": fields})
    return {"form_type": f.form_type, "title": f.title, "cadence": f.cadence, "purpose": schema.get("purpose", ""), "safety_triggers": schema.get("safety_triggers", []), "sections": sections}


INCIDENT_FORM_SIGNALS = {
    "medication": ("medicatiefout", "medicatie vergeten", "medicatie was vergeten", "medicatie gemist", "verkeerde medicatie", "medicatie te laat", "medicatie niet ingenomen", "niet ingenomen", "nog in het bakje", "bijwerking", "afwijking"),
    "internal_incident": ("incident", "gevallen", "gleed", "viel", "letsel", "agressie", "geweld"),
    "wkkgz": ("calamiteit", "geweld", "ernstig letsel", "overleden"),
    "wzd": ("verzet", "onvrijwillig", "trok haar arm terug", "trok zijn arm terug", "zei nee"),
    "acute_crisis": ("ernstig benauwd", "bewusteloos", "reageerde nauwelijks", "suïc", "112", "crisis"),
    "missing": ("vermist", "weggelopen", "ongeplande afwezigheid"),
    "meldcode": ("mishandel", "huiselijk geweld", "seksueel", "veilig thuis"),
}


def incident_form_relevant(form_type: str, narrative: str) -> bool:
    normalized_type = form_type.casefold()
    text_value = narrative.casefold()
    return any(key in normalized_type and any(signal in text_value for signal in signals) for key, signals in INCIDENT_FORM_SIGNALS.items())


def forms_to_fill(db: Session, organization_id: str, narrative: str = "") -> list[dict]:
    rows = db.scalars(select(FormTemplate).where(FormTemplate.organization_id == organization_id, FormTemplate.status == "active", FormTemplate.cadence.in_(["daily", "incident"]), FormTemplate.form_type != "daily_care").order_by(FormTemplate.form_type)).all()
    selected = []
    for form in rows:
        if form.cadence == "daily":
            selected.append(form)
    return [compact_form(form) for form in selected]


def build_form_catalog(db: Session, organization_id: str) -> list[dict]:
    rows = db.scalars(select(FormTemplate).where(FormTemplate.organization_id == organization_id, FormTemplate.status == "active", FormTemplate.cadence != "disabled", FormTemplate.form_type != "daily_care")).all()
    catalog = []
    for f in rows:
        schema = decrypt_json(f.schema_enc)
        catalog.append({"form_type": f.form_type, "title": f.title, "purpose": schema.get("purpose", "") if isinstance(schema, dict) else "", "cadence": f.cadence, "safety_triggers": schema.get("safety_triggers", []) if isinstance(schema, dict) else []})
    return catalog


DAILY_FORM_SCHEMA = {
    "intro": "Vertel vrij hoe de dienst met deze cliënt is verlopen. De AI haalt relevante onderdelen uit je verhaal en vraagt alleen door waar dat nodig is.",
    "formal_required": ["moment", "author", "client", "narrative", "care_minutes", "human_confirmation"],
    "contextual_topics": [
        {"id": "wellbeing", "label": "Algemeen welzijn"}, {"id": "sleep", "label": "Slaap en rust"},
        {"id": "adl", "label": "Persoonlijke verzorging en ADL"}, {"id": "nutrition", "label": "Eten en drinken"},
        {"id": "toileting", "label": "Toiletgang"}, {"id": "mobility", "label": "Mobiliteit"},
        {"id": "medication", "label": "Medicatie en afwijkingen"}, {"id": "behaviour", "label": "Gedrag en stemming"},
        {"id": "communication", "label": "Communicatie"}, {"id": "participation", "label": "Activiteiten en dagstructuur"},
        {"id": "client_voice", "label": "Wens of uitspraak van cliënt"}, {"id": "intervention", "label": "Begeleiding en waarneembaar effect"},
        {"id": "goals", "label": "Voortgang zorgdoelen"}, {"id": "risk", "label": "Risico, incident, letsel of verzet"},
        {"id": "handover", "label": "Overdracht en vervolgactie"}
    ],
    "rule": "Contextuele onderwerpen zijn alleen verplicht als ze door het verhaal geraakt worden; AI mag ontbrekende onderwerpen niet verzinnen."
}

FORM_PREPARATION_INTROS = {
    "01_client_intake": "Vertel wat de cliënt zelf wil bereiken, welke dagelijkse ondersteuning nodig is, hoe communicatie werkt, welke acute risico’s bekend zijn en wie waarvoor verantwoordelijk is.",
    "02_consent_representation": "Leg per besluit of gegevensdeling vast wie mag beslissen, met welk doel, welke minimale gegevens nodig zijn en of toestemming of een andere grondslag geldt.",
    "03_personal_profile": "Beschrijf wat voor deze cliënt belangrijk is, hoe de cliënt communiceert, wat wel en niet helpt en hoe een gewone goede dag of vroege spanning eruitziet.",
    "04_care_plan_goals": "Beschrijf het doel in de woorden van de cliënt, de waarneembare beginsituatie, concrete ondersteuning, eigen regie, verantwoordelijke en evaluatiemoment.",
    "05_daily_report": "Vertel feitelijk wat er gebeurde, wat je zelf waarnam, welke ondersteuning je bood, hoe de cliënt reageerde en of er een vervolgactie nodig is.",
    "06_shift_handover": "Noem alleen actuele veranderingen, risico- of veiligheidsafspraken en open acties met eigenaar en deadline voor de volgende dienst.",
    "07_goal_evaluation": "Beschrijf het geëvalueerde doel, concrete resultaten, de beleving van de cliënt en het voorgestelde vervolg; een bevoegd persoon neemt het besluit.",
    "08_risk_safety_plan": "Beschrijf het concrete risico en de context, vroege signalen, preventie, minder ingrijpende alternatieven, acute acties en wie verantwoordelijk is.",
    "09_internal_incident": "Leg het incident feitelijk en in tijdsvolgorde vast, inclusief gevolgen, directe veiligheidsmaatregelen, betrokken informatie en concrete verbeteractie.",
    "10_wkkgz_triage": "Vul dit alleen in voor menselijke Wkkgz-beoordeling: beschrijf incidentreferentie, mogelijke categorie, schadelijk gevolg, relatie met zorgkwaliteit en het bevoegde besluit.",
    "11_wzd_resistance": "Beschrijf concreet verzet, de zorg die aan de orde was, mogelijk ernstig nadeel, oorzaken, de wens van de cliënt en vrijwillige minder ingrijpende alternatieven.",
    "12_medication_deviation": "Noem medicijn en voorschrift, wat feitelijk is gegeven, gemist of geweigerd, klachten of observaties, letterlijk professioneel advies en uitgevoerde acties met tijden.",
    "13_acute_crisis": "Beschrijf wat plotseling veranderde, het verschil met normaal, objectieve observaties, direct gevaar, gebelde hulp en het effect van de uitgevoerde acties.",
    "14_missing_wandering": "Leg laatst-gezien-informatie, signalement en individueel risico vast, gevolgd door zoekacties en contacten in tijdsvolgorde en de toestand bij terugvinden.",
    "15_meldcode": "Beschrijf per meldcodestap concrete signalen, overleg, gesprek, risicoweging en het menselijke besluit; scheid feiten steeds van interpretaties.",
    "16_complaint_feedback": "Leg het signaal in de woorden van de klager vast, de gewenste oplossing, onafhankelijke behandeling, termijnen, uitkomst en verbeteractie.",
    "17_end_of_care": "Beschrijf reden en datum van beëindiging, reactie van de cliënt, open risico’s, noodzakelijke overdracht, grondslag en acties voor veilige dossierafsluiting.",
}

APP_MANAGED_FORM_FIELDS = {"client_id", "client_reference", "client_name", "event_datetime", "datetime", "date", "time", "author", "employee", "caregiver", "location", "shift", "service", "to_shift", "recipient_shift", "handover_to", "time_spent", "care_minutes", "review_confirmed", "human_confirmation"}


def form_preparation(form_type: str, schema: dict) -> dict:
    groups = []
    for section in schema.get("sections", []):
        fields = [field for field in section.get("fields", []) if field.get("id") not in APP_MANAGED_FORM_FIELDS]
        if fields:
            groups.append({"title": section.get("title", "Onderwerpen"), "required": [field.get("label", field.get("id", "")) for field in fields if field.get("required")], "optional": [field.get("label", field.get("id", "")) for field in fields if not field.get("required")]})
    field_count = sum(len(group["required"]) + len(group["optional"]) for group in groups)
    return {"intro": FORM_PREPARATION_INTROS.get(form_type, schema.get("purpose", "Vertel wat voor dit formulier feitelijk nodig is.")), "instruction": "Beantwoord deze punten of vertel vrij in je eigen woorden; je hoeft de volgorde niet aan te houden en vult alleen in wat je weet.", "complexity": "simple" if field_count <= 6 else "detailed", "groups": groups}


def form_payload(f: FormTemplate) -> dict:
    schema = decrypt_json(f.schema_enc)
    purpose = schema.get("purpose", "") if isinstance(schema, dict) else ""
    return {"id": f.id, "title": f.title, "form_type": f.form_type, "version": f.version, "cadence": f.cadence, "purpose": purpose, "scope": "organization" if f.organization_id else "platform", "schema": schema, "preparation": form_preparation(f.form_type, schema if isinstance(schema, dict) else {})}


@app.get("/api/forms")
def forms(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.scalars(select(FormTemplate).where(((FormTemplate.organization_id == user.organization_id) | (FormTemplate.organization_id.is_(None))), FormTemplate.status == "active", FormTemplate.cadence != "disabled", FormTemplate.form_type != "daily_care").order_by(FormTemplate.form_type)).all()
    return [form_payload(f) for f in rows]


@app.get("/api/daily-form-status")
def daily_form_status(db: Session = Depends(get_db), user: User = Depends(current_user)):
    client_query = select(Client).where(Client.organization_id == user.organization_id, Client.active.is_(True))
    if user.role == "caregiver":
        assigned_ids = db.scalars(select(ClientAssignment.client_id).where(ClientAssignment.user_id == user.id)).all()
        client_query = client_query.where(Client.id.in_(assigned_ids))
    clients = db.scalars(client_query).all()
    daily_forms = db.scalars(select(FormTemplate).where(FormTemplate.organization_id == user.organization_id, FormTemplate.status == "active", FormTemplate.cadence == "daily", FormTemplate.form_type != "daily_care")).all()
    local_now = datetime.now(ZoneInfo("Europe/Amsterdam"))
    start_utc = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    rows = db.scalars(select(FormSubmission).where(FormSubmission.organization_id == user.organization_id, FormSubmission.created_at >= start_utc)).all()
    completed_by_client: dict[str, set[str]] = {}
    for row in rows:
        if row.client_id: completed_by_client.setdefault(row.client_id, set()).add(row.form_type)
    required = [{"form_type": form.form_type, "title": form.title} for form in daily_forms]
    return [{"client_id": client.id, "required": required, "completed_form_types": sorted(completed_by_client.get(client.id, set())), "complete": all(form.form_type in completed_by_client.get(client.id, set()) for form in daily_forms)} for client in clients]


@app.get("/api/organization/forms")
def organization_forms(db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    rows = db.scalars(select(FormTemplate).where(FormTemplate.organization_id == user.organization_id, FormTemplate.status == "active", FormTemplate.form_type != "daily_care").order_by(FormTemplate.form_type)).all()
    return [form_payload(f) for f in rows]


@app.post("/api/organization/forms/{form_id}/cadence")
def set_form_cadence(form_id: str, data: FormCadenceIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    form = db.get(FormTemplate, form_id)
    if not form or form.organization_id != user.organization_id:
        raise HTTPException(404, "Formulier niet gevonden")
    form.cadence = data.cadence
    audit(db, user, "form.cadence_changed", "form", form.id, {"cadence": data.cadence})
    db.commit()
    return {"ok": True, "cadence": form.cadence}


@app.get("/api/organization/shifts")
def get_shift_settings(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return {"shifts": organization_shifts(db, user.organization_id), "active": shift_context(db, user.organization_id)}


@app.post("/api/organization/shifts")
def set_shift_settings(data: ShiftSettingsIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    names = [shift.name.strip().casefold() for shift in data.shifts]
    starts = [shift.starts_at for shift in data.shifts]
    if len(names) != len(set(names)) or len(starts) != len(set(starts)):
        raise HTTPException(422, "Dienstnamen en starttijden moeten uniek zijn")
    row = db.scalar(select(OrganizationSettings).where(OrganizationSettings.organization_id == user.organization_id))
    if not row:
        row = OrganizationSettings(organization_id=user.organization_id, care_types_enc=encrypt_json([]), branding_enc=encrypt_json({}))
        db.add(row)
    branding = decrypt_json(row.branding_enc)
    branding = branding if isinstance(branding, dict) else {}
    branding["shifts"] = [shift.model_dump() for shift in data.shifts]
    row.branding_enc = encrypt_json(branding)
    audit(db, user, "organization.shifts_changed", "organization", user.organization_id, {"count": len(data.shifts)})
    db.commit()
    return {"ok": True, "shifts": branding["shifts"]}


def caregiver_may_access_client(db: Session, user: User, client_id: str) -> bool:
    if user.role != "caregiver":
        return True
    return bool(db.scalar(select(ClientAssignment).where(ClientAssignment.client_id == client_id, ClientAssignment.user_id == user.id)))


@app.post("/api/forms/{form_id}/submit")
def submit_form(form_id: str, data: FormSubmitIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = db.get(FormTemplate, form_id)
    if not form or form.organization_id != user.organization_id or form.status != "active":
        raise HTTPException(404, "Formulier niet gevonden")
    if not data.human_review_confirmed:
        raise HTTPException(422, "Menselijke eindcontrole is verplicht")
    client_id = data.client_id or None
    if client_id:
        client = db.get(Client, client_id)
        if not client or client.organization_id != user.organization_id:
            raise HTTPException(404, "Cliënt niet gevonden")
        if not caregiver_may_access_client(db, user, client_id):
            raise HTTPException(403, "Deze cliënt is niet aan jou toegewezen")
    row = FormSubmission(organization_id=user.organization_id, client_id=client_id, form_template_id=form.id, form_type=form.form_type, form_title=form.title, author_id=user.id, data_enc=encrypt_json(data.answers))
    db.add(row); db.flush()
    audit(db, user, "form.submitted", "form_submission", row.id, {"form_type": form.form_type, "client_id": client_id})
    db.commit()
    return {"ok": True, "submission_id": row.id}


@app.get("/api/clients/{client_id}/submissions")
def client_submissions(client_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    client = db.get(Client, client_id)
    if not client or client.organization_id != user.organization_id:
        raise HTTPException(404, "Cliënt niet gevonden")
    if not caregiver_may_access_client(db, user, client_id):
        raise HTTPException(403, "Deze cliënt is niet aan jou toegewezen")
    rows = db.scalars(select(FormSubmission).where(FormSubmission.client_id == client_id).order_by(FormSubmission.created_at.desc())).all()
    return [{"id": s.id, "form_title": s.form_title, "form_type": s.form_type, "created_at": s.created_at.isoformat()} for s in rows]


@app.get("/api/submissions/{submission_id}")
def get_submission(submission_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    row = db.get(FormSubmission, submission_id)
    if not row or row.organization_id != user.organization_id:
        raise HTTPException(404, "Formulier niet gevonden")
    if row.author_id != user.id and row.client_id and not caregiver_may_access_client(db, user, row.client_id):
        raise HTTPException(403, "Geen toegang tot dit formulier")
    return {"id": row.id, "form_title": row.form_title, "form_type": row.form_type, "created_at": row.created_at.isoformat(), "client_id": row.client_id, "answers": decrypt_json(row.data_enc)}


@app.post("/api/organization/seed-demo")
def seed_demo(db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    settings_row = db.scalar(select(OrganizationSettings).where(OrganizationSettings.organization_id == user.organization_id))
    if settings_row and settings_row.demo_ready: return {"ok": True, "already_ready": True}
    if not settings_row:
        settings_row = OrganizationSettings(organization_id=user.organization_id, care_types_enc=encrypt_json(["verstandelijke beperking", "dementiezorg"]), branding_enc=encrypt_json({}))
        db.add(settings_row)
    examples = [
        ("Henk Jansen", "79 jaar, dementie. Rustige benadering, één vraag tegelijk.", ["Onrust vroeg signaleren", "Een voorspelbare dagstructuur"]),
        ("Marieke de Boer", "34 jaar, matige verstandelijke beperking. Communiceert met korte zinnen en pictogrammen.", ["Zelfstandigheid bij persoonlijke verzorging", "Duidelijk aangeven wat zij wil"]),
        ("Samir El Amrani", "51 jaar, niet-aangeboren hersenletsel. Snel overprikkeld bij onverwachte veranderingen.", ["Prikkels tijdig herkennen", "Meedoen aan passende activiteiten"]),
    ]
    clients_created = []
    for name, context, goals in examples:
        client = Client(organization_id=user.organization_id, display_name_enc=encrypt_text(name), context_enc=encrypt_text(context)); db.add(client); db.flush()
        for goal in goals: db.add(CareGoal(client_id=client.id, title_enc=encrypt_text(goal), description_enc=encrypt_text("")))
        clients_created.append(client)
    due = datetime.now(timezone.utc) + timedelta(hours=2)
    db.add(Reminder(organization_id=user.organization_id, client_id=clients_created[0].id, assigned_user_id=None, title_enc=encrypt_text("Let vandaag extra op ochtendonrust"), detail_enc=encrypt_text("Bekijk de overdracht van de nachtdienst en rapporteer het waarneembare effect van begeleiding."), due_at=due, priority="high", created_by=user.id))
    db.add(Reminder(organization_id=user.organization_id, client_id=clients_created[1].id, assigned_user_id=None, title_enc=encrypt_text("Zorgdoel persoonlijke verzorging evalueren"), detail_enc=encrypt_text("Leg vast welke stap Marieke vandaag zelfstandig uitvoerde."), due_at=due + timedelta(hours=3), priority="normal", created_by=user.id))
    active_form = db.scalar(select(FormTemplate).where(FormTemplate.organization_id == user.organization_id, FormTemplate.form_type == "daily_care", FormTemplate.status == "active"))
    if not active_form:
        prior_forms = db.scalars(select(FormTemplate).where(FormTemplate.organization_id == user.organization_id, FormTemplate.form_type == "daily_care")).all()
        db.add(FormTemplate(organization_id=user.organization_id, title="Dagelijkse zorgrapportage", form_type="daily_care", version=len(prior_forms) + 1, schema_enc=encrypt_json(DAILY_FORM_SCHEMA), cadence="daily", status="active", created_by=user.id))
    forms_created = 0
    for form in load_form_library():
        form_type = form.get("id")
        if not form_type:
            continue
        if db.scalar(select(FormTemplate).where(FormTemplate.organization_id == user.organization_id, FormTemplate.form_type == form_type, FormTemplate.status == "active")):
            continue
        db.add(FormTemplate(organization_id=user.organization_id, title=form.get("title", form_type), form_type=form_type, version=1, schema_enc=encrypt_json(form), cadence=default_cadence(form_type), status="active", created_by=user.id))
        forms_created += 1
    documents_created = 0
    for file_name, mime_type, note in DEMO_DOCUMENTS:
        path = DEMO_ASSETS_DIR / file_name
        if not path.exists():
            continue
        content = path.read_bytes()
        db.add(DocumentUpload(organization_id=user.organization_id, uploaded_by=user.id, file_name_enc=encrypt_text(file_name), mime_type=mime_type, content_enc=encrypt_text(base64.b64encode(content).decode()), note_enc=encrypt_text(note), admin_note_enc=encrypt_text("")))
        documents_created += 1
    settings_row.demo_ready = True; audit(db, user, "demo.seeded", "organization", user.organization_id); db.commit()
    return {"ok": True, "clients": len(clients_created), "documents": documents_created, "forms": forms_created}


@app.post("/api/clients/{client_id}/archive")
def archive_client(client_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    client = db.get(Client, client_id)
    if not client or client.organization_id != user.organization_id: raise HTTPException(404, "Cliënt niet gevonden")
    client.active = False
    for reminder in db.scalars(select(Reminder).where(Reminder.client_id == client.id, Reminder.status == "open")).all(): reminder.status = "cancelled"
    audit(db, user, "client.archived", "client", client.id); db.commit(); return {"ok": True}


@app.post("/api/organization/reset-demo")
def reset_demo(db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    for client in db.scalars(select(Client).where(Client.organization_id == user.organization_id)).all(): client.active = False
    for reminder in db.scalars(select(Reminder).where(Reminder.organization_id == user.organization_id, Reminder.status == "open")).all(): reminder.status = "cancelled"
    for form in db.scalars(select(FormTemplate).where(FormTemplate.organization_id == user.organization_id)).all(): form.status = "archived"
    settings_row = db.scalar(select(OrganizationSettings).where(OrganizationSettings.organization_id == user.organization_id))
    if settings_row: settings_row.demo_ready = False
    audit(db, user, "demo.reset", "organization", user.organization_id); db.commit(); return {"ok": True}


def owned_session(db: Session, session_id: str, user: User) -> ReportingSession:
    row = db.get(ReportingSession, session_id)
    if not row or row.organization_id != user.organization_id: raise HTTPException(404, "Rapportagesessie niet gevonden")
    return row


def run_ai(db: Session, row: ReportingSession, user: User):
    client = db.get(Client, row.client_id)
    goals = [{"goal_id": g.id, "title": decrypt_text(g.title_enc), "description": decrypt_text(g.description_enc)} for g in client.goals if g.active]
    conversation = decrypt_json(row.conversation_enc)
    target_context = next((item for item in conversation if item.get("kind") == "target_form"), None)
    target_form = db.get(FormTemplate, target_context.get("form_id")) if target_context else None
    if target_form and (target_form.organization_id != row.organization_id or target_form.status != "active"):
        raise HTTPException(409, "Het gekozen formulier is niet meer actief")
    form = db.scalar(select(FormTemplate).where(FormTemplate.organization_id == row.organization_id, FormTemplate.form_type == "daily_care", FormTemplate.status == "active").order_by(FormTemplate.version.desc()))
    form_schema = {} if target_form else (decrypt_json(form.schema_enc) if form else DAILY_FORM_SCHEMA)
    fill_forms = [compact_form(target_form)] if target_form else forms_to_fill(db, row.organization_id, decrypt_text(row.narrative_enc))
    narrative = decrypt_text(row.narrative_enc)
    # Gewone code beperkt de catalogus tot formulieren met een concreet signaal.
    # De AI hoeft daardoor niet bij iedere rustige dienst zeventien formulieren te lezen.
    form_catalog = [] if target_form else [form for form in build_form_catalog(db, row.organization_id) if incident_form_relevant(form["form_type"], narrative)]
    org = db.get(Organization, row.organization_id)
    role_label = professional_user_label(user).split(" · ", 1)[-1]
    name_label = professional_user_label(user).split(" · ", 1)[0]
    registration_context = {
        "client_name": decrypt_text(client.display_name_enc),
        "client_reference": client.id,
        "datetime": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "author": name_label,
        "author_role": role_label,
        "location": org.name if org else "",
        **shift_context(db, row.organization_id),
    }
    try:
        result = next_plan(narrative=narrative, conversation=[item for item in conversation if item.get("kind") != "target_form"], client_context=decrypt_text(client.context_enc), goals=goals, form_schema=form_schema, fill_forms=fill_forms, form_catalog=form_catalog, registration_context=registration_context, user_id=user.id)
    except AIUnavailable as exc:
        db.rollback()
        raise HTTPException(503, {"message": str(exc), "code": exc.code}) from exc
    plan = result.plan
    row.ai_state_enc = encrypt_json(plan.model_dump(mode="json"))
    row.status = "ready" if plan.state == "ready" else "collecting"
    db.add(AuditLog(organization_id=user.organization_id, user_id=user.id, action="ai.assessed", target_type="session", target_id=row.id, details=json.dumps({"state": plan.state, "risk": plan.risk_level.value, **result.telemetry}, ensure_ascii=False)))
    db.commit()
    return plan


@app.post("/api/sessions")
def start_session(data: StartSessionIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    client = db.get(Client, data.client_id)
    if not client or client.organization_id != user.organization_id: raise HTTPException(404, "Cliënt niet gevonden")
    if user.role == "caregiver" and not db.scalar(select(ClientAssignment).where(ClientAssignment.client_id == client.id, ClientAssignment.user_id == user.id)):
        raise HTTPException(403, "Deze cliënt is niet aan jou toegewezen")
    narrative_text = data.narrative.casefold()
    selected_name = decrypt_text(client.display_name_enc)
    selected_first = selected_name.split()[0].casefold()
    if not re.search(rf"\b{re.escape(selected_first)}\b", narrative_text):
        for other in db.scalars(select(Client).where(Client.organization_id == user.organization_id, Client.id != client.id, Client.active.is_(True))).all():
            other_name = decrypt_text(other.display_name_enc)
            other_first = other_name.split()[0].casefold()
            if len(other_first) >= 3 and re.search(rf"\b{re.escape(other_first)}\b", narrative_text):
                raise HTTPException(409, f"Je koos {selected_name}, maar de rapportage noemt {other_name}. Controleer eerst de cliënt.")
    target_form = None
    if data.form_id:
        target_form = db.get(FormTemplate, data.form_id)
        if not target_form or target_form.organization_id != user.organization_id or target_form.status != "active" or target_form.cadence == "disabled":
            raise HTTPException(404, "Formulier niet gevonden")
    conversation = [{"kind": "target_form", "form_id": target_form.id}] if target_form else []
    row = ReportingSession(organization_id=user.organization_id, client_id=client.id, user_id=user.id, narrative_enc=encrypt_text(data.narrative), conversation_enc=encrypt_json(conversation), ai_state_enc=encrypt_json({}))
    db.add(row); db.flush()
    plan = run_ai(db, row, user)
    return {"session_id": row.id, "plan": plan}


@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(current_user)):
    content = await file.read()
    if not content:
        raise HTTPException(422, "De opname is leeg")
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(413, "De opname is te groot; spreek maximaal enkele minuten in")
    if not (file.content_type or "").startswith("audio/"):
        raise HTTPException(415, "Ongeldig audioformaat")
    try:
        text_value, telemetry = transcribe_audio(content, file_name=file.filename or "spraak.webm", mime_type=file.content_type or "audio/webm", user_id=user.id)
    except AIUnavailable as exc:
        raise HTTPException(503, {"message": str(exc), "code": exc.code}) from exc
    audit(db, user, "audio.transcribed", "user", user.id, telemetry)
    db.commit()
    return {"text": text_value}


@app.post("/api/sessions/{session_id}/answer")
def answer(session_id: str, data: AnswerIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    row = owned_session(db, session_id, user)
    if row.status == "finalized": raise HTTPException(409, "Rapportage is al definitief")
    plan = decrypt_json(row.ai_state_enc)
    questions = plan.get("clarification_questions", []) if isinstance(plan, dict) else []
    if not questions: raise HTTPException(409, "Er staan geen vragen open")
    open_ids = {question.get("id") for question in questions}
    received_ids = {answer.question_id for answer in data.answers}
    if received_ids != open_ids or len(data.answers) != len(open_ids):
        raise HTTPException(422, "Beantwoord alle open vragen precies één keer")
    conversation = decrypt_json(row.conversation_enc)
    question_by_id = {question.get("id"): question for question in questions}
    conversation.extend({"question_id": answer.question_id, "field_ids": question_by_id.get(answer.question_id, {}).get("field_ids", []), "answer": answer.value} for answer in data.answers)
    row.conversation_enc = encrypt_json(conversation)
    local_plan = AIPlan.model_validate(plan)
    if apply_simple_answers_without_ai(local_plan, [answer.model_dump() for answer in data.answers]):
        row.ai_state_enc = encrypt_json(local_plan.model_dump(mode="json")); row.status = "ready"
        audit(db, user, "answers.applied_locally", "session", row.id, {"answers": len(data.answers), "ai_calls_saved": 1})
        db.commit()
        return {"session_id": row.id, "plan": local_plan, "processed_locally": True}
    return {"session_id": row.id, "plan": run_ai(db, row, user)}


@app.post("/api/sessions/{session_id}/finalize")
def finalize(session_id: str, data: FinalizeIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    row = owned_session(db, session_id, user)
    if row.status == "finalized": raise HTTPException(409, "Rapportage is al definitief")
    plan = decrypt_json(row.ai_state_enc)
    if plan.get("state") != "ready":
        raise HTTPException(409, "De AI-verheldering is nog niet afgerond")
    if not data.human_review_confirmed:
        raise HTTPException(422, "Menselijke eindcontrole is verplicht")
    if plan.get("incident_review_required") and not data.incident_review_acknowledged:
        raise HTTPException(422, "Bevestig dat de incidentwaarschuwing is beoordeeld")
    expected_form_types = {draft.get("form_type") for draft in plan.get("form_drafts", []) if draft.get("form_type")}
    submitted_form_types = {submission.form_type for submission in data.form_submissions}
    if not expected_form_types.issubset(submitted_form_types):
        raise HTTPException(422, "Niet alle gecontroleerde formulieren zijn meegestuurd")
    prepared_submissions = []
    for submission in data.form_submissions:
        template = db.scalar(select(FormTemplate).where(FormTemplate.organization_id == user.organization_id, FormTemplate.form_type == submission.form_type, FormTemplate.status == "active").order_by(FormTemplate.version.desc()))
        if not template:
            raise HTTPException(422, f"Formulier {submission.form_type} is niet meer actief")
        answers = dict(submission.answers)
        if "time_spent" in answers: answers["time_spent"] = data.care_minutes
        if "care_minutes" in answers: answers["care_minutes"] = data.care_minutes
        if "review_confirmed" in answers: answers["review_confirmed"] = True
        if "human_confirmation" in answers: answers["human_confirmation"] = True
        schema = decrypt_json(template.schema_enc)
        required = {field.get("id") for section in schema.get("sections", []) for field in section.get("fields", []) if field.get("required")}
        missing = [field_id for field_id in required if answers.get(field_id) in (None, "", [])]
        if missing:
            raise HTTPException(422, f"Verplichte velden ontbreken in {template.title}: {', '.join(sorted(missing))}")
        prepared_submissions.append((template, answers))
    report = Report(organization_id=user.organization_id, client_id=row.client_id, author_id=user.id, session_id=row.id, report_text_enc=encrypt_text(data.report_text), metadata_enc=encrypt_json({"care_minutes": data.care_minutes, "goal_ids": data.selected_goal_ids, "ai_plan": plan}))
    db.add(report); db.flush(); row.status = "finalized"
    draft_fields = {(draft.get("form_type"), field.get("field_id")): str(field.get("value") or "").strip() for draft in plan.get("form_drafts", []) for field in draft.get("fields", [])}
    edited_fields = sum(1 for submission in data.form_submissions for field_id, value in submission.answers.items() if str(value or "").strip() != draft_fields.get((submission.form_type, field_id), ""))
    db.add(AuditLog(organization_id=user.organization_id, user_id=user.id, action="report.finalized", target_type="report", target_id=report.id, details=json.dumps({"session": row.id, "report_edited": data.report_text.strip() != str(plan.get("draft_report") or "").strip(), "edited_fields": edited_fields}, ensure_ascii=False)))
    submissions_created = 0
    for tpl, final_answers in prepared_submissions:
        sub = FormSubmission(organization_id=user.organization_id, client_id=row.client_id, form_template_id=tpl.id, form_type=tpl.form_type, form_title=tpl.title, author_id=user.id, session_id=row.id, data_enc=encrypt_json(final_answers))
        db.add(sub); db.flush()
        audit(db, user, "form.submitted", "form_submission", sub.id, {"form_type": tpl.form_type, "session": row.id})
        submissions_created += 1
    db.commit()
    return {"ok": True, "report_id": report.id, "form_submissions": submissions_created}


def review_payload(db: Session, review: ReportReview) -> dict:
    client = db.get(Client, review.client_id)
    assignee = db.get(User, review.assigned_to)
    addendum = db.scalar(select(ReportAddendum).where(ReportAddendum.review_id == review.id).order_by(ReportAddendum.created_at.desc()))
    return {
        "id": review.id, "report_id": review.report_id, "client_id": review.client_id,
        "client_name": decrypt_text(client.display_name_enc) if client else "Onbekende cliënt",
        "assigned_to": review.assigned_to, "assigned_to_email": assignee.email if assignee else "",
        "question": decrypt_text(review.question_enc), "status": review.status,
        "due_at": review.due_at.isoformat() if review.due_at else None,
        "created_at": review.created_at.isoformat(), "answered_at": review.answered_at.isoformat() if review.answered_at else None,
        "addendum": decrypt_text(addendum.text_enc) if addendum else None,
        "addendum_created_at": addendum.created_at.isoformat() if addendum else None,
    }


@app.get("/api/organization/workspace")
def organization_workspace(db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    now_utc = datetime.now(timezone.utc)
    local_now = datetime.now(ZoneInfo("Europe/Amsterdam"))
    start_utc = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    reports_today = db.scalars(select(Report).where(Report.organization_id == user.organization_id, Report.signed_at >= start_utc)).all()
    forms_today = db.scalars(select(FormSubmission).where(FormSubmission.organization_id == user.organization_id, FormSubmission.created_at >= start_utc)).all()
    reports = db.scalars(select(Report).where(Report.organization_id == user.organization_id).order_by(Report.signed_at.desc()).limit(30)).all()
    reviews = db.scalars(select(ReportReview).where(ReportReview.organization_id == user.organization_id).order_by(ReportReview.created_at.desc()).limit(50)).all()
    recent = []
    for report in reports:
        client = db.get(Client, report.client_id); author = db.get(User, report.author_id)
        recent.append({"id": report.id, "client_id": report.client_id, "client_name": decrypt_text(client.display_name_enc) if client else "Onbekende cliënt", "author": professional_user_label(author), "signed_at": report.signed_at.isoformat()})
    open_reviews = [review_payload(db, item) for item in reviews if item.status in ("open", "answered")]
    daily_forms = db.scalars(select(FormTemplate).where(FormTemplate.organization_id == user.organization_id, FormTemplate.status == "active", FormTemplate.cadence == "daily", FormTemplate.form_type != "daily_care")).all()
    active_clients = db.scalars(select(Client).where(Client.organization_id == user.organization_id, Client.active.is_(True))).all()
    completed = {(item.client_id, item.form_type) for item in forms_today if item.client_id}
    missing_daily = []
    for client in active_clients:
        missing = [form.title for form in daily_forms if (client.id, form.form_type) not in completed]
        if missing:
            missing_daily.append({"client_id": client.id, "client_name": decrypt_text(client.display_name_enc), "missing_forms": missing, "count": len(missing)})
    overdue = sum(review.status == "open" and review.due_at is not None and (review.due_at.replace(tzinfo=timezone.utc) if review.due_at.tzinfo is None else review.due_at) < now_utc for review in reviews)
    care_minutes = sum(int((decrypt_json(report.metadata_enc) or {}).get("care_minutes") or 0) for report in reports_today)
    return {"metrics": {"reports_today": len(reports_today), "forms_today": len(forms_today), "care_minutes_today": care_minutes, "missing_daily": sum(item["count"] for item in missing_daily), "open_questions": sum(item.status == "open" for item in reviews), "overdue_questions": overdue, "answers_to_review": sum(item.status == "answered" for item in reviews)}, "reviews": open_reviews, "missing_daily": missing_daily, "recent_reports": recent}


@app.get("/api/organization/clients/{client_id}/timeline")
def organization_client_timeline(client_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    client = db.get(Client, client_id)
    if not client or client.organization_id != user.organization_id: raise HTTPException(404, "Cliënt niet gevonden")
    events = []
    for report in db.scalars(select(Report).where(Report.client_id == client_id, Report.organization_id == user.organization_id)).all():
        author = db.get(User, report.author_id)
        events.append({"kind": "report", "id": report.id, "title": "Dagrapportage", "subtitle": professional_user_label(author), "created_at": report.signed_at.isoformat()})
    for submission in db.scalars(select(FormSubmission).where(FormSubmission.client_id == client_id, FormSubmission.organization_id == user.organization_id)).all():
        author = db.get(User, submission.author_id)
        events.append({"kind": "form", "id": submission.id, "title": submission.form_title, "subtitle": professional_user_label(author), "created_at": submission.created_at.isoformat()})
    for addendum in db.scalars(select(ReportAddendum).where(ReportAddendum.client_id == client_id, ReportAddendum.organization_id == user.organization_id)).all():
        author = db.get(User, addendum.author_id)
        events.append({"kind": "addendum", "id": addendum.id, "report_id": addendum.report_id, "title": "Aanvulling op rapportage", "subtitle": professional_user_label(author), "created_at": addendum.created_at.isoformat()})
    events.sort(key=lambda item: item["created_at"], reverse=True)
    audit(db, user, "dossier.timeline_viewed", "client", client.id); db.commit()
    return {"client": {"id": client.id, "display_name": decrypt_text(client.display_name_enc), "context": decrypt_text(client.context_enc), "goals": [decrypt_text(goal.title_enc) for goal in client.goals if goal.active]}, "events": events}


@app.get("/api/organization/reports/{report_id}")
def organization_report(report_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    report = db.get(Report, report_id)
    if not report or report.organization_id != user.organization_id: raise HTTPException(404, "Rapportage niet gevonden")
    client = db.get(Client, report.client_id); author = db.get(User, report.author_id)
    reviews = db.scalars(select(ReportReview).where(ReportReview.report_id == report.id).order_by(ReportReview.created_at.desc())).all()
    audit(db, user, "dossier.report_viewed", "report", report.id); db.commit()
    metadata = decrypt_json(report.metadata_enc)
    submissions = db.scalars(select(FormSubmission).where(FormSubmission.session_id == report.session_id).order_by(FormSubmission.created_at)).all()
    rendered_submissions = []
    for submission in submissions:
        template = db.get(FormTemplate, submission.form_template_id)
        schema = decrypt_json(template.schema_enc) if template else {}
        labels = {field.get("id"): field.get("label", field.get("id")) for section in schema.get("sections", []) for field in section.get("fields", [])} if isinstance(schema, dict) else {}
        answers = decrypt_json(submission.data_enc)
        fields = [{"id": key, "label": labels.get(key, key.replace("_", " ").title()), "value": value, "empty": value in (None, "", [])} for key, value in answers.items()]
        rendered_submissions.append({"id": submission.id, "title": submission.form_title, "form_type": submission.form_type, "fields": fields, "filled": sum(not field["empty"] for field in fields), "empty": sum(field["empty"] for field in fields)})
    return {"id": report.id, "client_id": report.client_id, "client_name": decrypt_text(client.display_name_enc) if client else "", "author": professional_user_label(author), "signed_at": report.signed_at.isoformat(), "report_text": decrypt_text(report.report_text_enc), "care_minutes": metadata.get("care_minutes"), "submissions": rendered_submissions, "reviews": [review_payload(db, item) for item in reviews], "immutable": True}


@app.post("/api/organization/reports/{report_id}/request-addition")
def request_report_addition(report_id: str, data: ReviewRequestIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    report = db.get(Report, report_id)
    if not report or report.organization_id != user.organization_id: raise HTTPException(404, "Rapportage niet gevonden")
    due_at = None
    if data.due_at:
        try: due_at = datetime.fromisoformat(data.due_at.replace("Z", "+00:00"))
        except ValueError as exc: raise HTTPException(422, "Ongeldige reactiedatum") from exc
    review = ReportReview(organization_id=user.organization_id, report_id=report.id, client_id=report.client_id, requested_by=user.id, assigned_to=report.author_id, question_enc=encrypt_text(data.question), due_at=due_at)
    db.add(review); db.flush(); audit(db, user, "report.addition_requested", "report_review", review.id, {"report_id": report.id, "assigned_to": report.author_id}); db.commit()
    return review_payload(db, review)


@app.post("/api/organization/reviews/{review_id}/close")
def close_report_review(review_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    review = db.get(ReportReview, review_id)
    if not review or review.organization_id != user.organization_id: raise HTTPException(404, "Verzoek niet gevonden")
    if review.status != "answered": raise HTTPException(409, "Er is nog geen aanvulling om af te ronden")
    review.status = "closed"; review.closed_at = datetime.now(timezone.utc)
    audit(db, user, "report.addition_closed", "report_review", review.id); db.commit(); return {"ok": True}


@app.get("/api/my/review-requests")
def my_review_requests(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.scalars(select(ReportReview).where(ReportReview.organization_id == user.organization_id, ReportReview.assigned_to == user.id, ReportReview.status == "open").order_by(ReportReview.created_at.desc())).all()
    result = []
    for review in rows:
        payload = review_payload(db, review); report = db.get(Report, review.report_id)
        payload.update({"report_text": decrypt_text(report.report_text_enc), "signed_at": report.signed_at.isoformat()})
        result.append(payload)
    return result


@app.post("/api/my/review-requests/{review_id}/addendum")
def answer_review_request(review_id: str, data: AddendumIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    review = db.get(ReportReview, review_id)
    if not review or review.organization_id != user.organization_id or review.assigned_to != user.id: raise HTTPException(404, "Verzoek niet gevonden")
    if review.status != "open": raise HTTPException(409, "Dit verzoek is al beantwoord")
    addendum = ReportAddendum(organization_id=user.organization_id, report_id=review.report_id, review_id=review.id, client_id=review.client_id, author_id=user.id, text_enc=encrypt_text(data.text))
    db.add(addendum); db.flush(); review.status = "answered"; review.answered_at = datetime.now(timezone.utc)
    audit(db, user, "report.addendum_submitted", "report_addendum", addendum.id, {"report_id": review.report_id, "review_id": review.id}); db.commit()
    return {"ok": True, "addendum_id": addendum.id}


@app.get("/api/organization/audit")
def organization_audit(db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "org_admin")
    rows = db.scalars(select(AuditLog).where(AuditLog.organization_id == user.organization_id).order_by(AuditLog.created_at.desc()).limit(100)).all()
    return [{"id": row.id, "action": row.action, "target_type": row.target_type, "target_id": row.target_id, "user": (db.get(User, row.user_id).email if db.get(User, row.user_id) else ""), "created_at": row.created_at.isoformat()} for row in rows]
