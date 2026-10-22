"""Lossless, server-side assembly of CV facts from a compact merge plan."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.services.cv_compact_contract import WireFragment


class _PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ConsolidationPlan(_PlanModel):
    headline: str | None = Field(default=None, max_length=180, alias="h")
    summary: str | None = Field(default=None, max_length=500, alias="s")
    skills: list[list[str]] = Field(alias="sk", min_length=0, max_length=100)
    work_experience: list[list[str]] = Field(alias="x", min_length=0, max_length=50)
    education: list[list[str]] = Field(alias="ed", min_length=0, max_length=20)
    languages: list[list[str]] = Field(alias="l", min_length=0, max_length=20)
    credentials: list[list[str]] = Field(alias="c", min_length=0, max_length=50)


SECTIONS = {
    "sk": ("skills", ("n", "k", "l", "o", "y", "ci")),
    "x": ("work_experience", ("t", "o", "m")),
    "ed": ("education", ("l", "f", "i", "c", "st", "q")),
    "l": ("languages", ("n", "l")),
    "c": ("credentials", ("n", "i", "d", "x", "o")),
}


class IncompatibleFactGroup(ValueError):
    """A proposed merge is unsafe, but its individual source facts remain valid."""


def validate_and_build_profile(
    raw_plan: dict[str, Any],
    fragments: list[dict[str, Any]],
    candidate_id: int,
) -> dict[str, Any]:
    profile, _effective_plan = validate_and_build_profile_with_effective_plan(
        raw_plan, fragments, candidate_id,
    )
    return profile


def validate_and_build_profile_with_effective_plan(
    raw_plan: dict[str, Any],
    fragments: list[dict[str, Any]],
    candidate_id: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Check exhaustive, section-correct references and merge facts without rewriting them."""
    try:
        plan = ConsolidationPlan.model_validate(raw_plan)
    except ValidationError as exc:
        raise ValueError("El plan de consolidación del CV es inválido o está incompleto.") from exc

    facts, preferences = _collect_facts(fragments)
    plan_data = plan.model_dump(by_alias=True)
    for alias, (section_name, _input_fields) in SECTIONS.items():
        _validate_exact_coverage(plan_data, alias, facts[section_name])
    # Coverage is a hard boundary: only after it succeeds may an unsafe
    # semantic grouping be repaired into lossless singletons.
    effective_plan = dict(plan_data)
    for alias, (section_name, _input_fields) in SECTIONS.items():
        effective_plan[alias] = _repair_groups(plan_data[alias], facts[section_name], section_name)
    effective_plan_model = ConsolidationPlan.model_validate(effective_plan)
    effective_plan_data = effective_plan_model.model_dump(by_alias=True)

    profile: dict[str, Any] = {
        "schema_version": "candidate-profile.v2",
        "profile_id": str(candidate_id),
        "headline": plan.headline,
        "summary": plan.summary,
        "skills": _merge_groups(effective_plan_model.skills, facts["skills"], "skills"),
        "work_experience": _merge_groups(effective_plan_model.work_experience, facts["work_experience"], "work_experience"),
        "education": _merge_groups(effective_plan_model.education, facts["education"], "education"),
        "languages": _merge_groups(effective_plan_model.languages, facts["languages"], "languages"),
        "credentials": _merge_groups(effective_plan_model.credentials, facts["credentials"], "credentials"),
        "preferences": preferences,
        "rejected_skills": [],
    }
    return profile, effective_plan_data


def _repair_groups(groups: list[list[str]], facts: dict[str, dict], section: str) -> list[list[str]]:
    repaired: list[list[str]] = []
    positions = {item_id: index for index, item_id in enumerate(facts)}
    for group in groups:
        rows = [facts[item_id] for item_id in sorted(group, key=positions.__getitem__)]
        try:
            _merge_one(rows, section)
        except IncompatibleFactGroup:
            repaired.extend([[item_id] for item_id in sorted(group, key=positions.__getitem__)])
        else:
            repaired.append(list(group))
    return repaired


def compact_facts_for_plan(fragments: list[dict[str, Any]]) -> dict[str, Any]:
    """Build plan input with identifiers and facts, omitting repeated evidence and quotes."""
    facts, preferences = _collect_facts(fragments)
    del preferences  # Preferences are combined deterministically by the server.
    payload: dict[str, Any] = {}
    for alias, (section_name, fields) in SECTIONS.items():
        payload[alias] = [
            {"id": item_id, **{key: row[key] for key in fields if key in row}}
            for item_id, row in facts[section_name].items()
        ]
    return payload


def _collect_facts(fragments: list[dict[str, Any]]) -> tuple[dict[str, dict[str, dict]], dict[str, list]]:
    facts: dict[str, dict[str, dict]] = {
        "skills": {}, "work_experience": {}, "education": {}, "languages": {}, "credentials": {},
    }
    preferences: dict[str, list] = {"locations": [], "work_modes": []}
    counters = {alias: 0 for alias in SECTIONS}
    for fragment_index, raw_fragment in enumerate(fragments):
        try:
            fragment = WireFragment.model_validate(raw_fragment)
        except ValidationError as exc:
            raise ValueError("Un fragmento validado del CV dejó de cumplir el esquema compacto.") from exc
        data = fragment.model_dump(mode="json", by_alias=True, exclude_defaults=True)
        for alias, (section_name, _fields) in SECTIONS.items():
            for row in data.get(alias, []):
                item_id = f"{alias}{counters[alias]}"
                counters[alias] += 1
                facts[section_name][item_id] = row
        prefs = data.get("p", {})
        preferences["locations"] = _ordered_union(preferences["locations"], prefs.get("l", []))
        preferences["work_modes"] = _ordered_union(preferences["work_modes"], prefs.get("w", []))
    if len(preferences["locations"]) > 10 or len(preferences["work_modes"]) > 3:
        raise ValueError("Las preferencias del CV superan los límites permitidos; no se descartaron datos.")
    return facts, preferences


def _validate_exact_coverage(plan_data: dict, alias: str, facts: dict[str, dict]) -> None:
    groups = plan_data.get(alias)
    if not isinstance(groups, list):
        raise ValueError("El plan de consolidación omitió una sección obligatoria.")
    expected = set(facts)
    seen: list[str] = []
    for group in groups:
        if not isinstance(group, list) or not group:
            raise ValueError("El plan contiene un grupo vacío o inválido.")
        if len(group) != len(set(group)):
            raise ValueError("El plan repite un identificador dentro de un grupo.")
        for item_id in group:
            if item_id not in expected:
                raise ValueError("El plan contiene un identificador inexistente o de otra sección.")
            seen.append(item_id)
    if len(seen) != len(set(seen)):
        raise ValueError("El plan asigna un hecho a más de un grupo.")
    if set(seen) != expected:
        raise ValueError("El plan debe conservar todos los hechos de cada sección exactamente una vez.")


def _merge_groups(groups: list[list[str]], facts: dict[str, dict], section: str) -> list[dict]:
    merged: list[dict] = []
    positions = {item_id: index for index, item_id in enumerate(facts)}
    ordered_groups = sorted(groups, key=lambda group: min(positions[item_id] for item_id in group))
    for ids in ordered_groups:
        rows = [facts[item_id] for item_id in sorted(ids, key=positions.__getitem__)]
        merged.append(_merge_one(rows, section))
    return merged


def _merge_one(rows: list[dict], section: str) -> dict:
    if section == "skills":
        return _merge_skill(rows)
    if section == "work_experience":
        return _merge_experience(rows)
    if section == "education":
        return _merge_education(rows)
    if section == "languages":
        return _merge_language(rows)
    return _merge_credential(rows)


def _merge_skill(rows: list[dict]) -> dict:
    first = rows[0]
    names = {_normalize(row.get("n")) for row in rows}
    concepts = {_normalize(row.get("ci")) for row in rows if row.get("ci") is not None}
    if len(concepts) > 1:
        raise IncompatibleFactGroup("El plan agrupó habilidades con identificadores de concepto distintos.")
    if len(names) > 1 and (len(concepts) != 1 or any(row.get("ci") is None for row in rows)):
        raise IncompatibleFactGroup("El plan agrupó habilidades distintas sin un concepto compartido verificable.")
    return {
        "name": first["n"],
        "kind": _one_known(rows, "k", {"unknown"}) or "unknown",
        "level": _one_known(rows, "l", {"unknown"}) or "unknown",
        "origin": "declared" if any(row.get("o", "declared") == "declared" for row in rows) else "inferred",
        "years_experience": _one_known(rows, "y", {None}),
        "evidence": _union_text_field(rows, "e"),
        "source_quotes": _union_text_field(rows, "q"),
        "concept_id": _one_known(rows, "ci", {None}),
    }


def _merge_experience(rows: list[dict]) -> dict:
    if len({_normalize(row.get("t")) for row in rows}) > 1:
        raise IncompatibleFactGroup("El plan agrupó puestos con títulos distintos.")
    _reject_conflicts(rows, "o", "work experience organization", incompatible=True)
    return {
        "job_title": rows[0]["t"],
        "organization": _one_known(rows, "o", {None}),
        "duration_months": _one_known(rows, "m", {None}),
        "highlights": _union_text_field(rows, "h"),
    }


def _merge_education(rows: list[dict]) -> dict:
    for key, label in (("l", "level"), ("f", "field"), ("i", "institution")):
        _reject_conflicts(rows, key, f"education {label}", unknown_values={None, "unknown"}, incompatible=True)
    _reject_conflicts(rows, 'c', 'education completion', incompatible=True)
    _reject_conflicts(rows, 'st', 'education completion status', unknown_values={None, 'unknown'}, incompatible=True)
    return {
        "level": _one_known(rows, "l", {"unknown"}) or "unknown",
        "field": _one_known(rows, "f", {None}),
        "institution": _one_known(rows, "i", {None}),
        "completed": _one_known(rows, "c", {None}),
        "completion_status": _one_known(rows, "st", {None, 'unknown'}) or 'unknown',
        "source_quotes": _union_text_field(rows, 'q'),
    }


def _merge_language(rows: list[dict]) -> dict:
    if len({_normalize(row["n"]) for row in rows}) != 1:
        raise IncompatibleFactGroup("El plan intentó combinar idiomas con nombres distintos.")
    return {
        "name": rows[0]["n"],
        "level": _one_known(rows, "l", {"unknown"}) or "unknown",
    }


def _merge_credential(rows: list[dict]) -> dict:
    for key, label in (("n", "name"), ("i", "issuer"), ("d", "issued date"), ("x", "expiration date")):
        _reject_conflicts(rows, key, f"credential {label}", incompatible=True)
    return {
        "name": _one_known(rows, "n", {None}),
        "issuer": _one_known(rows, "i", {None}),
        "issued_at": _one_known(rows, "d", {None}),
        "expires_at": _one_known(rows, "x", {None}),
        "evidence": _union_text_field(rows, "e"),
        "source_quotes": _union_text_field(rows, "q"),
        "origin": "declared" if any(row.get("o", "declared") == "declared" for row in rows) else "user_declared",
    }


def _union_text_field(rows: list[dict], key: str) -> list[str]:
    values: list[str] = []
    for row in rows:
        values = _ordered_union(values, row.get(key, []))
    if len(values) > 8:
        raise IncompatibleFactGroup("La unión de hechos supera el límite de evidencia del contrato; no se descartaron citas.")
    return values


def _ordered_union(existing: list, additions: list) -> list:
    result = list(existing)
    seen = {_normalize(item) for item in result}
    for item in additions:
        key = _normalize(item)
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result


def _one_known(rows: list[dict], key: str, unknown_values: set) -> Any:
    known: list[Any] = []
    seen: set[str] = set()
    for row in rows:
        value = row.get(key)
        if value is None or value in unknown_values:
            continue
        marker = _normalize(value)
        if marker not in seen:
            seen.add(marker)
            known.append(value)
    return known[0] if len(known) == 1 else None


def _reject_conflicts(
    rows: list[dict], key: str, label: str, unknown_values: set | None = None, *, incompatible: bool = False,
) -> None:
    unknown = {None} if unknown_values is None else unknown_values
    values = {_normalize(row.get(key)) for row in rows if row.get(key) not in unknown}
    if len(values) > 1:
        error_type = IncompatibleFactGroup if incompatible else ValueError
        raise error_type(f"El plan agrupó hechos con {label} contradictorios.")


def _normalize(value: Any) -> str:
    if value is None:
        return "<none>"
    return re.sub(r"\s+", " ", str(value)).strip().casefold()
