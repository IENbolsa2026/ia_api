"""Rúbrica v1: Groq entrega juicios atómicos y este módulo combina pesos."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import math

from ien_contracts.models import MatchResult, RequirementAssessment


WEIGHTS = {
    "mandatory_skills": 0.45,
    "experience": 0.25,
    "education_and_languages": 0.15,
    "preferences_and_desirables": 0.15,
}
RUBRIC_PROMPT_VERSION = 'match-rubric.v1'
RUBRIC_PROMPT = (Path(__file__).resolve().parents[1] / 'prompts' / 'match-rubric.v1.md').read_text(encoding='utf-8').strip()


def questions() -> dict[str, Any]:
    levels = ["No demonstrated fit", "Limited fit", "Partial fit", "Strong fit", "Excellent fit"]
    return {
        "mandatory_skills": {"type": "score", "instructions": [RUBRIC_PROMPT, "Assess fit for the mandatory skills in job_offer using only evidence in candidate_profile."], "criteria": levels},
        "experience": {"type": "score", "instructions": [RUBRIC_PROMPT, "Assess whether documented work experience meets job_offer expectations. Missing evidence is not evidence of absence."], "criteria": levels},
        "education_and_languages": {"type": "score", "instructions": [RUBRIC_PROMPT, "Assess explicitly requested education and language requirements only."], "criteria": levels},
        "preferences_and_desirables": {"type": "score", "instructions": [RUBRIC_PROMPT, "Assess work-mode, location and non-mandatory requirements without treating them as exclusionary."], "criteria": levels},
        "exclusionary_requirements": {"type": "noul", "instructions": [RUBRIC_PROMPT, "Do the candidate_profile data support every exclusionary mandatory requirement in job_offer? Answer yes only with evidence."], "criteria": {"true": "All exclusionary requirements are supported", "false": "At least one is unsupported or missing"}},
    }


def _score(answer: dict[str, Any]) -> float:
    value = float(answer.get("score", 0))
    return min(1.0, max(0.0, value / 4.0))


def _confidence(answer: dict[str, Any]) -> float:
    return min(1.0, max(0.0, float(answer.get("confidence", 0))))


def _assess_requirements(candidate: dict[str, Any], offer: dict[str, Any]) -> tuple[list[RequirementAssessment], float]:
    skills = {str(item.get("name", "")).casefold(): item for item in candidate.get("skills", [])}
    levels = {"unknown": 0, "beginner": 1, "intermediate": 2, "advanced": 3, "expert": 4}
    assessments: list[RequirementAssessment] = []
    known = 0
    requirements = offer.get("requirements", [])
    for requirement in requirements:
        name = str(requirement["name"])
        found = skills.get(name.casefold())
        mandatory = bool(requirement.get("mandatory", True))
        if found is None:
            assessments.append(RequirementAssessment(name=name, score=0, status="unknown", mandatory=mandatory, explanation="No matching evidence was found in the profile."))
            continue
        known += 1
        expected = levels.get(requirement.get("minimum_level", "unknown"), 0)
        actual = levels.get(found.get("level", "unknown"), 0)
        score = 1.0 if expected == 0 else min(1.0, actual / expected)
        status = "met" if score >= 1 else "partial" if score > 0 else "not_met"
        assessments.append(RequirementAssessment(name=name, score=score, status=status, mandatory=mandatory, explanation="Assessment based on the profile skill and its recorded evidence."))
    coverage = (known / len(requirements)) if requirements else 1.0
    return assessments, coverage


def build_result(request: dict[str, Any], jev_response: dict[str, Any]) -> MatchResult:
    answers = jev_response["answers"]
    for key in WEIGHTS:
        answer = answers.get(key, {})
        for field, maximum in [('score', 4), ('confidence', 1)]:
            value = answer.get(field)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= maximum:
                raise ValueError(f'Invalid provider answer: {key}.{field}')
    weighted = sum(WEIGHTS[key] * _score(answers.get(key, {})) for key in WEIGHTS)
    assessments, coverage = _assess_requirements(request["candidate_profile"], request["job_offer"])
    exclusionary = [item for item in request["job_offer"].get("requirements", []) if item.get("exclusionary")]
    exclusionary_supported = float(answers.get("exclusionary_requirements", {}).get("noul", 0)) >= 0.5
    if exclusionary and not exclusionary_supported:
        weighted = min(weighted, 0.40)
    confidence_answers = [answers.get(key, {}) for key in WEIGHTS]
    confidence = sum(_confidence(answer) for answer in confidence_answers) / len(confidence_answers)
    return MatchResult(
        request_id=request["request_id"],
        match_percentage=round(weighted * 100, 2),
        coverage_percentage=round(coverage * 100, 2),
        confidence_percentage=round(confidence * 100, 2),
        assessments=assessments,
        summary="Compatibility is based on the documented profile evidence and the current job-offer rubric.",
    )
