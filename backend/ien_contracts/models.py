"""Modelos Pydantic de los contratos v1.

Los límites hacen que payloads de CV y ofertas no puedan ocupar memoria o
contexto sin control. Los textos siguen siendo datos no confiables.
"""

from __future__ import annotations

from enum import Enum
from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator


class SkillLevel(str, Enum):
    beginner = "beginner"
    intermediate = "intermediate"
    advanced = "advanced"
    expert = "expert"
    unknown = "unknown"


class SkillKind(str, Enum):
    technical = "technical"
    soft = "soft"
    unknown = "unknown"


class SkillOrigin(str, Enum):
    declared = "declared"
    inferred = "inferred"
    user_declared = "user_declared"


class RequirementCategory(str, Enum):
    technical_skills = "technical_skills"
    soft_skills = "soft_skills"
    experience = "experience"
    education = "education"
    languages = "languages"
    logistics = "logistics"
    desirables = "desirables"


MATCH_DIMENSIONS = tuple(item.value for item in RequirementCategory)
DEFAULT_SCORING_WEIGHTS = {
    "technical_skills": 3500,
    "soft_skills": 1500,
    "experience": 2000,
    "education": 1000,
    "languages": 500,
    "logistics": 500,
    "desirables": 1000,
}


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
    source_quotes: list[str] = Field(default_factory=list, max_length=8)
    kind: SkillKind = SkillKind.unknown
    origin: SkillOrigin = SkillOrigin.declared
    concept_id: str | None = Field(default=None, max_length=120)

    @field_validator("evidence")
    @classmethod
    def limit_evidence(cls, values: list[str]) -> list[str]:
        if any(not item or len(item) > 300 for item in values):
            raise ValueError("Each evidence item must contain 1 to 300 characters.")
        return values

    @field_validator("source_quotes")
    @classmethod
    def limit_source_quotes(cls, values: list[str]) -> list[str]:
        if any(not item or len(item) > 500 for item in values):
            raise ValueError("Cada cita original debe contener entre 1 y 500 caracteres.")
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
    completion_status: Literal['completed', 'in_progress', 'incomplete', 'unknown'] = 'unknown'
    source_quotes: list[str] = Field(default_factory=list, max_length=8)

    @field_validator('source_quotes')
    @classmethod
    def education_quotes_must_be_nonempty(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 500 for value in values):
            raise ValueError('Las citas de estudios deben contener entre 1 y 500 caracteres.')
        return values


class Language(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    level: Literal["basic", "conversational", "professional", "native", "unknown"] = "unknown"


class Credential(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    issuer: str | None = Field(default=None, max_length=160)
    issued_at: str | None = Field(default=None, max_length=40)
    expires_at: str | None = Field(default=None, max_length=40)
    evidence: list[str] = Field(default_factory=list, max_length=8)
    source_quotes: list[str] = Field(default_factory=list, max_length=8)
    origin: Literal["declared", "user_declared"] = "declared"

    @field_validator("evidence", "source_quotes")
    @classmethod
    def credential_text_must_be_nonempty(cls, values: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 500 for item in values):
            raise ValueError("La evidencia y las citas de una credencial deben contener texto válido.")
        return values


class RejectedSkill(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    concept_id: str | None = Field(default=None, max_length=120)
    reason: str | None = Field(default=None, max_length=300)


class CandidatePreferences(StrictModel):
    locations: list[str] = Field(default_factory=list, max_length=10)
    work_modes: list[WorkMode] = Field(default_factory=list, max_length=3)


class CandidateProfile(StrictModel):
    schema_version: Literal["candidate-profile.v1", "candidate-profile.v2"] = "candidate-profile.v1"
    profile_id: str = Field(min_length=1, max_length=64)
    headline: str | None = Field(default=None, max_length=180)
    summary: str | None = Field(default=None, max_length=2000)
    skills: list[Skill] = Field(default_factory=list, max_length=100)
    work_experience: list[Experience] = Field(default_factory=list, max_length=50)
    education: list[Education] = Field(default_factory=list, max_length=20)
    languages: list[Language] = Field(default_factory=list, max_length=20)
    preferences: CandidatePreferences = Field(default_factory=CandidatePreferences)
    credentials: list[Credential] = Field(default_factory=list, max_length=50)
    rejected_skills: list[RejectedSkill] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def inferred_skills_need_evidence(self) -> "CandidateProfile":
        if self.schema_version == "candidate-profile.v2":
            if any(not skill.evidence or (skill.origin != SkillOrigin.user_declared and not skill.source_quotes) for skill in self.skills):
                raise ValueError("Cada habilidad de CV en un perfil v2 debe incluir evidencia y citas originales.")
            if any(skill.origin == SkillOrigin.user_declared and not skill.evidence for skill in self.skills):
                raise ValueError("Cada habilidad declarada por el usuario debe conservar su evidencia de declaración.")
            if any(credential.origin == "declared" and (not credential.evidence or not credential.source_quotes) for credential in self.credentials):
                raise ValueError("Cada credencial declarada de un perfil v2 debe incluir evidencia y citas del CV original.")
        return self


class Requirement(StrictModel):
    id: str | None = Field(default=None, min_length=1, max_length=100)
    category: RequirementCategory | None = None
    source: str | None = Field(default=None, max_length=160)
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
    schema_version: Literal["job-offer.v1", "job-offer.v2"] = "job-offer.v1"
    offer_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=180)
    summary: str | None = Field(default=None, max_length=2500)
    location: str | None = Field(default=None, max_length=100)
    work_mode: WorkMode = WorkMode.unknown
    minimum_experience_months: int | None = Field(default=None, ge=0, le=960)
    education_levels: list[str] = Field(default_factory=list, max_length=10)
    languages: list[Language] = Field(default_factory=list, max_length=20)
    requirements: list[Requirement] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_requirement_ids(self) -> "JobOffer":
        ids = [item.id for item in self.requirements if item.id is not None]
        if len(ids) != len(set(ids)):
            raise ValueError("Los identificadores de requisitos deben ser únicos.")
        return self


class MatchMetadata(StrictModel):
    candidate_profile_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_profile_version: int = Field(ge=1)
    offer_profile_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    offer_profile_version: int = Field(ge=1)
    rubric_version: str = Field(min_length=1, max_length=64)
    matcher_prompt_version: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=120)
    evaluation_date: date | None = None


class MatchRequest(StrictModel):
    protocol_version: Literal["match.v1", "match.v2"] = "match.v1"
    request_id: UUID
    candidate_profile: CandidateProfile
    job_offer: JobOffer
    metadata: MatchMetadata
    scoring_config: "ScoringConfig | None" = None

    @model_validator(mode="after")
    def scoring_config_for_v2(self) -> "MatchRequest":
        if self.protocol_version == "match.v2" and self.scoring_config is None:
            raise ValueError("La configuración de ponderación es obligatoria para match.v2.")
        if self.protocol_version == "match.v2":
            ids = [item.id for item in self.job_offer.requirements]
            if any(item_id is None for item_id in ids) or len(ids) != len(set(ids)):
                raise ValueError("Cada requisito de match.v2 debe tener un identificador único.")
            if any(item.category is None for item in self.job_offer.requirements):
                raise ValueError("Cada requisito de match.v2 debe incluir una categoría.")
            for item in self.job_offer.requirements:
                if "mandatory" not in item.model_fields_set:
                    raise ValueError("Cada requisito de match.v2 debe indicar si es obligatorio.")
                if item.category == RequirementCategory.desirables and item.mandatory:
                    raise ValueError("Un requisito deseable no puede estar marcado como obligatorio.")
                if not item.mandatory and item.category != RequirementCategory.desirables:
                    raise ValueError("Los requisitos no obligatorios deben pertenecer a la categoría de deseables.")
            if self.job_offer.schema_version != "job-offer.v2":
                raise ValueError("match.v2 requiere una oferta job-offer.v2.")
            if self.candidate_profile.schema_version != "candidate-profile.v2":
                raise ValueError("match.v2 requiere un perfil candidate-profile.v2.")
            if self.metadata.evaluation_date is None:
                raise ValueError("match.v2 requiere una fecha de evaluación para comprobar credenciales vigentes.")
            if not any(self.scoring_config.weights[item.category.value] > 0 for item in self.job_offer.requirements):
                raise ValueError("No hay criterios suficientes para calcular compatibilidad con estos pesos.")
        return self


class ScoringConfig(StrictModel):
    version: str = Field(min_length=1, max_length=64)
    weights: dict[str, StrictInt]

    @field_validator("weights")
    @classmethod
    def validate_weights(cls, weights: dict[str, int]) -> dict[str, int]:
        if set(weights) != set(MATCH_DIMENSIONS):
            raise ValueError("La configuración debe incluir exactamente las siete dimensiones de compatibilidad.")
        if any(isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10000 for value in weights.values()):
            raise ValueError("Cada ponderación debe ser un entero entre 0 y 10000.")
        if sum(weights.values()) != 10000:
            raise ValueError("La suma de las ponderaciones debe ser exactamente 10000.")
        return weights


class RequirementAssessment(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    score: float = Field(ge=0, le=1)
    status: Literal["met", "partial", "not_met", "unknown"]
    mandatory: bool
    explanation: str = Field(max_length=500)
    requirement_id: str | None = None
    category: RequirementCategory | None = None
    source: str | None = None
    evidence_refs: list[str] = Field(default_factory=list, max_length=8)
    points_awarded: float | None = Field(default=None, ge=0, le=100)
    points_possible: float | None = Field(default=None, ge=0, le=100)


class MatchDimensionBreakdown(StrictModel):
    score: float = Field(ge=0, le=1)
    configured_weight_bp: int = Field(ge=0, le=10000)
    effective_weight_bp: float = Field(ge=0, le=10000)
    applicable: bool


class MatchBreakdown(StrictModel):
    technical_skills: MatchDimensionBreakdown
    soft_skills: MatchDimensionBreakdown
    experience: MatchDimensionBreakdown
    education: MatchDimensionBreakdown
    languages: MatchDimensionBreakdown
    logistics: MatchDimensionBreakdown
    desirables: MatchDimensionBreakdown


class MatchResult(StrictModel):
    schema_version: Literal["match-result.v1", "match-result.v2"] = "match-result.v1"
    request_id: UUID
    status: Literal["completed"] = "completed"
    match_percentage: float = Field(ge=0, le=100)
    coverage_percentage: float = Field(ge=0, le=100)
    confidence_percentage: float | None = Field(default=None, ge=0, le=100)
    confidence_available: bool = True
    assessments: list[RequirementAssessment] = Field(default_factory=list, max_length=100)
    summary: str = Field(max_length=1000)
    breakdown: MatchBreakdown | None = None
    scoring_config: ScoringConfig | None = None
    mandatory_status: Literal["all_met", "not_met", "unknown"] | None = None
    provider_model: str | None = Field(default=None, max_length=120)
