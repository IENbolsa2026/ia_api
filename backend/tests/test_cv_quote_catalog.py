from __future__ import annotations

import copy

import pytest

from app.services.cv_compact_contract import validate_fragment
from app.services.cv_packets import _quote_catalog, _resolve_quote_ids


def test_quote_catalog_returns_literal_spans_with_unicode_and_long_sentences():
    long_sentence = "Árbol ñandú café " * 35
    source = f"Primera línea con Unicode.\n{long_sentence}\nÚltima oración."

    catalog = _quote_catalog(source)

    assert catalog
    assert all(0 < len(span) <= 160 for span in catalog.values())
    assert all(span in source for span in catalog.values())
    assert "Primera línea con Unicode." in catalog.values()
    assert "Última oración." in catalog.values()
    long_spans = [span for span in catalog.values() if "Árbol" in span]
    assert len(long_spans) > 1
    # Overlapping chunks preserve coverage and each chunk remains source-owned.
    assert all(span in long_sentence for span in long_spans)
    assert max(map(len, long_spans)) == 160
    covered = [False] * len(long_sentence)
    for span in long_spans:
        start = long_sentence.find(span)
        while start >= 0:
            for offset in range(start, start + len(span)):
                covered[offset] = True
            start = long_sentence.find(span, start + 1)
    assert all(covered)


def test_quote_catalog_keeps_wrapped_study_and_status_together():
    source = ('Lic. en Comunicación social\ncursado 2016-2018 UNNE\n'
              'Curso de análisis de datos -\nInnova finalizado 05/2026')
    values = set(_quote_catalog(source).values())
    assert 'Lic. en Comunicación social\ncursado 2016-2018 UNNE' in values
    assert 'Curso de análisis de datos -\nInnova finalizado 05/2026' in values


def test_resolve_quote_ids_expands_skills_and_credentials_without_mutating_input():
    catalog = {"quote_0": "Trabajé con Python.", "quote_1": "Curso de redes. Último módulo."}
    raw = {
        "sk": [{"n": "Python", "e": ["Desarrollo con Python"], "q": ["quote_0"]}],
        "c": [{"n": "Redes", "e": ["Curso declarado"], "q": "quote_1"}],
    }
    original = copy.deepcopy(raw)

    resolved = _resolve_quote_ids(raw, catalog)

    assert resolved["sk"][0]["q"] == [catalog["quote_0"]]
    assert resolved["c"][0]["q"] == [catalog["quote_1"]]
    assert raw == original
    assert resolved is not raw
    assert resolved["sk"][0] is not raw["sk"][0]


def test_unknown_quote_id_remains_invalid_under_literal_quote_validation():
    raw = {"sk": [{"n": "Python", "e": ["Experiencia con Python"], "q": ["quote_missing"]}]}
    resolved = _resolve_quote_ids(raw, {"quote_0": "Desarrollé con Python."})

    assert resolved["sk"][0]["q"] == ["quote_missing"]
    with pytest.raises(ValueError, match="no aparecen"):
        validate_fragment(resolved, "Desarrollé con Python.")


def test_literal_quotes_remain_backward_compatible_after_resolution():
    source = "Usé Python. Curso de redes."
    raw = {
        "sk": [{"n": "Python", "e": ["Experiencia declarada"], "q": ["Usé Python."]}],
        "c": [{"n": "Redes", "e": ["Curso mencionado"], "q": ["Curso de redes."]}],
    }

    resolved = _resolve_quote_ids(raw, {"quote_0": "Usé Python."})
    expanded, _compact = validate_fragment(resolved, source)

    assert expanded["skills"][0]["source_quotes"] == ["Usé Python."]
    assert expanded["credentials"][0]["source_quotes"] == ["Curso de redes."]
