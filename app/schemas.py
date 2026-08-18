from enum import Enum
from pydantic import BaseModel, EmailStr, Field


class RiskLevel(str, Enum):
    none = "none"
    attention = "attention"
    urgent = "urgent"


class LegalSignal(BaseModel):
    code: str
    title: str
    rationale: str
    source: str
    human_action: str


class GoalSuggestion(BaseModel):
    goal_id: str
    title: str
    rationale: str


class FormSuggestion(BaseModel):
    form_type: str
    title: str
    reason: str
    urgency: str = Field(default="normal", pattern="^(normal|soon|urgent)$")


class FilledField(BaseModel):
    field_id: str
    label: str
    value: str = ""
    status: str = Field(default="filled", pattern="^(filled|unknown|needs_input)$")


class FormDraft(BaseModel):
    form_type: str
    title: str
    complete: bool = False
    fields: list[FilledField] = Field(default_factory=list)


class ClarificationQuestion(BaseModel):
    id: str
    field_ids: list[str] = Field(default_factory=list)
    question: str
    why: str = ""
    answer_type: str = Field(default="free_text", pattern="^(free_text|yes_no_unknown|choice)$")
    answer_options: list[str] = Field(default_factory=list)


class AIPlan(BaseModel):
    state: str = Field(pattern="^(ask|ready|urgent)$")
    risk_level: RiskLevel
    short_safety_message: str | None = None
    next_question: str | None = None
    why_this_question: str | None = None
    answer_type: str = Field(pattern="^(free_text|yes_no_unknown|choice)$")
    answer_options: list[str] = Field(default_factory=list)
    clarification_questions: list[ClarificationQuestion] = Field(default_factory=list, max_length=8)
    draft_report: str
    missing_information: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    legal_signals: list[LegalSignal] = Field(default_factory=list)
    goal_suggestions: list[GoalSuggestion] = Field(default_factory=list)
    suggested_forms: list[FormSuggestion] = Field(default_factory=list)
    form_drafts: list[FormDraft] = Field(default_factory=list)
    incident_review_required: bool = False
    human_review_note: str


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class PasswordChangeIn(BaseModel):
    password: str = Field(min_length=12, max_length=200)


class FormModeIn(BaseModel):
    form_mode: str = Field(pattern="^(ask|ai|manual)$")


class ClientIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    context: str = Field(default="", max_length=4000)
    goals: list[str] = Field(default_factory=list)


class StartSessionIn(BaseModel):
    client_id: str
    narrative: str = Field(min_length=3, max_length=12000)
    form_id: str | None = None


class ClarificationAnswer(BaseModel):
    question_id: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=4000)


class AnswerIn(BaseModel):
    answers: list[ClarificationAnswer] = Field(min_length=1, max_length=8)


class FinalFormIn(BaseModel):
    form_type: str
    answers: dict = Field(default_factory=dict)


class FinalizeIn(BaseModel):
    report_text: str = Field(min_length=3, max_length=16000)
    care_minutes: int = Field(ge=0, le=1440)
    selected_goal_ids: list[str] = Field(default_factory=list)
    form_submissions: list[FinalFormIn] = Field(default_factory=list)
    incident_review_acknowledged: bool = False
    human_review_confirmed: bool


class OrganizationIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    admin_email: EmailStr
    admin_password: str = Field(min_length=12, max_length=200)


class JoinIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=12, max_length=200)


class InvitationIn(BaseModel):
    employee_name: str = Field(min_length=2, max_length=120)
    email: EmailStr | None = None


class EmployerInvitationIn(BaseModel):
    organization_name: str = Field(min_length=2, max_length=200)
    contact_name: str = Field(min_length=2, max_length=120)
    email: EmailStr


class ReminderIn(BaseModel):
    client_id: str | None = None
    assigned_user_id: str | None = None
    title: str = Field(min_length=2, max_length=200)
    detail: str = Field(default="", max_length=2000)
    due_at: str
    priority: str = Field(default="normal", pattern="^(normal|high)$")


class AssignmentIn(BaseModel):
    client_id: str
    user_id: str


class DocumentStatusIn(BaseModel):
    status: str = Field(pattern="^(uploaded|reviewing|concept_ready|client_review|active)$")
    admin_note: str = Field(default="", max_length=2000)


class FormCadenceIn(BaseModel):
    cadence: str = Field(pattern="^(daily|incident|on_demand|disabled)$")


class ShiftDefinition(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    starts_at: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    minimum_handover: str = Field(default="", max_length=2000)


class ShiftSettingsIn(BaseModel):
    shifts: list[ShiftDefinition] = Field(min_length=1, max_length=8)


class FormSubmitIn(BaseModel):
    client_id: str | None = None
    answers: dict = Field(default_factory=dict)
    human_review_confirmed: bool


class ImportedFormField(BaseModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_]+$")
    label: str = Field(min_length=1, max_length=300)
    type: str = Field(pattern="^(text|textarea|select|multiselect|boolean|date|datetime|number)$")
    required: bool = False
    options: list[str] = Field(default_factory=list, max_length=100)
    source_quote: str = Field(min_length=1, max_length=1000)


class ImportedFormSection(BaseModel):
    title: str = Field(default="", max_length=300)
    fields: list[ImportedFormField] = Field(min_length=1, max_length=100)


class FormImportProposal(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    purpose: str = Field(default="", max_length=1000)
    suggested_form_type: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_]+$")
    suggested_cadence: str = Field(pattern="^(daily|incident|on_demand)$")
    sections: list[ImportedFormSection] = Field(min_length=1, max_length=50)
    uncertainties: list[str] = Field(default_factory=list, max_length=50)
    fidelity_note: str = Field(default="", max_length=2000)


class FormImportActivateIn(BaseModel):
    proposal: FormImportProposal
    human_review_confirmed: bool


class ReviewRequestIn(BaseModel):
    question: str = Field(min_length=3, max_length=4000)
    due_at: str | None = None


class AddendumIn(BaseModel):
    text: str = Field(min_length=3, max_length=8000)
