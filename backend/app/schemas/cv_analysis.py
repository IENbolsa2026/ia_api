from uuid import UUID
from enum import Enum
from pydantic import BaseModel, Field
from typing import Any

class CVAnalysisStatus(str, Enum):
    queued = "queued"
    processing = "processing"
    completed = "completed"
    failed = "failed"

class CVAnalysisRequest(BaseModel):
    candidate_id: int = Field(gt=0)
    cv_text: str = Field(min_length=1)


class CVAnalysisAccepted(BaseModel):
    job_id: UUID
    status: CVAnalysisStatus


class CVAnalysisDetail(CVAnalysisAccepted):
    candidate_id: int
    result: dict[str, Any] | None = None
    error: str | None = None