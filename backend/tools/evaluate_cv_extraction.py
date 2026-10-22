"""Opt-in live evaluation with synthetic CVs and the production quota limiter.

Run from backend: .venv/Scripts/python tools/evaluate_cv_extraction.py
    --prompt v7 --output <path.json> [--cases case_id ...] [--repeat 1]
Never loads user CVs; no secrets or provider error bodies are written to reports.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
import unicodedata
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import settings
from app.services import cv_pipeline, gemini_client
from app.services.cv_skill_taxonomy import SOFT_SKILL_TAXONOMY
from app.services.prompt_loader import load_prompt


def key(value):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', value)
                           if not unicodedata.combining(c)).casefold().split())


def score(case, profile):
    skills = profile.get('skills', [])
    names = {key(row['name']) for row in skills}
    missing = [aliases[0] for aliases in case['required_skills']
               if not any(key(alias) in names for alias in aliases)]
    forbidden = [name for name in case['forbidden_skills'] if key(name) in names]
    invalid_soft = [row['name'] for row in skills
                    if row['kind'] == 'soft' and row['name'] not in SOFT_SKILL_TAXONOMY]
    education_errors = [expected for expected in case['expected_education'] if not any(
        key(expected['field_contains']) in key(row.get('field') or '')
        and row.get('completion_status') == expected['status']
        for row in profile.get('education', []))]
    return {'missing_required': missing, 'forbidden_found': forbidden,
            'invalid_soft': invalid_soft, 'education_errors': education_errors,
            'required_count': len(case['required_skills']),
            'passed': not (missing or forbidden or invalid_soft or education_errors)}


async def run(args):
    if settings.LLM_PROVIDER != 'gemini':
        raise SystemExit('This evaluation requires the configured Gemini provider.')
    cases = json.loads((ROOT / 'tests/fixtures/cv_extraction_quality.json').read_text(encoding='utf-8'))
    if args.cases:
        requested = set(args.cases)
        if requested - {case['id'] for case in cases}:
            raise SystemExit('Unknown case ID.')
        cases = [case for case in cases if case['id'] in requested]
    report = {'prompt': args.prompt, 'model': settings.GEMINI_MODEL, 'cases': [],
              'note': 'Synthetic coverage checks, not population accuracy or full precision.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    original_generate = gemini_client.generate_json
    for repetition in range(args.repeat):
        for case in cases:
            usage, calls, attempts = [], [], []

            def measured(system, payload, marker, max_output_tokens=None):
                attempts.append(marker.rsplit('-', 1)[-1])
                start = time.perf_counter()
                result = original_generate(system, payload, marker, max_output_tokens)
                usage.append(result.get('usage', {}))
                calls.append(round(time.perf_counter() - start, 3))
                return result

            def selected_prompt(name):
                if name.startswith('cv_extraction.v2.packets-soft.'):
                    name = f'cv_extraction.v2.packets-soft.{args.prompt}.system.md'
                return load_prompt(name)

            start = time.perf_counter()
            row = {'id': case['id'], 'repetition': repetition + 1}
            try:
                with patch('app.services.cv_packets.load_prompt', side_effect=selected_prompt), \
                     patch.object(gemini_client, 'generate_json', side_effect=measured), \
                     patch.object(cv_pipeline, 'CV_EXTRACTION_PROMPT_VERSION', f'cv-extraction.v2-packets-soft.{args.prompt}'):
                    profile = await asyncio.wait_for(cv_pipeline.process_cv_text(case['text'], 987654), 240)
                row.update(score(case, profile), profile=profile)
            except Exception as exc:
                row.update(passed=False, error_type=type(exc).__name__,
                           error_code=getattr(exc, 'code', None))
            row.update(seconds=round(time.perf_counter() - start, 3), provider_attempts=len(attempts),
                       completed_calls=len(calls), call_seconds=calls, usage=usage)
            report['cases'].append(row)
            report['passed'] = sum(item['passed'] for item in report['cases'])
            report['total'] = len(report['cases'])
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({k: v for k, v in row.items() if k not in {'profile', 'usage'}}, ensure_ascii=False), flush=True)
            if row.get('error_type') in {'RateLimitExceeded', 'GeminiUnavailable'}:
                print('Stopped to respect provider availability and quota.', flush=True)
                return 2
    print(f"Passed {report['passed']}/{report['total']}; report: {args.output}", flush=True)
    return 0 if report['passed'] == report['total'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prompt', choices=['v7', 'v8'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--repeat', type=int, default=1, choices=range(1, 4))
    raise SystemExit(asyncio.run(run(parser.parse_args())))
