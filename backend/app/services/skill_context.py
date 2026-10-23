"""Select compact, relevant skill guidance for one CV extraction request."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re
from functools import lru_cache

from app.services.skill_catalog_store import SkillCatalogHit, SkillCatalogStore, SkillConcept
from app.services.skill_embeddings import embed_queries


logger = logging.getLogger(__name__)
_SEED_PATH = Path(__file__).resolve().parents[1] / "data" / "skill_catalog.v1.json"
_SKILLS_PATH = Path(__file__).resolve().parents[1] / "prompts" / "skills"
_LINE_SPLIT = re.compile(r"[\r\n]+|(?<=[.;])\s+(?=[A-ZÁÉÍÓÚÑ])")
_DOMAIN_TERMS = {
    "technical": re.compile(r"\b(programaci[oó]n|desarrollo|software|c[oó]digo|api|backend|frontend|datos|base de datos|devops|python|javascript|java|sql)\b", re.I),
    "marketing": re.compile(r"\b(marketing|venta|cliente|comercial|campa[ñn]a|publicidad|redes sociales|mercado|crm|seo)\b", re.I),
}


@lru_cache(maxsize=1)
def seed_concepts() -> tuple[SkillConcept, ...]:
    rows = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
    return tuple(SkillConcept.from_mapping(row) for row in rows)


@lru_cache(maxsize=16)
def _skill(name: str) -> str:
    return (_SKILLS_PATH / name).read_text(encoding="utf-8").strip()


def load_skill_instruction(name: str) -> str:
    """Load a reviewed prompt fragment from the repository, not user content."""
    return _skill(name)


def _segments(text: str, *, limit: int = 20) -> list[str]:
    """Retrieve against short spans so unrelated parts of a mixed CV survive."""
    values = []
    for line in _LINE_SPLIT.split(text):
        normalized = " ".join(line.split())
        while normalized:
            values.append(normalized[:360])
            normalized = normalized[360:]
    values = list(dict.fromkeys(line for line in values if len(line) >= 5))
    if len(values) <= limit:
        return values
    # Preserve spans that name concrete tools before sampling the rest of a long
    # CV. Otherwise an isolated "Excel" or "Python" line can disappear even
    # though the source text is included in the provider request.
    concrete = []
    for concept in seed_concepts():
        if concept.concept_id.startswith("tech.") or concept.concept_id in {"sales.crm", "marketing.seo"}:
            for name in (concept.label, *concept.aliases):
                if len(name) >= 3:
                    concrete.append(re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.I))
    priority = [index for index, value in enumerate(values) if any(pattern.search(value) for pattern in concrete)]
    priority_budget = min(len(priority), max(1, limit // 2))
    priority_positions = {priority[round(i * (len(priority) - 1) / (priority_budget - 1))]
                          for i in range(priority_budget)} if priority_budget > 1 else set(priority[:1])
    remaining = limit - len(priority_positions)
    # Evenly sample the complete document for chronology, education and soft
    # evidence, then fill any collisions with the remaining lines.
    positions = set(priority_positions)
    positions.update(round(i * (len(values) - 1) / max(1, remaining - 1)) for i in range(remaining))
    if len(positions) < limit:
        positions.update(index for index in range(len(values)) if index not in positions and len(positions) < limit)
    return [values[index] for index in sorted(positions)]


def _card(hit: SkillCatalogHit) -> str:
    concept = hit.concept
    aliases = ", ".join(concept.aliases[:5]) or "—"
    positive = "; ".join(concept.positive_signals[:3]) or "requiere mención o práctica atribuida"
    negative = "; ".join(concept.negative_signals[:3]) or "no inferir por cargo, título o curso aislado"
    return (f"- {concept.label} [{concept.concept_id}; {concept.domain}]. "
            f"Definición: {concept.definition} Sinónimos: {aliases}. "
            f"Señales a evaluar: {positive}. No basta: {negative}.")


def _select_domains(text: str, hits: list[SkillCatalogHit]) -> tuple[str, ...]:
    domains = {hit.concept.domain for hit in hits[:16] if hit.score >= 0.6}
    domains.update(name for name, pattern in _DOMAIN_TERMS.items() if pattern.search(text))
    return tuple(name for name in ("technical", "marketing") if name in domains)


async def build_cv_skill_context(text: str, *, max_chars: int = 16000,
                                 store: SkillCatalogStore | None = None) -> str:
    """Return model instructions and retrieved definitions, never CV evidence.

    The caller still supplies the original CV text. The catalog is only a guide
    to recognizing concepts; its examples must not be copied into output quotes.
    """
    if max_chars < 8000:
        raise ValueError("max_chars must leave room for the fixed skill guidance")
    own_store = store is None
    catalog = store or SkillCatalogStore.from_env(concepts=seed_concepts())
    try:
        segments = _segments(text)
        vectors = await embed_queries(segments) if catalog.configured else None
        collected: dict[str, SkillCatalogHit] = {}
        for index, segment in enumerate(segments):
            vector = vectors[index] if vectors is not None and index < len(vectors) else None
            for hit in await catalog.search(segment, embedding=vector, limit=8):
                if hit.score < (0.50 if hit.method == "vector" else 0.35):
                    continue
                existing = collected.get(hit.concept.concept_id)
                if existing is None or hit.score > existing.score:
                    collected[hit.concept.concept_id] = hit
        hits = sorted(collected.values(), key=lambda hit: (-hit.score, hit.concept.concept_id))
        domains = _select_domains(text, hits)
        parts = [
            _skill("general/SKILL.md"),
            _skill("PROFILE_FORMAT.md"),
            _skill("soft/SKILL.md"),
        ]
        parts.extend(_skill(f"{domain}/SKILL.md") for domain in domains)
        parts.append(
            "## Contexto recuperado del catálogo\n"
            "Estas fichas son definiciones y ejemplos, NO evidencia del postulante. "
            "Una habilidad entra al resultado solo con una cita literal del CV que la atribuya. "
            "Una similitud vectorial propone candidatos, no autoriza inferencias. "
            "Si el CV nombra una competencia que no aparece aquí, extraela igualmente."
        )
        total = sum(len(part) + 2 for part in parts)
        for hit in hits:
            card = _card(hit)
            if total + len(card) + 2 > max_chars:
                break
            parts.append(card)
            total += len(card) + 2
        logger.info("CV RAG context segments=%d concepts=%d domains=%s chars=%d backend=%s",
                    len(segments), len(parts) - 4 - len(domains), ",".join(domains),
                    total, getattr(catalog, "last_search_backend", "local"))
        return "\n\n".join(parts)
    finally:
        if own_store:
            await catalog.close()
