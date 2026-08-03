import uuid
from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base


def uid() -> str:
    return str(uuid.uuid4())


def now() -> datetime:
    return datetime.now(timezone.utc)


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(500))
    role: Mapped[str] = mapped_column(String(30), default="caregiver")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Client(Base):
    __tablename__ = "clients"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    display_name_enc: Mapped[str] = mapped_column(Text)
    context_enc: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    goals: Mapped[list["CareGoal"]] = relationship(cascade="all, delete-orphan")


class CareGoal(Base):
    __tablename__ = "care_goals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), index=True)
    title_enc: Mapped[str] = mapped_column(Text)
    description_enc: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ReportingSession(Base):
    __tablename__ = "reporting_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    narrative_enc: Mapped[str] = mapped_column(Text)
    conversation_enc: Mapped[str] = mapped_column(Text, default="[]")
    ai_state_enc: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(30), default="collecting", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), index=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("reporting_sessions.id"), unique=True)
    report_text_enc: Mapped[str] = mapped_column(Text)
    metadata_enc: Mapped[str] = mapped_column(Text, default="{}")
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    target_type: Mapped[str] = mapped_column(String(60))
    target_id: Mapped[str] = mapped_column(String(36), index=True)
    details: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class OrganizationSettings(Base):
    __tablename__ = "organization_settings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), unique=True, index=True)
    care_types_enc: Mapped[str] = mapped_column(Text, default="[]")
    branding_enc: Mapped[str] = mapped_column(Text, default="{}")
    demo_ready: Mapped[bool] = mapped_column(Boolean, default=False)


class Invitation(Base):
    __tablename__ = "invitations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(30), default="caregiver")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    max_uses: Mapped[int] = mapped_column(Integer, default=10)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ClientAssignment(Base):
    __tablename__ = "client_assignments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)


class Reminder(Base):
    __tablename__ = "reminders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    client_id: Mapped[str | None] = mapped_column(ForeignKey("clients.id"), nullable=True, index=True)
    assigned_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    title_enc: Mapped[str] = mapped_column(Text)
    detail_enc: Mapped[str] = mapped_column(Text, default="")
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    priority: Mapped[str] = mapped_column(String(20), default="normal")
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)


class DocumentUpload(Base):
    __tablename__ = "document_uploads"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    uploaded_by: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    file_name_enc: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String(150))
    content_enc: Mapped[str] = mapped_column(Text)
    note_enc: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="uploaded", index=True)
    admin_note_enc: Mapped[str] = mapped_column(Text, default="")
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class FormSubmission(Base):
    __tablename__ = "form_submissions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    client_id: Mapped[str | None] = mapped_column(ForeignKey("clients.id"), nullable=True, index=True)
    form_template_id: Mapped[str] = mapped_column(ForeignKey("form_templates.id"), index=True)
    form_type: Mapped[str] = mapped_column(String(50), index=True)
    form_title: Mapped[str] = mapped_column(String(180))
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    data_enc: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="submitted", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class FormTemplate(Base):
    __tablename__ = "form_templates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(180))
    form_type: Mapped[str] = mapped_column(String(50), default="daily_care")
    version: Mapped[int] = mapped_column(Integer, default=1)
    schema_enc: Mapped[str] = mapped_column(Text)
    cadence: Mapped[str] = mapped_column(String(20), default="on_demand", index=True)
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
