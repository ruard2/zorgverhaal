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


class AIPlan(BaseModel):
    state: str = Field(pattern="^(ask|ready|urgent)$")
    risk_level: RiskLevel
    short_safety_message: str | None = None
    next_question: str | None = None
    why_this_question: str | None = None
    answer_type: str = Field(pattern="^(free_text|yes_no_unknown|choice)$")
    answer_options: list[str] = Field(default_factory=list)
    draft_report: str
    missing_information: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    legal_signals: list[LegalSignal] = Field(default_factory=list)
    goal_suggestions: list[GoalSuggestion] = Field(default_factory=list)
    suggested_forms: list[FormSuggestion] = Field(default_factory=list)
    incident_review_required: bool = False
    human_review_note: str


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class ClientIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    context: str = Field(default="", max_length=4000)
    goals: list[str] = Field(default_factory=list)


class StartSessionIn(BaseModel):
    client_id: str
    narrative: str = Field(min_length=3, max_length=12000)


class AnswerIn(BaseModel):
    answer: str = Field(min_length=1, max_length=4000)


class FinalizeIn(BaseModel):
    report_text: str = Field(min_length=3, max_length=16000)
    care_minutes: int = Field(ge=0, le=1440)
    selected_goal_ids: list[str] = Field(default_factory=list)
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


class FormSubmitIn(BaseModel):
    client_id: str | None = None
    answers: dict = Field(default_factory=dict)
    human_review_confirmed: bool
