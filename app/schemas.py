from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    phone: str
    otp: str


class UserOut(BaseModel):
    id: UUID
    first_name: str
    last_name: str
    phone: str
    organization_id: UUID
    role: str  # manager | executor
    full_name: str

    model_config = {"from_attributes": True}


class BrandOut(BaseModel):
    id: UUID
    name: str
    description: str | None = None
    image: str | None = None

    model_config = {"from_attributes": True}


class BrandCreate(BaseModel):
    name: str
    description: str | None = None
    image: str | None = None  # S3 key or URL
    storage_key: str | None = None


class BrandUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    image: str | None = None
    storage_key: str | None = None


class TeamUserCreate(BaseModel):
    first_name: str
    last_name: str = ""
    phone: str
    role: str = "executor"  # executor | supervisor
    brand_id: UUID | None = None  # required for supervisor

    @field_validator("brand_id", mode="before")
    @classmethod
    def empty_brand_to_none(cls, v):
        if v == "" or v is None:
            return None
        return v

    @field_validator("first_name")
    @classmethod
    def require_first_name(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("First name is required")
        return v.strip()

    @field_validator("phone")
    @classmethod
    def require_phone(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("Phone is required")
        return v.strip()


class TeamMemberOut(BaseModel):
    id: UUID
    first_name: str
    last_name: str
    full_name: str
    phone: str
    role: str
    brand_id: UUID | None = None
    brand_name: str | None = None
    active: bool = True


class CampaignAssignMembers(BaseModel):
    executor_ids: list[UUID] = []
    supervisor_ids: list[UUID] = []


class ProofImageOut(BaseModel):
    url: str
    slot: str = "photo_1"
    latitude: float | None = None
    longitude: float | None = None
    captured_at: str | None = None


class CreativeTaskOut(BaseModel):
    verification_code: str | None = None
    generated_image_url: str | None = None
    verification_status: str
    detected_code: str | None = None
    detection_method: str | None = None
    verification_notes: str | None = None
    # The automatic check found no task's code in the proof photo.
    code_missing: bool = False
    proof_uploaded_at: datetime | None = None
    verified_at: datetime | None = None


class TaskOut(BaseModel):
    id: UUID
    campaign_id: UUID
    campaign_name: str | None = None
    sequence_number: int = 0
    executor_user_id: UUID
    executor_name: str | None = None
    status: str
    latitude: float | None = None
    longitude: float | None = None
    within_geofence: bool | None = None
    required_photos: int = 1
    service_type: str | None = None
    detail_form: str | None = None
    capture_slots: list[str] = []
    details_complete: bool = False
    proof_images: list[ProofImageOut] = []
    proof_image_count: int = 0
    driver_name: str | None = None
    driver_phone: str | None = None
    vehicle_number: str | None = None
    gym_name: str | None = None
    gym_location: str | None = None
    notes: str | None = None
    flagged: bool = False
    created_at: datetime
    completed_at: datetime | None = None
    creative: CreativeTaskOut | None = None


class ExecutorCampaignSummary(BaseModel):
    id: UUID
    name: str
    service_type: str | None = None
    status: str
    address: str | None = None
    location_label: str | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    task_count: int = 0
    pending_count: int = 0


class ExecutorTasksPage(BaseModel):
    items: list[TaskOut]
    page: int = 1
    page_size: int = 15
    total: int = 0
    next_open_task_id: UUID | None = None


class TaskDetailsSubmit(BaseModel):
    driver_name: str | None = None
    driver_phone: str | None = None
    vehicle_number: str | None = None
    gym_name: str | None = None
    gym_location: str | None = None


class ProofImageCreate(BaseModel):
    storage_key: str
    url: str
    latitude: float
    longitude: float
    slot: str = "photo_1"
    captured_at: datetime | None = None


class CampaignCreate(BaseModel):
    name: str
    brand_id: UUID | None = None
    service_type: str
    description: str = ""
    address: str | None = None
    center_latitude: float
    center_longitude: float
    radius_km: float = Field(default=3.0, ge=0.1, le=500)
    total_tasks: int = Field(ge=1, le=10000)
    executor_ids: list[UUID] = []
    start_date: datetime | None = None
    end_date: datetime | None = None
    # Creative only: storage_key from /api/uploads/presign-creative, after uploading
    source_image_key: str | None = None


class CampaignUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    status: str | None = None
    address: str | None = None
    brand_id: UUID | None = None
    center_latitude: float | None = None
    center_longitude: float | None = None
    radius_km: float | None = Field(default=None, ge=0.1, le=500)
    start_date: datetime | None = None
    end_date: datetime | None = None


class CampaignAddTasks(BaseModel):
    count: int = Field(default=1, ge=1, le=500)
    executor_user_id: UUID | None = None
    manager_owned: bool = False


class CampaignAssignExecutors(BaseModel):
    executor_ids: list[UUID]


class CampaignAssignSupervisors(BaseModel):
    supervisor_ids: list[UUID] = []


class CampaignListItem(BaseModel):
    id: UUID
    name: str
    brand_id: UUID | None = None
    brand: BrandOut | None = None
    service_type: str | None = None
    status: str
    center_latitude: float | None = None
    center_longitude: float | None = None
    radius_km: float = 3.0
    address: str | None = None
    total_tasks: int
    created_at: datetime
    start_date: datetime | None = None
    end_date: datetime | None = None
    logo: str | None = None
    task_count: int = 0
    completed_task_count: int = 0
    pending_task_count: int = 0
    proof_image_count: int = 0


class MemberOut(BaseModel):
    id: UUID
    user_id: UUID
    full_name: str
    phone: str
    role: str
    location: str | None = None
    assigned_by: str | None = None
    active: bool = True


class CreativeCampaignOut(BaseModel):
    source_image_url: str
    generation_status: str
    generated_count: int = 0
    generation_error: str | None = None
    print_pdf_ready: bool = False
    print_pdf_pages: int | None = None
    print_pdf_generated_at: datetime | None = None


class CampaignOut(CampaignListItem):
    description: str = ""
    photos_per_task: int = 1
    organization_id: UUID
    tasks: list[TaskOut] = []
    executors: list[UserOut] = []
    task_assignees: list[UserOut] = []
    members: list[MemberOut] = []
    # pagination for tasks
    tasks_page: int = 1
    tasks_page_size: int = 24
    tasks_total: int = 0
    creative: CreativeCampaignOut | None = None


class CampaignAddTasksResponse(BaseModel):
    campaign: CampaignOut
    task_ids: list[UUID]


class PresignRequest(BaseModel):
    filename: str
    content_type: str = "image/jpeg"
    task_id: UUID


class CreativePresignRequest(BaseModel):
    filename: str
    content_type: str = "image/jpeg"


class BrandPresignRequest(BaseModel):
    filename: str
    content_type: str = "image/jpeg"
    brand_id: UUID | None = None


class PresignResponse(BaseModel):
    upload_url: str
    storage_key: str
    public_url: str
    use_direct_put: bool


class CreativeReportTotals(BaseModel):
    total_tasks: int = 0
    not_submitted: int = 0
    pending_verification: int = 0
    verified: int = 0
    failed: int = 0
    needs_review: int = 0
    success_rate: float = 0.0  # verified ÷ submitted proofs, %


class CreativeReportItem(BaseModel):
    task_id: UUID
    sequence_number: int = 0
    executor_user_id: UUID
    executor_name: str | None = None
    verification_code: str | None = None
    verification_status: str
    detected_code: str | None = None
    detection_method: str | None = None
    ocr_confidence: float | None = None
    generated_image_url: str | None = None
    proof_image_url: str | None = None
    proof_uploaded_at: datetime | None = None
    verified_at: datetime | None = None
    verification_attempts: int = 0
    verification_notes: str | None = None
    code_missing: bool = False


class CreativeReport(BaseModel):
    campaign_id: UUID
    campaign_name: str
    generation: CreativeCampaignOut
    totals: CreativeReportTotals
    items: list[CreativeReportItem]
    page: int = 1
    page_size: int = 50
    items_total: int = 0
    # e.g. "every hour"; None when scheduled checks are off (only "Verify now").
    verify_schedule: str | None = None


class CreativeCodeUpdate(BaseModel):
    code: str | None = None  # omit to generate a fresh code


class CreativeManualVerify(BaseModel):
    status: Literal["VERIFIED", "FAILED"]
    note: str | None = None
