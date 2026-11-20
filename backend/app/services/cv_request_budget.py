"""Compact Gemini CV transport; source and local validation remain unchanged."""

from __future__ import annotations

import json
import re

from app.services.gemini_budget import estimate_input_tokens
from app.services.gemini_cv_schema import cv_response_schema


def annotated_source(text: str, catalog: dict[str, str]) -> str:
    """Mark non-overlapping literal quotes without dropping any source character.

    Prefer a wrapped study quote over its shorter first line. Unmarked gaps and
    repeated text remain verbatim. The full catalog stays on the server.
    """
    candidates = []
    for quote_id, quote in catalog.items():
        if not isinstance(quote_id, str) or not isinstance(quote, str) or not quote:
            continue
        start = text.find(quote)
        if start >= 0:
            candidates.append((start, -len(quote), quote_id, quote))
    cursor = 0
    parts = []
    for start, _length, quote_id, quote in sorted(candidates):
        if start < cursor:
            continue
        display_id = 'q' + quote_id[6:] if re.fullmatch(r'quote_\d+', quote_id) else quote_id
        parts.extend((text[cursor:start], f'[{display_id}]', quote, f'[/{display_id}]'))
        cursor = start + len(quote)
    parts.append(text[cursor:])
    return ''.join(parts)


def prepare_gemini_input(system: str, payload: str) -> tuple[str, dict | None, int]:
    """Estimate the actual transport, including the native decoder schema once."""
    schema = cv_response_schema(payload)
    if schema is None:
        return payload, None, estimate_input_tokens(system + payload)
    data = json.loads(payload)
    data.pop('fragment_schema', None)
    data.pop('plan_schema', None)
    if (data.get('source_format') == 'annotated-quotes.v1'
            and isinstance(data.get('text'), str) and isinstance(data.get('source_quotes'), dict)):
        data['source_text'] = annotated_source(data.pop('text'), data.pop('source_quotes'))
        data.pop('source_format')
    content = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    schema_text = json.dumps(schema, ensure_ascii=False, separators=(',', ':'))
    return content, schema, estimate_input_tokens(system + content + schema_text)
