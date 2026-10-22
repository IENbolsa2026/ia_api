"""Closed, evidence-preserving soft-skill vocabulary for CV extraction.

No local inference: only explicit aliases are equivalent. Rejected records must
be regenerated or reviewed by the caller, never silently discarded.
"""

from __future__ import annotations

from copy import deepcopy
import re
import unicodedata
from typing import Any, Literal

from pydantic import ValidationError

from ien_contracts.models import Skill


SOFT_SKILL_TAXONOMY = {
    'Pensamiento crítico': 'soft.critical_thinking',
    'Alfabetización en IA': 'soft.ai_literacy',
    'Inteligencia emocional': 'soft.emotional_intelligence',
    'Learnability': 'soft.learnability',
    'Comunicación': 'soft.communication',
    'Colaboración': 'soft.collaboration',
    'Adaptabilidad al cambio': 'soft.adaptability',
    'Creatividad': 'soft.creativity',
    'Responsabilidad': 'soft.responsibility',
    'Proactividad': 'soft.proactivity',
}

_ALIASES = {
    'Pensamiento crítico': ('critical thinking',),
    'Alfabetización en IA': ('alfabetización en inteligencia artificial', 'ai literacy'),
    'Inteligencia emocional': ('emotional intelligence',),
    'Learnability': ('aprendizaje continuo', 'aprendizaje permanente', 'capacidad de aprendizaje'),
    'Comunicación': ('comunicación efectiva', 'comunicación verbal y escrita',
                     'comunicación efectiva verbal y escrita', 'habilidades de comunicación',
                     'comunicación interpersonal', 'communication'),
    'Colaboración': ('trabajo en equipo', 'trabajo colaborativo', 'teamwork', 'collaboration'),
    'Adaptabilidad al cambio': ('adaptabilidad', 'adaptación al cambio'),
    'Creatividad': ('creativity',),
    'Responsabilidad': ('responsibility',),
    'Proactividad': ('iniciativa', 'proactivity'),
}
_LEGACY_CONCEPTS = {
    'soft.teamwork': 'soft.collaboration',
    'soft.continuous_learning': 'soft.learnability',
    'soft.initiative': 'soft.proactivity',
}


def _key(value: str) -> str:
    text = unicodedata.normalize('NFKD', value)
    text = ''.join(char for char in text if not unicodedata.combining(char))
    return re.sub(r'\s+', ' ', text).strip().casefold()


_BY_NAME = {
    _key(alias): (name, SOFT_SKILL_TAXONOMY[name])
    for name, aliases in _ALIASES.items() for alias in (name, *aliases)
}
_CONCEPTS = {concept: concept for concept in SOFT_SKILL_TAXONOMY.values()} | _LEGACY_CONCEPTS
_OUTSIDE_SOFT_NAMES = {
    _key(name) for name in ('Liderazgo', 'Organización', 'Gestión del tiempo', 'Resolución de problemas',
                           'Atención al detalle', 'Orientación al cliente', 'Pensamiento analítico',
                           'Pensamiento estructurado', 'Pensamiento sistémico')
}


class SkillTaxonomyError(ValueError):
    """Safe machine-readable failure for a bounded extraction retry."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__('Las habilidades extraídas necesitan revisión de taxonomía o evidencia.')


def non_competency_kind(name: str) -> Literal['course', 'degree', 'role'] | None:
    """Detect only explicit labels, not domain nouns or arbitrary occupations.

    Administración/Enfermería can name knowledge domains. Diseño de cursos is a
    competence; Curso de Excel names a course, and Doctorado en ... a degree.
    Absence of a hit is not proof that every possible job title is a skill.
    """
    if not isinstance(name, str):
        return None
    text = _key(name)
    if re.match(r'^cursos?(?:\s+|\s*:\s*)\S', text):
        return 'course'
    if re.match(r'^(?:taller|seminario)\s+(?:de|en|sobre)\s+\S', text):
        return 'course'
    degrees = r'(?:doctorado|licenciatura|maestria|master|diplomatura|diplomado|grado|posgrado|certificado|certificacion|titulo)'
    if re.fullmatch(degrees, text) or re.match(degrees + r'\s+(?:en|de|oficial|universitario|universitaria)\b', text):
        return 'degree'
    if re.match(r'^(?:doctor|doctora|tecnico|tecnica|licenciado|licenciada|ingeniero|ingeniera)\s+en\s+\S', text):
        return 'degree'
    role = (r'(?:auxiliar|asistente|operario|operaria|empleado|empleada|vendedor|vendedora|'
            r'gerente|supervisor|supervisora|coordinador|coordinadora|director|directora)')
    if re.fullmatch(role, text) or re.match(role + r'\s+(?:de|del|en|administrativo|administrativa)\b', text):
        return 'role'
    if text in {'enfermero', 'enfermera', 'docente', 'profesor', 'profesora'}:
        return 'role'
    return None


def _union_exact(first: list[str], second: list[str]) -> list[str]:
    values = list(dict.fromkeys([*first, *second]))
    if len(values) > 8:
        raise SkillTaxonomyError('evidence_limit')
    return values


def _training_only_quote(quote: str) -> bool:
    text = _key(quote).strip(' .;!?:')
    if not re.match(r'^cursos?(?:\s+|\s*:\s*)\S', text):
        return False
    # A heading alone is not proof of competence. Once a quote contains a
    # second clause, let the model's semantic judgment and source validation
    # decide: a finite verb whitelist silently discarded valid practice such
    # as "desarrollé" and "implementé" within training descriptions.
    if re.search(r'[.!?;\n]', quote.strip().rstrip(' .;!?:')):
        return False
    if re.search(r'\b(?:donde|en (?:el|la) (?:que|cual)|durante el cual|in which|where)\b', text):
        return False
    return not re.search(r'\b(?:use|utilice|practique|prepare|aplique|realice|elabore|desarrolle|implemente|aprendi|construi|cree|program[eé]|worked|built|developed|implemented|learned|used)\b', text)


def normalize_expanded_skills(skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonicalize exact soft aliases and safely merge duplicate concepts.

    Apply after source/fragment validation, then revalidate the rebuilt compact
    fragment/profile. This function preserves literal evidence; it cannot verify
    source membership without the source text. Non-soft technical names remain
    unchanged. Known names override unknown/technical kind; a concept/name
    disagreement, unsupported soft skill or unsafe merge raises for retry.
    """
    if not isinstance(skills, list) or len(skills) > 100:
        raise SkillTaxonomyError('invalid_skills')
    result, positions = [], {}
    for supplied in skills:
        if not isinstance(supplied, dict):
            raise SkillTaxonomyError('invalid_skill')
        try:
            validated = Skill.model_validate(supplied)
        except (ValidationError, TypeError):
            raise SkillTaxonomyError('invalid_skill') from None
        row = deepcopy(supplied)
        name = validated.name
        if non_competency_kind(name):
            raise SkillTaxonomyError('non_competency_name')
        if validated.origin == 'user_declared':
            raise SkillTaxonomyError('invalid_origin')
        if (not validated.evidence or not validated.source_quotes
                or any(not value.strip() or len(value) > 160
                       for value in [*validated.evidence, *validated.source_quotes])):
            raise SkillTaxonomyError('invalid_evidence')
        if all(_training_only_quote(quote) for quote in validated.source_quotes):
            raise SkillTaxonomyError('training_only_evidence')
        target = _BY_NAME.get(_key(name))
        concept = _key(validated.concept_id) if validated.concept_id else None
        known_concept = _CONCEPTS.get(concept)
        if target is None:
            if known_concept:
                raise SkillTaxonomyError('concept_name_conflict')
            if (validated.kind == 'soft' or _key(name) in _OUTSIDE_SOFT_NAMES
                    or (concept and concept.startswith('soft.'))):
                raise SkillTaxonomyError('unsupported_soft_skill')
            result.append(row)
            continue
        canonical_name, canonical_concept = target
        if concept and known_concept != canonical_concept:
            raise SkillTaxonomyError('concept_name_conflict')
        row.update(name=canonical_name, kind='soft', concept_id=canonical_concept)
        if canonical_concept not in positions:
            positions[canonical_concept] = len(result)
            result.append(row)
            continue
        previous = result[positions[canonical_concept]]
        for field, unknown in (('level', 'unknown'), ('years_experience', None)):
            old, new = previous.get(field, unknown), row.get(field, unknown)
            if old not in (unknown, None) and new not in (unknown, None) and old != new:
                raise SkillTaxonomyError('incompatible_metadata')
            if old in (unknown, None) and new not in (unknown, None):
                previous[field] = new
        previous['origin'] = ('declared' if 'declared' in
                              (previous.get('origin', 'declared'), row.get('origin', 'declared')) else 'inferred')
        previous['evidence'] = _union_exact(previous.get('evidence', []), row.get('evidence', []))
        previous['source_quotes'] = _union_exact(previous.get('source_quotes', []), row.get('source_quotes', []))
    return result
