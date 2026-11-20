"""Compare local CV request budgets without an LLM, Redis or private documents.

Run from backend: python -B scripts/benchmark_cv_budget.py
Numbers estimate input only; they do not measure billed tokens or extraction quality.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.cv_compact_contract import fragment_schema
from app.services.cv_packets import (
    CV_EFFICIENT_PROMPT_VERSION, CV_PREVIOUS_PROMPT_VERSION, _quote_catalog,
    _split_text_exact, make_packet_identity, new_manifest,
)
from app.services.cv_request_budget import prepare_gemini_input
from app.services.gemini_budget import estimate_input_tokens
from app.services.prompt_loader import load_prompt
from app.services.skill_catalog_store import SkillCatalogStore
from app.services.skill_context import build_cv_skill_context, seed_concepts


def synthetic_text(size: int) -> str:
    return ''.join(f'Proyecto {i:04d}: Desarrollo de aplicaciones en Python y Django. '
                   'Implementación de APIs, pruebas, SQL y documentación para clientes.\n'
                   for i in range(1000))[:size]


async def simulate(text: str, *, efficient: bool, limit: int) -> dict:
    identity = make_packet_identity(
        job_id='simulation', analysis_id=1, candidate_id=1, cv_version_id=1,
        source_hash='a' * 64, text=text,
        prompt_version=CV_EFFICIENT_PROMPT_VERSION if efficient else CV_PREVIOUS_PROMPT_VERSION,
        provider='gemini', model='simulation', packet_chars=20000, block_chars=20000, budget=16384,
    )
    manifest = new_manifest(text, identity)
    admissions = []
    prompt_file = ('cv_extraction.v2.efficient.system.md' if efficient
                   else 'cv_extraction.v2.packets-soft.v8.system.md')
    catalog = SkillCatalogStore(concepts=seed_concepts())  # Explicitly local; no environment DSN.

    async def visit(chunk: str) -> None:
        context = await build_cv_skill_context(chunk, store=catalog, compact=efficient)
        system = load_prompt(prompt_file) + '\n\n' + context + (
            '\nTarea: extraé hechos solo de este paquete y respondé con aliases compactos, sin identidad ni habilidades rechazadas.')
        payload = json.dumps({
            'packet_id': 'p-' + 'a' * 32, 'packet_index': 1, 'text': chunk,
            'source_quotes': _quote_catalog(chunk), 'fragment_schema': fragment_schema(),
            **({'source_format': 'annotated-quotes.v1'} if efficient else {}),
        }, ensure_ascii=False)
        estimate = (prepare_gemini_input(system, payload)[2] if efficient
                    else estimate_input_tokens(system + payload))
        if estimate > limit:
            left, right = _split_text_exact(chunk)
            await visit(left)
            await visit(right)
        else:
            admissions.append(estimate)

    for root_id in manifest['roots']:
        node = manifest['packets'][root_id]
        await visit(text[node['start']:node['end']])
    return {'extraction_requests': len(admissions), 'estimated_input_tokens': sum(admissions),
            'consolidation_requests': 0 if efficient or len(admissions) == 1 else 1}


async def main(sizes: list[int], limit: int) -> None:
    for size in sizes:
        text = synthetic_text(size)
        before = await simulate(text, efficient=False, limit=limit)
        after = await simulate(text, efficient=True, limit=limit)
        print(json.dumps({'characters': len(text), 'before': before, 'after': after,
                          'estimated_input_reduction_percent': round(
                              100 * (1 - after['estimated_input_tokens'] / before['estimated_input_tokens']), 1),
                          'note': 'Synthetic input estimates; excludes retries, output and baseline consolidation tokens.'}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sizes', type=int, nargs='+', default=[20000, 60000, 100000])
    parser.add_argument('--limit', type=int, default=20000)
    args = parser.parse_args()
    if any(size < 1 or size > 100000 for size in args.sizes) or args.limit < 20000:
        parser.error('Use sizes 1..100000 and a limit of at least 20000.')
    asyncio.run(main(args.sizes, args.limit))
