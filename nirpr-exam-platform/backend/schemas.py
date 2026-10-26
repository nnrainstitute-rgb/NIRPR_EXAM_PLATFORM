"""
NIRPR RSO Examination Platform - Pydantic Schemas
"""

from pydantic import BaseModel, Field, EmailStr, validator, field_validator
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from enum import Enum


class UserRole(str, Enum):
    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"
    EXAMINER = "examiner"
    CANDIDATE = "candidate"


class ExamStatus(str, Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class AttemptStatus(str, Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    AUTO_SUBMITTED = "auto_submitted"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class QuestionType(str, Enum):
    MULTIPLE_CHOICE = "multiple_choice"
    MULTIPLE_SELECT = "multiple_select"
    TRUE_FALSE = "true_false"
    SHORT_ANSWER = "short_answer"
    FILL_IN_GAP = "fill_in_gap"


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_role: str
    must_change_password: bool = False


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=6)
    portal: str = Field(default="candidate", pattern="^(candidate|staff)$")
    otp: Optional[str] = None
    captcha_token: Optional[str] = None
    captcha_answer: Optional[int] = None
    captcha_selection: List[int] = []
    recaptcha_token: Optional[str] = None


class RegisterResponse(BaseModel):
    detail: str
    email: EmailStr
    email_sent: bool = False


class MessageResponse(BaseModel):
    detail: str


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class ForgotPasswordRequest(BaseModel):
    email: EmailStr
    recaptcha_token: Optional[str] = None


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(..., min_length=8)
    confirm_password: str

    @validator('confirm_password')
    def passwords_match(cls, v, values):
        if 'new_password' in values and v != values['new_password']:
            raise ValueError('Passwords do not match')
        return v


class CSVImportResult(BaseModel):
    created: int
    skipped: int
    errors: List[str] = []


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)
    confirm_password: str

    @validator('confirm_password')
    def passwords_match(cls, v, values):
        if 'new_password' in values and v != values['new_password']:
            raise ValueError('Passwords do not match')
        return v


class UserBase(BaseModel):
    email: EmailStr
    full_name: str = Field(..., min_length=2, max_length=200)
    phone: Optional[str] = None
    role: UserRole = UserRole.CANDIDATE


class UserCreate(UserBase):
    password: str = Field(..., min_length=8)
    surname: Optional[str] = None
    first_name: Optional[str] = None
    other_name: Optional[str] = None
    training_program_id: Optional[int] = None
    institution: Optional[str] = None
    qualification: Optional[str] = None
    practice_type: Optional[str] = None
    recaptcha_token: Optional[str] = None


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    surname: Optional[str] = None
    first_name: Optional[str] = None
    other_name: Optional[str] = None
    phone: Optional[str] = None
    is_active: Optional[bool] = None


class UserResponse(UserBase):
    id: int
    surname: Optional[str] = None
    first_name: Optional[str] = None
    other_name: Optional[str] = None
    is_active: bool
    email_verified: bool
    must_change_password: bool = False
    last_login: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True


class AdminPasswordReset(BaseModel):
    new_password: str = Field(..., min_length=8)


class CandidateProfileResponse(BaseModel):
    id: int
    institution: Optional[str]
    department: Optional[str]
    qualification: Optional[str]
    years_of_experience: int
    nnra_registration_number: Optional[str]
    practice_type: Optional[str]
    accreditation_status: str
    training_program_id: int

    class Config:
        from_attributes = True


class UserDetailResponse(UserResponse):
    candidate_profile: Optional[CandidateProfileResponse] = None
    candidate_number: Optional[str] = None
    dashboard_candidate_number: Optional[str] = None
    is_impersonated: bool = False


class TrainingProgramBase(BaseModel):
    code: str = Field(..., min_length=2, max_length=20)
    name: str = Field(..., min_length=3, max_length=200)
    description: Optional[str] = None
    practice_area: str = Field(..., min_length=2, max_length=100)
    duration_days: int = Field(default=5, ge=1, le=30)
    passing_score: float = Field(default=70.0, ge=0, le=100)
    is_active: bool = True


class TrainingProgramCreate(TrainingProgramBase):
    pass


class TrainingProgramUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    practice_area: Optional[str] = None
    duration_days: Optional[int] = None
    passing_score: Optional[float] = None
    is_active: Optional[bool] = None


class TrainingProgramResponse(TrainingProgramBase):
    id: int
    created_at: datetime
    updated_at: datetime
    question_bank_count: int = 0
    candidate_count: int = 0

    class Config:
        from_attributes = True


class QuestionBankBase(BaseModel):
    name: str = Field(..., min_length=3, max_length=200)
    description: Optional[str] = None
    training_program_id: int


class QuestionBankCreate(QuestionBankBase):
    pass


class QuestionBankResponse(QuestionBankBase):
    id: int
    is_active: bool
    created_at: datetime
    question_count: int = 0

    class Config:
        from_attributes = True


class QuestionBase(BaseModel):
    question_text: str = Field(..., min_length=10)
    question_type: QuestionType = QuestionType.MULTIPLE_CHOICE
    # For multiple_choice / multiple_select / true_false: option key -> option text.
    # For short_answer / fill_in_gap: leave empty ({}); acceptable answers go in
    # correct_answer instead (each entry may list alternatives separated by "/").
    options: Dict[str, str] = Field(default_factory=dict)
    correct_answer: List[str]
    explanation: Optional[str] = None
    marks: float = Field(default=1.0, gt=0)
    difficulty: str = Field(default="medium", pattern="^(easy|medium|hard)$")
    topic: Optional[str] = None
    is_active: bool = True

    @validator('options')
    def options_required_for_choice_types(cls, v, values):
        qtype = values.get('question_type')
        if qtype in (QuestionType.MULTIPLE_CHOICE, QuestionType.MULTIPLE_SELECT, QuestionType.TRUE_FALSE):
            if not v:
                raise ValueError('Options are required for multiple-choice, multiple-select and true/false questions')
        return v

    @validator('correct_answer')
    def correct_answer_required(cls, v):
        if not v or not any(a.strip() for a in v):
            raise ValueError('At least one correct answer / acceptable answer is required')
        return v


class QuestionCreate(QuestionBase):
    question_bank_id: int


class QuestionUpdate(BaseModel):
    question_text: Optional[str] = None
    question_type: Optional[QuestionType] = None
    options: Optional[Dict[str, str]] = None
    correct_answer: Optional[List[str]] = None
    explanation: Optional[str] = None
    marks: Optional[float] = None
    difficulty: Optional[str] = None
    topic: Optional[str] = None
    is_active: Optional[bool] = None


class QuestionResponse(QuestionBase):
    id: int
    question_bank_id: int
    created_at: datetime

    class Config:
        from_attributes = True


class QuestionForExam(BaseModel):
    id: int
    question_text: str
    question_type: QuestionType
    options: Dict[str, str]
    marks: float
    difficulty: str
    topic: Optional[str]
    order_index: int


class ExamScheduleTimes(BaseModel):
    @field_validator("start_time", "end_time", mode="after", check_fields=False)
    @classmethod
    def normalize_schedule_time(cls, value):
        # Database timestamps and server-side exam clocks use naive UTC.
        # Convert the instant before dropping tzinfo; simply removing the
        # offset would shift schedules submitted from Nigeria or other zones.
        if value is not None and value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value


class ExamBase(ExamScheduleTimes):
    training_program_id: int
    title: str = Field(..., min_length=5, max_length=200)
    description: Optional[str] = None
    duration_minutes: int = Field(default=120, ge=15, le=300)
    passing_score: float = Field(default=70.0, ge=0, le=100)
    total_marks: float = Field(default=100.0, gt=0)
    questions_per_exam: int = Field(default=50, ge=1, le=200)
    randomize_questions: bool = True
    randomize_options: bool = True
    allow_review: bool = False
    max_attempts: int = Field(default=1, ge=1, le=10)
    start_time: datetime
    end_time: datetime
    instructions: Optional[str] = None


class ExamCreate(ExamBase):
    batch_id: int = Field(..., gt=0)
    question_bank_ids: List[int]


class ExamUpdate(ExamScheduleTimes):
    batch_id: Optional[int] = Field(default=None, gt=0)
    title: Optional[str] = None
    description: Optional[str] = None
    duration_minutes: Optional[int] = None
    passing_score: Optional[float] = None
    total_marks: Optional[float] = None
    questions_per_exam: Optional[int] = None
    randomize_questions: Optional[bool] = None
    randomize_options: Optional[bool] = None
    allow_review: Optional[bool] = None
    max_attempts: Optional[int] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    status: Optional[ExamStatus] = None
    instructions: Optional[str] = None


class ExamPolicyUpdate(BaseModel):
    allow_resume: bool = True
    allow_previous: bool = True
    allow_revisit: bool = True
    sequential: bool = False
    allow_skip: bool = True
    unanswered_zero: bool = True
    negative_marking: float = Field(default=0.0, ge=0, le=100)
    random_question_selection: bool = True
    random_option_order: bool = True
    topic_distribution: Dict[str, int] = {}
    require_fullscreen: bool = True
    max_tab_switches: int = Field(default=3, ge=0, le=100)
    block_clipboard: bool = True
    block_printing: bool = True
    snapshot_questions: bool = True
    payment_required: bool = False


class AppealCreate(BaseModel):
    attempt_id: int
    reason: str = Field(..., min_length=20, max_length=5000)
    evidence_url: Optional[str] = None


class AppealReview(BaseModel):
    status: str = Field(..., pattern="^(under_review|approved|upheld|dismissed)$")
    resolution: str = Field(..., min_length=5, max_length=5000)


class ReceiptReview(BaseModel):
    status: str = Field(..., pattern="^(approved|rejected)$")
    notes: Optional[str] = None


class NotificationCreate(BaseModel):
    user_ids: List[int] = []
    title: str = Field(..., min_length=3, max_length=200)
    message: str = Field(..., min_length=3, max_length=5000)
    notification_type: str = "announcement"
    scheduled_for: Optional[datetime] = None
    send_email: bool = False
    training_program_id: Optional[int] = None
    batch_id: Optional[int] = None


class StaffLoginDecision(BaseModel):
    decision: str = Field(..., pattern="^(approved|rejected)$")
    authority: str = Field(..., pattern="^(head_of_ict|general_manager)$")
    notes: Optional[str] = None


class ExamResponse(ExamBase):
    id: int
    status: ExamStatus
    created_by: int
    created_at: datetime
    question_count: int = 0
    registered_candidates: int = 0

    class Config:
        from_attributes = True


class RetakeGrantRequest(BaseModel):
    reason: Optional[str] = None


class RescheduleRequest(ExamScheduleTimes):
    user_ids: List[int]
    start_time: datetime
    end_time: datetime
    reason: Optional[str] = None


class ExamCandidateRow(BaseModel):
    user_id: int
    full_name: str
    email: str
    institution: Optional[str] = None
    latest_status: str
    attempts_used: int
    max_attempts: int
    has_schedule_override: bool
    override_start: Optional[str] = None
    override_end: Optional[str] = None


class ExamListResponse(BaseModel):
    assigned_candidates: int = 0
    batch_id: Optional[int] = None
    batch_name: Optional[str] = None
    id: int
    training_program_id: int
    title: str
    training_program_name: str
    practice_area: str
    program_description: Optional[str] = None
    exam_description: Optional[str] = None
    instructions: Optional[str] = None
    duration_minutes: int
    passing_score: float
    total_marks: float
    max_attempts: int
    start_time: datetime
    end_time: datetime
    status: ExamStatus
    questions_per_exam: int
    registered_candidates: int
    can_start: bool = True
    payment_required: bool = False
    access_status: Optional[str] = None
    access_message: Optional[str] = None

    class Config:
        from_attributes = True


class ExamPaymentRequirement(BaseModel):
    payment_required: bool


class ExamAccessUpdate(BaseModel):
    user_id: int
    exam_id: int
    granted: bool


class StartExamResponse(BaseModel):
    attempt_id: int
    exam_title: str
    duration_minutes: int
    total_questions: int
    total_marks: float
    passing_score: float
    instructions: Optional[str]
    started_at: datetime
    server_time: datetime


class AnswerSubmit(BaseModel):
    question_id: int
    selected_answer: List[str]
    time_spent_seconds: int = 0


class AnswerResponse(BaseModel):
    question_id: int
    selected_answer: List[str]
    is_correct: Optional[bool] = None
    marks_obtained: Optional[float] = None

    class Config:
        from_attributes = True


class ExamSubmit(BaseModel):
    answers: List[AnswerSubmit]
    time_spent_seconds: int


class ExamResult(BaseModel):
    attempt_id: int
    exam_title: str
    candidate_name: str
    status: AttemptStatus
    total_score: float
    percentage: float
    passed: Optional[bool]
    total_questions: int
    correct_answers: int
    time_spent_minutes: int
    tab_switch_count: int
    fullscreen_exit_count: int
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    approval_notes: Optional[str] = None
    result_released: bool


class ResultApproval(BaseModel):
    attempt_id: int
    action: str = Field(..., pattern="^(approve|reject)$")
    notes: Optional[str] = None


class CandidateResultView(BaseModel):
    attempt_id: int
    exam_title: str
    training_program: str
    exam_date: datetime
    attempt_number: int
    score: float
    total_possible_marks: float
    percentage: float
    passed: bool
    status: str
    certificate_eligible: bool
    certificate_number: Optional[str] = None
    certificate_download_url: Optional[str] = None


class DashboardStats(BaseModel):
    total_candidates: int
    total_exams: int
    total_questions: int
    active_exams: int
    pending_approvals: int
    pending_payments: int = 0
    recent_attempts: List[Dict[str, Any]]


class ExamAnalytics(BaseModel):
    exam_id: int
    exam_title: str
    total_attempts: int
    completed_attempts: int
    average_score: float
    pass_rate: float
    highest_score: float
    lowest_score: float
    passed_count: int = 0
    failed_count: int = 0
    score_distribution: List[Dict[str, Any]] = []
    difficulty_breakdown: Dict[str, Any] = {}
    topic_performance: List[Dict[str, Any]] = []
    flagged_attempts: int = 0


class AttemptAnswerDetail(BaseModel):
    question_id: int
    question_text: str
    question_type: str
    topic: Optional[str] = None
    difficulty: Optional[str] = None
    options: Dict[str, Any]
    correct_answer: List[str]
    selected_answer: List[str]
    is_correct: Optional[bool]
    marks_obtained: float
    marks_available: float
    explanation: Optional[str] = None


class AttemptScriptResponse(BaseModel):
    attempt_id: int
    candidate_name: str
    candidate_email: str
    exam_title: str
    status: str
    total_score: float
    total_possible_marks: float
    percentage: float
    passed: Optional[bool]
    started_at: Optional[datetime]
    submitted_at: Optional[datetime]
    time_spent_seconds: int
    answers: List[AttemptAnswerDetail]


class ActivityTimelineEntry(BaseModel):
    type: str
    timestamp: datetime
    details: Dict[str, Any] = {}


class AttemptTimelineResponse(BaseModel):
    attempt_id: int
    candidate_name: str
    exam_title: str
    started_at: Optional[datetime]
    submitted_at: Optional[datetime]
    ip_address: Optional[str]
    user_agent: Optional[str]
    tab_switch_count: int
    fullscreen_exit_count: int
    risk_score: int
    risk_level: str
    events: List[ActivityTimelineEntry]


class CandidatePerformanceRow(BaseModel):
    user_id: int
    full_name: str
    email: str
    training_program: Optional[str]
    institution: Optional[str]
    exams_taken: int
    exams_passed: int
    average_percentage: float
    best_percentage: float
    latest_attempt_date: Optional[datetime]


class TopicPerformanceRow(BaseModel):
    topic: str
    times_asked: int
    times_correct: int
    accuracy: float


class PerformanceReport(BaseModel):
    candidates: List[CandidatePerformanceRow]
    topic_breakdown: List[TopicPerformanceRow]
    score_distribution: List[Dict[str, Any]]
    overall_average: float
    overall_pass_rate: float
    total_attempts_considered: int
    median_score: float = 0
    score_standard_deviation: float = 0
    highest_score: float = 0
    lowest_score: float = 0


class AuditLogResponse(BaseModel):
    id: int
    user_email: Optional[str]
    action: str
    entity_type: Optional[str]
    entity_id: Optional[int]
    details: Optional[Dict]
    ip_address: Optional[str]
    created_at: datetime
    user_role: Optional[str] = None
    training_program_id: Optional[int] = None

    class Config:
        from_attributes = True


class ExamRenewalApprovalUpdate(BaseModel):
    approved: bool
    reason: str = Field(..., min_length=5, max_length=2000)


class TrainingBatchCreate(BaseModel):
    training_program_id: int
    name: str = Field(..., min_length=2, max_length=150)


class CandidateBatchUpdate(BaseModel):
    batch_id: int
    user_ids: List[int] = Field(..., min_length=1, max_length=500)


class ExamRosterApply(BaseModel):
    batch_id: int
    user_ids: List[int] = Field(..., min_length=1, max_length=500)
