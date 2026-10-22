"""Keep academic completion grounded in literal source statements, not inference."""

from __future__ import annotations

import copy
import re
import unicodedata


def _normalize(text: str) -> str:
    return ' '.join(''.join(char for char in unicodedata.normalize('NFKD', text.casefold())
                           if not unicodedata.combining(char)).split())


_INCOMPLETE = re.compile(
    r'\b(?:incomplet\w*|abandonad\w*|suspendid\w*|unfinished|uncompleted)\b|'
    r'\b(?:sin (?:terminar|finalizar|titulo)|no (?:finaliz\w*|complet\w*|graduad\w*|obtenid\w*)|'
    r'tesis pendiente|pendiente (?:de )?(?:tesis|defensa|graduacion|titulacion)|'
    r'cursada (?:finalizada|completa)|(?:coursework|asignaturas) (?:completed|completadas|aprobadas)|'
    r'all but dissertation|degree pending|not (?:completed|graduated))\b'
)
_ONGOING = re.compile(
    r'\b(?:en curso|cursando|doctorand[oa]|estudiante|in[- ]progress|pursuing|'
    r'currently enrolled|doctoral candidate|ph\.?d\.? (?:student|candidate)|actualidad|ongoing|'
    r'expected(?: graduation)?|graduacion (?:prevista|estimada))\b'
)
_GRADUATED = re.compile(
    r'\b(?:graduad[oa]|recibid[oa]|titulad[oa]|egresad[oa]|graduated|'
    r'finalizad[oa]|completad[oa]|concluid[oa]|completed|degree awarded|degree obtained|'
    r'titulo obtenido|se doctoro|obtuvo (?:el |su )?(?:titulo|doctorado))\b|'
    r'\b(?:doctor[ae]?|licenciad[oa]|ingenier[oa]|magister) en\b'
)
_LEVEL_ANCHORS = {
    'doctorate': r'\b(?:doctorado|doctor[ae]?|doctorand[oa]|doctoral|doctorate|ph\.?d\.?)\b',
    'master': r'\b(?:maestria|master|magister|msc)\b',
    'bachelor': r'\b(?:licenciatura|licenciad[oa]|ingenieria|ingenier[oa]|grado|bachelor|bsc)\b',
    'secondary': r'\b(?:secundari[oa]|bachiller|secondary|high school)\b',
    'technical': r'\b(?:tecnic\w*|technical|vocacional)\b',
}
_DEGREE_CLAIMS = {
    'doctorate': re.compile(r'\bdoctor[ae]? en\b|\bph\.?d\.?(?!\w|\s+(?:student|candidate))(?: in)?\b|\bcon (?:un )?doctorado(?: en)?\b'),
    'master': re.compile(r'\bmagister en\b|\bcon (?:una? )?(?:maestria|master)\b'),
    'bachelor': re.compile(r'\b(?:licenciad[oa]|ingenier[oa]) en\b'),
}
_PROGRAM_START = re.compile(r'\b(?:doctorado|doctorate|phd|maestria|master|licenciatura|ingenieria|'
                            r'tecnicatura|secundario|(?<!en )curso|certificado|certificacion|licencia|certification)\b')
_STOP_WORDS = {'en', 'de', 'del', 'la', 'el', 'los', 'las', 'of', 'in', 'and', 'y',
               'curso', 'certificado', 'certificacion', 'licencia', 'doctorado', 'maestria', 'master',
               'licenciatura', 'ingenieria', 'tecnicatura', 'degree', 'certificate'}


def _tokens(value: str) -> set[str]:
    return set(re.findall(r'[a-z0-9]+', _normalize(value))) - _STOP_WORDS


def _contexts(quotes: list[str]) -> list[str]:
    """Separate adjacent programs even when a provider quote spans several."""
    result = []
    for quote in quotes:
        text = _normalize(quote.replace('\n', '; ')).replace('ph.d.', 'phd')
        for sentence in re.split(r'\.(?:\s+|$)|;', text):
            starts = list(_PROGRAM_START.finditer(sentence))
            boundaries = [0, *(match.start() for match in starts[1:]), len(sentence)]
            result.extend(sentence[left:right].strip() for left, right in zip(boundaries, boundaries[1:]) if sentence[left:right].strip())
    return result


def _study_context(row: dict, quotes: list[str]) -> list[str]:
    anchor = _LEVEL_ANCHORS.get(row.get('level'))
    field = _tokens(row.get('field') or '')
    contexts = _contexts(quotes)
    if field:
        contexts = [context for context in contexts if field <= _tokens(context)]
    if anchor:
        contexts = [context for context in contexts if re.search(anchor, context)]
    if not contexts:
        raise ValueError('La cita debe identificar el programa y su estado sin mezclar otros estudios.')
    return contexts


def _completion_status(evidence: str) -> str:
    if _ONGOING.search(evidence) and not re.search(r'\b(?:abandonad\w*|suspendid\w*|unfinished)\b', evidence):
        return 'in_progress'
    if _INCOMPLETE.search(evidence):
        return 'incomplete'
    if _GRADUATED.search(evidence):
        return 'completed'
    return 'unknown'


def ground_education(education: list[dict], source_text: str) -> list[dict]:
    """Every extracted study needs its own quote; unspecified completion stays unknown."""
    source = ' '.join(source_text.split()).casefold()
    rows = copy.deepcopy(education)
    for row in rows:
        quotes = row.get('source_quotes', [])
        if not quotes or any(not quote.strip() or len(quote) > 160 or ' '.join(quote.split()).casefold() not in source for quote in quotes):
            raise ValueError('Cada estudio requiere citas literales del programa y su estado en el CV.')
        statuses = {_completion_status(context) for context in _study_context(row, quotes)}
        # Ambiguous/contradictory mentions cannot grant a degree.
        status = statuses.pop() if len(statuses) == 1 else 'unknown'
        row['completion_status'] = status
        row['completed'] = True if status == 'completed' else False if status in {'in_progress', 'incomplete'} else None
    return rows


def validate_credential_completion(credentials: list[dict]) -> None:
    """A course/qualification still being pursued is not an awarded credential."""
    for credential in credentials:
        name = _tokens(credential.get('name') or '')
        contexts = [context for context in _contexts(credential.get('source_quotes', [])) if name <= _tokens(context)]
        if not contexts:
            raise ValueError('La cita de una credencial debe identificar su nombre y estado propios.')
        if any(_ONGOING.search(context) or _INCOMPLETE.search(context) for context in contexts):
            raise ValueError('Una formación pendiente debe conservarse como estudio, no como credencial obtenida.')


def safe_academic_narrative(profile: dict) -> dict:
    """Drop unsupported degree claims from generated headline/summary, retaining facts."""
    result = copy.deepcopy(profile)
    education = result.get('education', [])
    for key in ('headline', 'summary'):
        text = _normalize(result.get(key) or '')
        for level, pattern in _DEGREE_CLAIMS.items():
            for claim in pattern.finditer(text):
                field_text = re.split(r'[.,;]|\b(?:y|con|que|ademas|trabaja|cursa|actualmente)\b', text[claim.end():], maxsplit=1)[0]
                claim_tokens = _tokens(field_text)
                completed = any(row.get('level') == level and row.get('completed') is True
                    and _tokens(row.get('field') or '')
                    and _tokens(row['field']) <= claim_tokens for row in education)
                if not completed:
                    result[key] = None
                    break
            if result.get(key) is None:
                break
    return result
