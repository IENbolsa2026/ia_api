"""Modelos Pydantic de los contratos v1.

Los límites hacen que payloads de CV y ofertas no puedan ocupar memoria o
contexto sin control. Los textos siguen siendo datos no confiables.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SkillLevel(str, Enum):
    beginner = "beginner"
    intermediate = "intermediate"
    advanced = "advanced"
    expert = "expert"
    unknown = "unknown"


class WorkMode(str, Enum):
    onsite = "onsite"
    remote = "remote"
    hybrid = "hybrid"
    unknown = "unknown"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Skill(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    level: SkillLevel = SkillLevel.unknown
    years_experience: float | None = Field(default=None, ge=0, le=80)
    evidence: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("evidence")
    @classmethod
    def limit_evidence(cls, values: list[str]) -> list[str]:
        if any(not item or len(item) > 300 for item in values):
            raise ValueError("Each evidence item must contain 1 to 300 characters.")
        return values


class Experience(StrictModel):
    job_title: str = Field(min_length=1, max_length=160)
    organization: str | None = Field(default=None, max_length=160)
    duration_months: int | None = Field(default=None, ge=0, le=960)
    highlights: list[str] = Field(default_factory=list, max_length=8)


class Education(StrictModel):
    level: Literal["secondary", "technical", "bachelor", "master", "doctorate", "other", "unknown"]
    field: str | None = Field(default=None, max_length=160)
    institution: str | None = Field(default=None, max_length=160)
    completed: bool | None = None


class Language(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    level: Literal["basic", "conversational", "professional", "native", "unknown"] = "unknown"


class CandidatePreferences(StrictModel):
    locations: list[str] = Field(default_factory=list, max_length=10)
    work_modes: list[WorkMode] = Field(default_factory=list, max_length=3)


class CandidateProfile(StrictModel):
    schema_version: Literal["candidate-profile.v1"] = "candidate-profile.v1"
    profile_id: str = Field(min_length=1, max_length=64)
    headline: str | None = Field(default=None, max_length=180)
    summary: str | None = Field(default=None, max_length=2000)
    skills: list[Skill] = Field(default_factory=list, max_length=100)
    work_experience: list[Experience] = Field(default_factory=list, max_length=50)
    education: list[Education] = Field(default_factory=list, max_length=20)
    languages: list[Language] = Field(default_factory=list, max_length=20)
    preferences: CandidatePreferences = Field(default_factory=CandidatePreferences)


class Requirement(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    minimum_level: SkillLevel = SkillLevel.unknown
    minimum_years: float | None = Field(default=None, ge=0, le=80)
    mandatory: bool = True
    exclusionary: bool = False
    evidence: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def exclusionary_must_be_mandatory(self) -> "Requirement":
        if self.exclusionary and not self.mandatory:
            raise ValueError("An exclusionary requirement must be mandatory.")
        return self


class JobOffer(StrictModel):
    schema_version: Literal["job-offer.v1"] = "job-offer.v1"
    offer_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=180)
    summary: str | None = Field(default=None, max_length=2500)
    location: str | None = Field(default=None, max_length=100)
    work_mode: WorkMode = WorkMode.unknown
    minimum_experience_months: int | None = Field(default=None, ge=0, le=960)
    education_levels: list[str] = Field(default_factory=list, max_length=10)
    languages: list[Language] = Field(default_factory=list, max_length=20)
    requirements: list[Requirement] = Field(default_factory=list, max_length=100)


class MatchMetadata(StrictModel):
    candidate_profile_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_profile_version: int = Field(ge=1)
    offer_profile_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    offer_profile_version: int = Field(ge=1)
    rubric_version: str = Field(min_length=1, max_length=64)
    matcher_prompt_version: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=120)


class MatchRequest(StrictModel):
    protocol_version: Literal["match.v1"] = "match.v1"
    request_id: UUID
    candidate_profile: CandidateProfile
    job_offer: JobOffer
    metadata: MatchMetadata


class RequirementAssessment(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    score: float = Field(ge=0, le=1)
    status: Literal["met", "partial", "not_met", "unknown"]
    mandatory: bool
    explanation: str = Field(max_length=500)


class MatchResult(StrictModel):
    schema_version: Literal["match-result.v1"] = "match-result.v1"
    request_id: UUID
    status: Literal["completed"] = "completed"
    match_percentage: float = Field(ge=0, le=100)
    coverage_percentage: float = Field(ge=0, le=100)
    confidence_percentage: float = Field(ge=0, le=100)
    assessments: list[RequirementAssessment] = Field(default_factory=list, max_length=100)
    summary: str = Field(max_length=1000)
