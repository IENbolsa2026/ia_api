"""Compact named-alias transport for CV extraction calls only."""

from __future__ import annotations

import logging
import html
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ien_contracts.models import (
    CandidateProfile,
    SkillKind,
    SkillLevel,
    SkillOrigin,
    WorkMode,
)
from app.services.cv_education_guard import ground_education, retain_explicit_courses, safe_academic_narrative, validate_credential_completion
from app.services.cv_skill_taxonomy import normalize_expanded_skills


logger = logging.getLogger(__name__)


def _decode_generated_text(value, *, key: str = ''):
    """Decode model-written HTML entities while preserving literal source quotes."""
    if key in {'q', 'source_quotes'}:
        return value
    if isinstance(value, str):
        decoded = html.unescape(value)
        return html.unescape(decoded) if decoded != value else decoded
    if isinstance(value, list):
        return [_decode_generated_text(item) for item in value]
    if isinstance(value, dict):
        return {name: _decode_generated_text(item, key=name) for name, item in value.items()}
    return value
_SAFE_SCHEMA_PATH_PARTS = {
    "v", "id", "h", "s", "sk", "x", "pr", "ed", "l", "p", "c",
    "n", "k", "o", "y", "e", "q", "ci", "t", "m", "f", "i", "d", "w", "st",
}


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class WireSkill(_WireModel):
    name: str = Field(min_length=1, max_length=100, alias="n")
    kind: SkillKind = Field(default=SkillKind.unknown, alias="k")
    level: SkillLevel = Field(default=SkillLevel.unknown, alias="l")
    origin: SkillOrigin = Field(default=SkillOrigin.declared, alias="o")
    years_experience: float | None = Field(default=None, ge=0, le=80, alias="y")
    evidence: list[str] = Field(default_factory=list, max_length=8, alias="e")
    source_quotes: list[str] = Field(default_factory=list, max_length=8, alias="q")
    concept_id: str | None = Field(default=None, max_length=120, alias="ci")

    @field_validator("name")
    @classmethod
    def require_display_name(cls, value):
        if value.casefold().startswith("soft."):
            raise ValueError("El nombre debe ser legible; el identificador de concepto corresponde a ci.")
        return value

    @field_validator("evidence", "source_quotes", mode="before")
    @classmethod
    def accept_one_transport_string(cls, value):
        return [value] if isinstance(value, str) else value


class WireExperience(_WireModel):
    job_title: str = Field(min_length=1, max_length=160, alias="t")
    organization: str | None = Field(default=None, max_length=160, alias="o")
    duration_months: int | None = Field(default=None, ge=0, le=960, alias="m")
    highlights: list[str] = Field(default_factory=list, max_length=8, alias="h")


class WireProject(_WireModel):
    name: str = Field(min_length=1, max_length=160, alias="n")
    highlights: list[str] = Field(default_factory=list, max_length=8, alias="h")
    source_quotes: list[str] = Field(default_factory=list, max_length=8, alias="q")

    @field_validator("source_quotes", mode="before")
    @classmethod
    def accept_one_quote(cls, value):
        return [value] if isinstance(value, str) else value


class WireEducation(_WireModel):
    level: Literal["secondary", "technical", "bachelor", "master", "doctorate", "other", "unknown"] = Field(alias="l")
    field: str | None = Field(default=None, max_length=160, alias="f")
    institution: str | None = Field(default=None, max_length=160, alias="i")
    completed: bool | None = Field(default=None, alias="c")
    completion_status: Literal['completed', 'in_progress', 'incomplete', 'unknown'] = Field(default='unknown', alias='st')
    source_quotes: list[str] = Field(default_factory=list, max_length=8, alias='q')


class WireLanguage(_WireModel):
    name: str = Field(min_length=1, max_length=80, alias="n")
    level: Literal["basic", "conversational", "professional", "native", "unknown"] = Field(default="unknown", alias="l")


class WirePreferences(_WireModel):
    locations: list[str] = Field(default_factory=list, max_length=10, alias="l")
    work_modes: list[WorkMode] = Field(default_factory=list, max_length=3, alias="w")


class WireCredential(_WireModel):
    name: str = Field(min_length=1, max_length=160, alias="n")
    issuer: str | None = Field(default=None, max_length=160, alias="i")
    issued_at: str | None = Field(default=None, max_length=40, alias="d")
    expires_at: str | None = Field(default=None, max_length=40, alias="x")
    evidence: list[str] = Field(default_factory=list, max_length=8, alias="e")
    source_quotes: list[str] = Field(default_factory=list, max_length=8, alias="q")
    origin: Literal["declared", "user_declared"] = Field(default="declared", alias="o")

    @field_validator("evidence", "source_quotes", mode="before")
    @classmethod
    def accept_one_transport_string(cls, value):
        return [value] if isinstance(value, str) else value


class _WireProfileFields(_WireModel):
    headline: str | None = Field(default=None, max_length=180, alias="h")
    summary: str | None = Field(default=None, max_length=2000, alias="s")
    skills: list[WireSkill] = Field(default_factory=list, max_length=100, alias="sk")
    work_experience: list[WireExperience] = Field(default_factory=list, max_length=50, alias="x")
    projects: list[WireProject] = Field(default_factory=list, max_length=30, alias="pr")
    education: list[WireEducation] = Field(default_factory=list, max_length=20, alias="ed")
    languages: list[WireLanguage] = Field(default_factory=list, max_length=20, alias="l")
    preferences: WirePreferences = Field(default_factory=WirePreferences, alias="p")
    credentials: list[WireCredential] = Field(default_factory=list, max_length=50, alias="c")


class WireFragment(_WireProfileFields):
    """A block result. Identity is supplied only by the server at consolidation."""

    summary: str | None = Field(default=None, max_length=500, alias="s")


class WireProfile(_WireProfileFields):
    schema_version: Literal["candidate-profile.v2"] = Field(alias="v")
    profile_id: str = Field(min_length=1, max_length=64, alias="id")


def _dump_wire(value: _WireProfileFields) -> dict:
    return value.model_dump(mode="json", by_alias=True, exclude_defaults=True)


def validate_fragment(raw: dict, source_block: str) -> tuple[dict, dict]:
    """Validate an aliased fragment and return expanded and compact forms."""
    try:
        fragment = WireFragment.model_validate(_decode_generated_text(raw))
    except ValidationError as exc:
        details = []
        for error in exc.errors(include_input=False, include_context=False, include_url=False):
            loc = error.get("loc", ())
            if all(
                (isinstance(part, int) and part >= 0)
                or (isinstance(part, str) and part in _SAFE_SCHEMA_PATH_PARTS)
                for part in loc
            ):
                path = ".".join(str(part) for part in loc) or "?"
            else:
                path = "?"
            error_type = error.get("type", "unknown")
            safe_type = error_type if isinstance(error_type, str) and re.fullmatch(r"[a-z0-9_]+", error_type) else "unknown"
            details.append({"path": path, "type": safe_type})
        logger.warning("CV fragment schema validation failed: %s", details[:20])
        raise ValueError("El proveedor devolvió un fragmento compacto de CV inválido.") from None
    expanded = fragment.model_dump(mode="python")
    _validate_profile_evidence(expanded, source_block)
    validate_credential_completion(expanded['credentials'])
    expanded['skills'] = normalize_expanded_skills(expanded['skills'], source_text=source_block)
    expanded['education'] = ground_education(expanded['education'], source_block)
    expanded = safe_academic_narrative(expanded)
    for field_name, model in (('skills', WireSkill), ('education', WireEducation), ('projects', WireProject)):
        setattr(fragment, field_name, [model.model_validate({
            field.alias or name: row[name] for name, field in model.model_fields.items() if name in row
        }) for row in expanded[field_name]])
    fragment.headline, fragment.summary = expanded['headline'], expanded['summary']
    return expanded, _dump_wire(fragment)


def salvage_fragment(raw: dict, source_block: str) -> tuple[dict, dict, dict[str, int]]:
    """After a failed model retry, retain only individually grounded facts.

    A bad optional row must not discard every valid skill in a CV. This path
    never invents a quote or awards a degree: it validates each row with the
    same strict contract and removes rows that still fail. It is not used for
    malformed transport objects.
    """
    try:
        wire = WireFragment.model_validate(_decode_generated_text(raw))
    except ValidationError:
        raise ValueError("El proveedor devolvió un fragmento compacto de CV inválido.") from None
    data = wire.model_dump(mode="python", by_alias=True)
    dropped: dict[str, int] = {}
    for key in ("sk", "ed", "c", "pr"):
        retained = []
        dropped[key] = 0
        for row in data[key]:
            candidate = dict(row)
            try:
                validate_fragment({key: [candidate]}, source_block)
            except ValueError:
                if key == "ed" and candidate.get("f") is not None:
                    # A paraphrased field is not source evidence. Keep the
                    # actual quoted study if its level remains grounded.
                    candidate["f"] = None
                    try:
                        validate_fragment({key: [candidate]}, source_block)
                    except ValueError:
                        dropped[key] += 1
                        continue
                else:
                    dropped[key] += 1
                    continue
            retained.append(candidate)
        data[key] = retained
    for experience in data["x"]:
        highlights = []
        for highlight in experience.get("h", []):
            if not highlight.strip():
                continue
            if len(highlight) > 160:
                prefix = highlight[:160]
                highlight = prefix.rsplit(" ", 1)[0] or prefix
            highlights.append(highlight)
        experience["h"] = highlights
    expanded, compact = validate_fragment(data, source_block)
    if not any(compact.get(key) for key in ("sk", "x", "pr", "ed", "l", "c")):
        raise ValueError("El proveedor no devolvió hechos verificables del CV.")
    return expanded, compact, dropped


def expand_profile(raw: dict, source_text: str, candidate_id: int) -> dict:
    """Expand a strict compact profile into the existing candidate-profile.v2 contract."""
    try:
        wire = WireProfile.model_validate(raw)
    except ValidationError as exc:
        raise ValueError("El proveedor devolvió un perfil compacto de CV inválido.") from exc
    if wire.schema_version != "candidate-profile.v2":
        raise ValueError("El perfil compacto devolvió una versión incompatible.")
    if wire.profile_id != str(candidate_id):
        raise ValueError("El perfil consolidado devolvió una identidad de candidato incorrecta.")
    profile = wire.model_dump(mode="python")
    profile["rejected_skills"] = []
    try:
        validated = CandidateProfile.model_validate(profile)
    except ValidationError as exc:
        raise ValueError("El perfil consolidado no cumple el contrato candidate-profile.v2.") from exc
    expanded = validated.model_dump(mode="json")
    _validate_profile_evidence(expanded, source_text)
    return expanded


def validate_expanded_profile(profile: dict, source_text: str, candidate_id: int) -> dict:
    """Ground academic state and enforce the vocabulary before publishing a profile."""
    validate_credential_completion(profile.get('credentials', []))
    profile = safe_academic_narrative({
        **_decode_generated_text(profile),
        'skills': normalize_expanded_skills(_decode_generated_text(profile.get('skills', [])), source_text=source_text),
        'education': ground_education(retain_explicit_courses(profile.get('education', []), source_text), source_text),
    })
    profile['skills'] = normalize_expanded_skills(
        _restore_project_list_skills(profile.get('skills', []), profile.get('projects', [])),
        source_text=source_text,
    )
    profile['skills'] = _calibrate_project_skill_levels(profile['skills'], profile.get('projects', []))
    try:
        validated = CandidateProfile.model_validate(profile)
    except ValidationError as exc:
        raise ValueError("El perfil ensamblado no cumple el contrato candidate-profile.v2.") from exc
    if validated.schema_version != "candidate-profile.v2" or validated.profile_id != str(candidate_id):
        raise ValueError("El perfil ensamblado devolvió una identidad o versión incompatible.")
    expanded = validated.model_dump(mode="json")
    _validate_profile_evidence(expanded, source_text)
    return expanded


def _restore_project_list_skills(skills: list[dict], projects: list[dict]) -> list[dict]:
    """Preserve explicit tools in project lists when the model omits a row."""
    result = list(skills)
    existing = {str(item.get('name') or '').strip().casefold() for item in result}
    list_label = re.compile(
        r'\b(?:tecnolog[ií]as?|herramientas?|software|stack)\s*'
        r'(?:utilizad[ao]s?|emplead[ao]s?)?\s*:\s*([^\n;]+)', re.I,
    )
    generic = {'framework', 'otros', 'otras', 'herramientas', 'tecnologias', 'software'}
    for project in projects:
        for quote in project.get('source_quotes', []):
            match = list_label.search(quote)
            if not match:
                continue
            for part in re.split(r'\s*,\s*|\s+&\s+|\s+y\s+', match.group(1), flags=re.I):
                name = re.sub(r'^framework\s+', '', part.strip(' .:;!'), flags=re.I).strip()
                typo_of_existing = any(
                    len(name) >= 5 and len(name) == len(known) and name[0].casefold() == known[0]
                    and sorted(name.casefold()) == sorted(known)
                    and SequenceMatcher(None, name.casefold(), known).ratio() >= 0.8
                    for known in existing if known
                )
                if (not name or len(name) > 80 or name.casefold() in generic
                        or name.casefold() in existing or typo_of_existing or len(result) >= 100):
                    continue
                result.append({
                    'name': name, 'kind': 'technical', 'level': 'unknown',
                    'origin': 'declared', 'years_experience': None,
                    'evidence': [f"Herramienta mencionada en el proyecto {project['name']}."[:160]],
                    'source_quotes': [quote], 'concept_id': None,
                })
                existing.add(name.casefold())
    return result


def _calibrate_project_skill_levels(skills: list[dict], projects: list[dict]) -> list[dict]:
    """Credit documented project practice without guessing proficiency from a tool list alone."""
    def fold(value: str) -> str:
        value = unicodedata.normalize('NFKD', value)
        return ''.join(char for char in value if not unicodedata.combining(char)).casefold()

    use_signal = re.compile(r'\b(?:tecnologias?|herramientas?)\s+utilizad|\b(?:use|utilice|implemente|desarrolle|construi|programe|built|used|implemented|developed)\b')
    ownership_signal = re.compile(r'\b(?:implemente|desarrolle|construi|programe|cree|built|implemented|developed)\b')
    calibrated = []
    for original in skills:
        skill = dict(original)
        if skill.get('kind') != 'technical' or skill.get('level') != 'unknown':
            calibrated.append(skill)
            continue
        name = fold(str(skill.get('name') or '').strip())
        if not name:
            calibrated.append(skill)
            continue
        pattern = re.compile(r'(?<!\w)' + re.escape(name) + r'(?!\w)')

        def mentioned(quote: str) -> bool:
            if pattern.search(quote):
                return True
            if len(name) < 5 or not name.isalnum():
                return False
            return any(
                len(token) == len(name) and token[:1] == name[:1]
                and sorted(token) == sorted(name)
                and SequenceMatcher(None, token, name).ratio() >= 0.8
                for token in re.findall(r'\b\w+\b', quote)
            )

        matches = []
        for project in projects:
            for quote in project.get('source_quotes', []):
                folded_quote = fold(quote)
                if mentioned(folded_quote) and use_signal.search(folded_quote):
                    matches.append((project, quote, bool(ownership_signal.search(folded_quote))))
                    break
        if matches:
            evidence = list(skill.get('evidence') or [])
            quotes = list(skill.get('source_quotes') or [])
            project, quote, _owned = matches[0]
            added = f"Práctica documentada en el proyecto {project['name']}."
            if (len(evidence) < 8 and (quote in quotes or len(quotes) < 8) and len(added) <= 160):
                if quote not in quotes:
                    quotes.append(quote)
                evidence.append(added)
                skill['evidence'] = evidence
                skill['source_quotes'] = quotes
                skill['level'] = ('intermediate' if len({p['name'] for p, _q, owned in matches if owned}) >= 2
                                  else 'beginner')
        calibrated.append(skill)
    return calibrated


def _validate_profile_evidence(profile: dict, source_text: str) -> None:
    source = " ".join(source_text.split()).casefold()
    for skill in profile.get("skills", []):
        if skill.get("origin") == "user_declared":
            raise ValueError("La extracción automática no puede crear habilidades declaradas por el usuario.")
        evidence = skill.get("evidence", [])
        quotes = skill.get("source_quotes", [])
        if not evidence or not quotes:
            raise ValueError("Cada habilidad de CV v2 debe incluir evidencia y citas del CV.")
        if any(not item.strip() or len(item) > 160 for item in evidence):
            raise ValueError("La evidencia de una habilidad debe ser breve y no vacía.")
        if any(not quote.strip() or len(quote) > 160 or " ".join(quote.split()).casefold() not in source for quote in quotes):
            raise ValueError("Una habilidad contiene citas que no aparecen en el CV original.")
    for credential in profile.get("credentials", []):
        evidence = credential.get("evidence", [])
        quotes = credential.get("source_quotes", [])
        if credential.get("origin") == "user_declared" or not evidence or not quotes:
            raise ValueError("Cada credencial extraída debe incluir evidencia y citas del CV original.")
        if any(not item.strip() or len(item) > 160 for item in evidence):
            raise ValueError("La evidencia de una credencial debe ser breve y no vacía.")
        if any(not quote.strip() or len(quote) > 160 or " ".join(quote.split()).casefold() not in source for quote in quotes):
            raise ValueError("Una credencial contiene citas que no aparecen en el CV original.")
    for experience in profile.get("work_experience", []):
        if any(not highlight.strip() or len(highlight) > 160 for highlight in experience.get("highlights", [])):
            raise ValueError("Los aspectos destacados deben ser breves y no vacíos.")
    for project in profile.get("projects", []):
        quotes = project.get("source_quotes", [])
        if " ".join(project.get("name", "").split()).casefold() not in source:
            raise ValueError("El nombre del proyecto no aparece en el CV original.")
        if not quotes or any(not quote.strip() or len(quote) > 160 or
                             " ".join(quote.split()).casefold() not in source for quote in quotes):
            raise ValueError("Cada proyecto debe tener citas literales del CV.")
        if any(not highlight.strip() or len(highlight) > 160 for highlight in project.get("highlights", [])):
            raise ValueError("Los aspectos destacados de un proyecto deben ser breves y no vacíos.")


def fragment_schema() -> dict:
    return WireFragment.model_json_schema(by_alias=True)


def profile_schema() -> dict:
    return WireProfile.model_json_schema(by_alias=True)


def contract_profile_to_wire(profile: dict) -> dict:
    """Compact an existing profile without changing or dropping facts."""
    try:
        validated = CandidateProfile.model_validate(profile)
    except ValidationError as exc:
        raise ValueError("No se pudo compactar un perfil candidate-profile.v2 válido.") from exc
    if validated.schema_version != "candidate-profile.v2":
        raise ValueError("Solo se pueden compactar perfiles candidate-profile.v2.")
    if validated.rejected_skills:
        raise ValueError("El transporte compacto no admite habilidades rechazadas.")
    expanded = validated.model_dump(mode="json")
    def aliases(rows: list[dict], fields: dict[str, str]) -> list[dict]:
        return [{alias: row[key] for key, alias in fields.items() if key in row} for row in rows]

    preferences = expanded.get("preferences") or {}
    wire_input = {
            "v": expanded["schema_version"],
            "id": expanded["profile_id"],
            "h": expanded.get("headline"),
            "s": expanded.get("summary"),
            "sk": aliases(expanded.get("skills", []), {
                "name": "n", "kind": "k", "level": "l", "origin": "o",
                "years_experience": "y", "evidence": "e", "source_quotes": "q", "concept_id": "ci",
            }),
            "x": aliases(expanded.get("work_experience", []), {
                "job_title": "t", "organization": "o", "duration_months": "m", "highlights": "h",
            }),
            "pr": aliases(expanded.get("projects", []), {
                "name": "n", "highlights": "h", "source_quotes": "q",
            }),
            "ed": aliases(expanded.get("education", []), {
                "level": "l", "field": "f", "institution": "i", "completed": "c",
                "completion_status": "st", "source_quotes": "q",
            }),
            "l": aliases(expanded.get("languages", []), {"name": "n", "level": "l"}),
            "p": {"l": preferences.get("locations", []), "w": preferences.get("work_modes", [])},
            "c": aliases(expanded.get("credentials", []), {
                "name": "n", "issuer": "i", "issued_at": "d", "expires_at": "x",
                "evidence": "e", "source_quotes": "q", "origin": "o",
            }),
        }
    try:
        wire = WireProfile.model_validate(wire_input)
    except ValidationError as exc:
        raise ValueError("No se pudo compactar un perfil candidate-profile.v2 válido.") from exc
    return _dump_wire(wire)
