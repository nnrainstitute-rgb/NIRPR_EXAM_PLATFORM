"""
NIRPR RSO Examination Platform - Database Models
National Institute of Radiation Protection and Research (NIRPR)
Technical Arm of Nigerian Nuclear Regulatory Authority (NNRA)
"""

from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, ForeignKey,
    Float, JSON, Enum, Table, UniqueConstraint, Index, CheckConstraint
)
from sqlalchemy.orm import relationship
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy import case, func
from datetime import datetime
import enum

from database import Base
from name_utils import compose_full_name, split_full_name

exam_question_association = Table(
    'exam_question_association',
    Base.metadata,
    Column('exam_id', Integer, ForeignKey('exams.id'), primary_key=True),
    Column('question_id', Integer, ForeignKey('questions.id'), primary_key=True),
    Column('order_index', Integer, nullable=False)
)


class UserRole(enum.Enum):
    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"
    EXAMINER = "examiner"
    CANDIDATE = "candidate"


class ExamStatus(enum.Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class AttemptStatus(enum.Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    AUTO_SUBMITTED = "auto_submitted"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class QuestionType(enum.Enum):
    MULTIPLE_CHOICE = "multiple_choice"
    MULTIPLE_SELECT = "multiple_select"
    TRUE_FALSE = "true_false"
    SHORT_ANSWER = "short_answer"
    FILL_IN_GAP = "fill_in_gap"


class TrainingProgram(Base):
    __tablename__ = "training_programs"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(20), unique=True, nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    practice_area = Column(String(100), nullable=False)
    duration_days = Column(Integer, default=5)
    passing_score = Column(Float, default=70.0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    question_banks = relationship("QuestionBank", back_populates="training_program")
    exams = relationship("Exam", back_populates="training_program")
    candidates = relationship("CandidateProfile", back_populates="training_program")


class QuestionBank(Base):
    __tablename__ = "question_banks"

    id = Column(Integer, primary_key=True, index=True)
    training_program_id = Column(Integer, ForeignKey("training_programs.id"), nullable=False)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(Integer, ForeignKey("users.id"))

    training_program = relationship("TrainingProgram", back_populates="question_banks")
    questions = relationship("Question", back_populates="question_bank", cascade="all, delete-orphan")


class Question(Base):
    __tablename__ = "questions"

    id = Column(Integer, primary_key=True, index=True)
    question_bank_id = Column(Integer, ForeignKey("question_banks.id"), nullable=False)
    question_text = Column(Text, nullable=False)
    question_type = Column(Enum(QuestionType), default=QuestionType.MULTIPLE_CHOICE)
    options = Column(JSON, nullable=False)
    correct_answer = Column(JSON, nullable=False)
    explanation = Column(Text)
    marks = Column(Float, default=1.0)
    difficulty = Column(String(20), default="medium")
    topic = Column(String(100))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    question_bank = relationship("QuestionBank", back_populates="questions")
    exams = relationship("Exam", secondary=exam_question_association, back_populates="questions")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    surname = Column(String(100), nullable=False)
    first_name = Column(String(100), nullable=False)
    other_name = Column(String(150))
    phone = Column(String(20))
    role = Column(Enum(UserRole), default=UserRole.CANDIDATE)
    is_active = Column(Boolean, default=True)
    email_verified = Column(Boolean, default=False)
    last_login = Column(DateTime)
    login_attempts = Column(Integer, default=0)
    locked_until = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Email verification
    verification_token = Column(String(255), nullable=True, index=True)
    verification_token_expires = Column(DateTime, nullable=True)

    # Password reset
    reset_token = Column(String(255), nullable=True, index=True)
    reset_token_expires = Column(DateTime, nullable=True)

    # Set true for accounts created directly by an admin (CSV import / staff
    # creation) so the UI can show "credentials sent by email" instead of
    # "please verify your email".
    created_by_admin = Column(Boolean, default=False)
    # Admin-created accounts receive a temporary password and must replace it
    # before any protected portal operation is permitted.
    must_change_password = Column(Boolean, default=False, nullable=False)

    candidate_profile = relationship("CandidateProfile", back_populates="user", uselist=False)
    exam_attempts = relationship("ExamAttempt", back_populates="user", foreign_keys="ExamAttempt.user_id")
    created_questions = relationship("QuestionBank", foreign_keys="QuestionBank.created_by")
    audit_logs = relationship("AuditLog", back_populates="user")

    @hybrid_property
    def full_name(self):
        return compose_full_name(self.surname or "", self.first_name or "", self.other_name)

    @full_name.setter
    def full_name(self, value):
        self.surname, self.first_name, self.other_name = split_full_name(value or "")

    @full_name.expression
    def full_name(cls):
        middle = case((func.length(func.trim(func.coalesce(cls.other_name, ""))) > 0,
                       " " + func.trim(cls.other_name)), else_="")
        return func.trim(cls.first_name + middle + " " + cls.surname)


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    training_program_id = Column(Integer, ForeignKey("training_programs.id"), nullable=False)
    institution = Column(String(200))
    department = Column(String(100))
    qualification = Column(String(100))
    years_of_experience = Column(Integer, default=0)
    nnra_registration_number = Column(String(50))
    practice_type = Column(String(100))
    accreditation_status = Column(String(50), default="pending")

    user = relationship("User", back_populates="candidate_profile")
    training_program = relationship("TrainingProgram", back_populates="candidates")


class Exam(Base):
    __tablename__ = "exams"

    id = Column(Integer, primary_key=True, index=True)
    training_program_id = Column(Integer, ForeignKey("training_programs.id"), nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(Text)
    duration_minutes = Column(Integer, default=120)
    passing_score = Column(Float, default=70.0)
    total_marks = Column(Float, default=100.0)
    questions_per_exam = Column(Integer, default=50)
    randomize_questions = Column(Boolean, default=True)
    randomize_options = Column(Boolean, default=True)
    allow_review = Column(Boolean, default=False)
    max_attempts = Column(Integer, default=1)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    status = Column(Enum(ExamStatus), default=ExamStatus.DRAFT)
    instructions = Column(Text)
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

    training_program = relationship("TrainingProgram", back_populates="exams")
    questions = relationship("Question", secondary=exam_question_association, back_populates="exams")
    attempts = relationship("ExamAttempt", back_populates="exam")


class ExamAttempt(Base):
    __tablename__ = "exam_attempts"

    id = Column(Integer, primary_key=True, index=True)
    exam_id = Column(Integer, ForeignKey("exams.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    attempt_number = Column(Integer, default=1, nullable=False)
    status = Column(Enum(AttemptStatus), default=AttemptStatus.NOT_STARTED)

    started_at = Column(DateTime)
    submitted_at = Column(DateTime)
    time_spent_seconds = Column(Integer, default=0)

    total_score = Column(Float, default=0.0)
    total_possible_marks = Column(Float, default=0.0)
    percentage = Column(Float, default=0.0)
    passed = Column(Boolean)

    approved_by = Column(Integer, ForeignKey("users.id"))
    approved_at = Column(DateTime)
    approval_notes = Column(Text)
    result_released = Column(Boolean, default=False)

    ip_address = Column(String(45))
    user_agent = Column(Text)
    tab_switch_count = Column(Integer, default=0)
    fullscreen_exit_count = Column(Integer, default=0)
    suspicious_activity = Column(JSON, default=list)

    question_set = Column(JSON, default=list)

    created_at = Column(DateTime, default=datetime.utcnow)

    exam = relationship("Exam", back_populates="attempts")
    user = relationship("User", back_populates="exam_attempts", foreign_keys=[user_id])
    answers = relationship("Answer", back_populates="attempt", cascade="all, delete-orphan")
    approver = relationship("User", foreign_keys=[approved_by])

    __table_args__ = (
        UniqueConstraint('exam_id', 'user_id', 'attempt_number', name='unique_exam_attempt_number'),
    )


class RetakeAuthorization(Base):
    """An admin-granted, one-time permission for a specific candidate to sit
    a specific exam again once their previous attempt(s) are used up. Kept
    as its own auditable record (who granted it, when, why) rather than
    just bumping a counter, since this is a regulated certification
    platform where that trail matters."""
    __tablename__ = "retake_authorizations"

    id = Column(Integer, primary_key=True, index=True)
    exam_id = Column(Integer, ForeignKey("exams.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    authorized_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    reason = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    used = Column(Boolean, default=False)
    used_at = Column(DateTime)

    exam = relationship("Exam")
    user = relationship("User", foreign_keys=[user_id])
    authorizer = relationship("User", foreign_keys=[authorized_by])


class ExamScheduleOverride(Base):
    """Lets an admin move a specific candidate's (or set of candidates')
    exam window without changing the exam for everyone else."""
    __tablename__ = "exam_schedule_overrides"

    id = Column(Integer, primary_key=True, index=True)
    exam_id = Column(Integer, ForeignKey("exams.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    reason = Column(Text)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    exam = relationship("Exam")
    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        UniqueConstraint('exam_id', 'user_id', name='unique_schedule_override'),
    )


class Answer(Base):
    __tablename__ = "answers"

    id = Column(Integer, primary_key=True, index=True)
    attempt_id = Column(Integer, ForeignKey("exam_attempts.id"), nullable=False)
    question_id = Column(Integer, ForeignKey("questions.id"), nullable=False)
    selected_answer = Column(JSON, default=list)
    is_correct = Column(Boolean)
    marks_obtained = Column(Float, default=0.0)
    answered_at = Column(DateTime, default=datetime.utcnow)
    time_spent_seconds = Column(Integer, default=0)

    attempt = relationship("ExamAttempt", back_populates="answers")
    question = relationship("Question")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String(100), nullable=False)
    entity_type = Column(String(50))
    entity_id = Column(Integer)
    details = Column(JSON)
    ip_address = Column(String(45))
    user_agent = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="audit_logs")

    __table_args__ = (
        Index('idx_audit_user_time', 'user_id', 'created_at'),
        Index('idx_audit_action', 'action', 'created_at'),
    )


class SystemSetting(Base):
    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(Text)
    description = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(Integer, ForeignKey("users.id"))


class ExamPolicy(Base):
    """Extended delivery rules kept separately so existing installations can
    adopt the new controls without destructive exam-table migrations."""
    __tablename__ = "exam_policies"
    id = Column(Integer, primary_key=True)
    exam_id = Column(Integer, ForeignKey("exams.id"), unique=True, nullable=False)
    allow_resume = Column(Boolean, default=True)
    allow_previous = Column(Boolean, default=True)
    allow_revisit = Column(Boolean, default=True)
    sequential = Column(Boolean, default=False)
    allow_skip = Column(Boolean, default=True)
    unanswered_zero = Column(Boolean, default=True)
    negative_marking = Column(Float, default=0.0)
    random_question_selection = Column(Boolean, default=True)
    random_option_order = Column(Boolean, default=True)
    topic_distribution = Column(JSON, default=dict)
    require_fullscreen = Column(Boolean, default=True)
    max_tab_switches = Column(Integer, default=3)
    block_clipboard = Column(Boolean, default=True)
    block_printing = Column(Boolean, default=True)
    snapshot_questions = Column(Boolean, default=True)
    payment_required = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CandidateIdentity(Base):
    __tablename__ = "candidate_identities"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    candidate_number = Column(String(40), unique=True, nullable=False, index=True)
    examination_number = Column(String(40), unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class QuestionRevision(Base):
    __tablename__ = "question_revisions"
    id = Column(Integer, primary_key=True)
    question_id = Column(Integer, ForeignKey("questions.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    snapshot = Column(JSON, nullable=False)
    action = Column(String(30), default="updated")
    changed_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    changed_at = Column(DateTime, default=datetime.utcnow)
    review_status = Column(String(30), default="pending")
    reviewed_by = Column(Integer, ForeignKey("users.id"))
    reviewed_at = Column(DateTime)
    review_notes = Column(Text)
    __table_args__ = (UniqueConstraint("question_id", "version", name="uq_question_version"),)


class ResultAppeal(Base):
    __tablename__ = "result_appeals"
    id = Column(Integer, primary_key=True)
    attempt_id = Column(Integer, ForeignKey("exam_attempts.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    reason = Column(Text, nullable=False)
    evidence_url = Column(Text)
    status = Column(String(30), default="submitted", index=True)
    resolution = Column(Text)
    reviewed_by = Column(Integer, ForeignKey("users.id"))
    submitted_at = Column(DateTime, default=datetime.utcnow)
    reviewed_at = Column(DateTime)


class PaymentReceipt(Base):
    __tablename__ = "payment_receipts"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    exam_id = Column(Integer, ForeignKey("exams.id"), nullable=False, index=True)
    file_name = Column(String(255), nullable=False)
    stored_path = Column(Text, nullable=False)
    content_type = Column(String(100))
    amount = Column(Float)
    reference = Column(String(100))
    status = Column(String(30), default="pending", index=True)
    review_notes = Column(Text)
    reviewed_by = Column(Integer, ForeignKey("users.id"))
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    reviewed_at = Column(DateTime)
    __table_args__ = (UniqueConstraint("user_id", "exam_id", name="uq_receipt_user_exam"),)


class ExamAccessGrant(Base):
    """Administrative permission to sit an exam without an approved receipt."""
    __tablename__ = "exam_access_grants"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    exam_id = Column(Integer, ForeignKey("exams.id"), nullable=False, index=True)
    granted_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    granted_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("user_id", "exam_id", name="uq_exam_access_user_exam"),)


class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    message = Column(Text, nullable=False)
    notification_type = Column(String(40), default="general")
    read_at = Column(DateTime)
    scheduled_for = Column(DateTime)
    email_sent_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)


class StaffSignature(Base):
    __tablename__ = "staff_signatures"
    id = Column(Integer, primary_key=True)
    role_key = Column(String(50), unique=True, nullable=False)
    signatory_name = Column(String(150), nullable=False)
    title = Column(String(150), nullable=False)
    image_path = Column(Text, nullable=False)
    uploaded_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    uploaded_at = Column(DateTime, default=datetime.utcnow)


class ProgrammeSignature(Base):
    """Certificate signatories scoped to one training programme."""
    __tablename__ = "programme_signatures"
    id = Column(Integer, primary_key=True)
    training_program_id = Column(Integer, ForeignKey("training_programs.id"), nullable=False, index=True)
    role_key = Column(String(50), nullable=False)
    signatory_name = Column(String(150), nullable=False)
    title = Column(String(150), nullable=False)
    image_path = Column(Text, nullable=False)
    uploaded_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("training_program_id", "role_key", name="uq_programme_signature_role"),)


class CandidateTagSignature(Base):
    """The single signature used on all candidate tags."""
    __tablename__ = "candidate_tag_signatures"
    id = Column(Integer, primary_key=True)
    signatory_name = Column(String(150), nullable=False)
    title = Column(String(150), nullable=False)
    image_path = Column(Text, nullable=False)
    uploaded_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (CheckConstraint("id = 1", name="ck_candidate_tag_signature_singleton"),)


class SavedReport(Base):
    """An admin-created immutable statistical report snapshot."""
    __tablename__ = "saved_reports"
    id = Column(Integer, primary_key=True)
    training_program_id = Column(Integer, ForeignKey("training_programs.id"), nullable=True, index=True)
    exam_id = Column(Integer, ForeignKey("exams.id"), nullable=True, index=True)
    schedule_key = Column(String(120), nullable=False, default="general")
    title = Column(String(250), nullable=False)
    report_data = Column(JSON, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id = Column(String(64), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    ip_address = Column(String(45))
    user_agent = Column(Text)
    device_label = Column(String(200))
    created_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime)


class StaffMFA(Base):
    __tablename__ = "staff_mfa"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    secret = Column(String(64), nullable=False)
    enabled = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class StaffLoginApproval(Base):
    __tablename__ = "staff_login_approvals"
    id = Column(String(64), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    poll_token_hash = Column(String(128), nullable=False)
    status = Column(String(30), default="pending", nullable=False, index=True)
    ip_address = Column(String(45))
    user_agent = Column(Text)
    requested_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    approved_by = Column(Integer, ForeignKey("users.id"))
    approver_authority = Column(String(40))
    decision_notes = Column(Text)
    decided_at = Column(DateTime)
    consumed_at = Column(DateTime)


class ExamRenewalApproval(Base):
    """Explicit admission of a returning candidate to a different examination."""
    # Retain the original storage name so approvals already saved locally survive.
    __tablename__ = "exam_resit_invitations"
    id = Column(Integer, primary_key=True)
    exam_id = Column(Integer, ForeignKey("exams.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    invited_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    reason = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("exam_id", "user_id", name="uq_exam_resit_invitation"),)


class TrainingBatch(Base):
    __tablename__ = "training_batches"
    id = Column(Integer, primary_key=True)
    training_program_id = Column(Integer, ForeignKey("training_programs.id"), nullable=False, index=True)
    name = Column(String(150), nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("training_program_id", "name", name="uq_training_batch_name"),)


class CandidateBatchAssignment(Base):
    __tablename__ = "candidate_batch_assignments"
    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    batch_id = Column(Integer, ForeignKey("training_batches.id"), nullable=False, index=True)
    assigned_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    assigned_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ExamBatchAssignment(Base):
    __tablename__ = "exam_batch_assignments"
    exam_id = Column(Integer, ForeignKey("exams.id"), primary_key=True)
    batch_id = Column(Integer, ForeignKey("training_batches.id"), nullable=False, index=True)
