"""Small, nonrecursive JSON Schema subset for CV constrained decoding only.

Provider grammar is a shape aid; local Pydantic/evidence/coverage validation
remains authoritative. Bounds and metadata are omitted to reduce decoder states.
See https://ai.google.dev/gemini-api/docs/structured-output#json-schema-support
"""

from __future__ import annotations

import json
from typing import Any


_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


def flatten_cv_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve local Pydantic refs and nullable unions; reject unsupported shapes."""
    def invalid():
        raise ValueError("El esquema estructurado del CV no tiene una forma compatible.")

    if not isinstance(schema, dict):
        invalid()
    definitions = schema.get("$defs", {})
    if not isinstance(definitions, dict):
        invalid()

    def visit(node, seen=(), depth=0):
        if not isinstance(node, dict) or depth > 20:
            invalid()
        if "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/$defs/") or ref in seen:
                invalid()
            target = definitions.get(ref[len("#/$defs/"):])
            if not isinstance(target, dict):
                invalid()
            return visit({**target, **{key: value for key, value in node.items() if key != "$ref"}},
                         (*seen, ref), depth + 1)
        if "anyOf" in node:
            branches = node["anyOf"]
            if not isinstance(branches, list) or len(branches) != 2:
                invalid()
            nonnull = [branch for branch in branches if isinstance(branch, dict) and branch.get("type") != "null"]
            if len(nonnull) != 1 or not any(branch == {"type": "null"} for branch in branches):
                invalid()
            result = visit(nonnull[0], seen, depth + 1)
            result["type"] = [result["type"], "null"]
            if "enum" in result:
                result["enum"] = [*result["enum"], None]
            return result
        kind = node.get("type")
        if not isinstance(kind, str) or kind not in _TYPES:
            invalid()
        result = {"type": kind}
        if kind == "object":
            properties = node.get("properties")
            if not isinstance(properties, dict) or any(not isinstance(key, str) for key in properties):
                invalid()
            result["properties"] = {key: visit(value, seen, depth + 1) for key, value in properties.items()}
            required = node.get("required", [])
            if not isinstance(required, list) or any(not isinstance(key, str) or key not in properties for key in required):
                invalid()
            if required:
                result["required"] = list(dict.fromkeys(required))
            if node.get("additionalProperties") is False:
                result["additionalProperties"] = False
        elif kind == "array":
            result["items"] = visit(node.get("items"), seen, depth + 1)
        if "enum" in node:
            values = node["enum"]
            if not isinstance(values, list) or not values or any(type(value) not in (str, int, float) for value in values):
                invalid()
            result["enum"] = list(values)
        return result

    result = visit(schema)
    if result["type"] != "object":
        invalid()
    return result


def cv_response_schema(user_payload: str) -> dict[str, Any] | None:
    """Select only packet/consolidation envelopes; other LLM flows stay unchanged."""
    try:
        payload = json.loads(user_payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    fragment = "fragment_schema" in payload and isinstance(payload.get("text"), str)
    plan = ("plan_schema" in payload and isinstance(payload.get("facts"), dict)
            and payload.get("candidate_profile_version") == "candidate-profile.v2")
    if fragment == plan:  # Neither envelope, or ambiguous: do not affect unrelated tasks.
        return None
    result = flatten_cv_schema(payload["fragment_schema" if fragment else "plan_schema"])
    if fragment:
        # Local wire defaults allow compact omission, but provider output must
        # explicitly grade each extracted skill and attach its evidence.
        required_skill = ["n", "k", "l", "o", "e", "q"]
        try:
            skill = result["properties"]["sk"]["items"]
            if skill["type"] != "object" or any(key not in skill["properties"] for key in required_skill):
                raise KeyError
        except (KeyError, TypeError):
            raise ValueError("El esquema estructurado del CV no tiene una forma compatible.") from None
        result["required"] = list(dict.fromkeys([*result.get("required", []), "sk", "pr"]))
        skill["required"] = list(dict.fromkeys([*skill.get("required", []), *required_skill]))
        education = result['properties']['ed']['items']
        education['required'] = list(dict.fromkeys([*education.get('required', []), 'q']))
        project = result['properties']['pr']['items']
        project['required'] = list(dict.fromkeys([*project.get('required', []), 'n', 'q']))
    else:
        result['required'] = list(dict.fromkeys([*result.get('required', []), 'pr']))
    return result
