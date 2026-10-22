"""Per-job packet manifests and resumable canonical CV extraction."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import logging
import re
from typing import Any

from app.services.cv_compact_contract import validate_expanded_profile, validate_fragment
from app.services.cv_consolidation import (
    ConsolidationPlan,
    compact_facts_for_plan,
    validate_and_build_profile_with_effective_plan,
)
from app.services.prompt_loader import load_prompt
from app.services.rate_limiter import ProviderResponseTruncated


PACKET_MANIFEST_VERSION = "cv-packets.v2"
_PREVIOUS_PACKET_MANIFEST_VERSION = "cv-packets.v1"
logger = logging.getLogger(__name__)


class PacketsIdentityError(ValueError):
    """Packet checkpoint identity or graph is invalid and must not be reused."""

    def __init__(self) -> None:
        super().__init__("El progreso guardado del CV no coincide con este análisis. Iniciá un análisis nuevo.")


def _quote_catalog(text: str) -> dict[str, str]:
    """Source-owned literal spans let small models cite IDs without rewriting text."""
    spans = []
    for sentence in re.split(r"(?<=[.!?;])\s+|\n+", text):
        sentence = sentence.strip()
        if not sentence:
            continue
        start = 0
        while start < len(sentence):
            quote = sentence[start:start + 160]
            if quote and quote not in spans:
                spans.append(quote)
            if start + 160 >= len(sentence):
                break
            start += 128
    return {f"quote_{index}": span for index, span in enumerate(spans)}


def _resolve_quote_ids(raw: dict, catalog: dict[str, str]) -> dict:
    value = copy.deepcopy(raw)
    for section in ("sk", "c", "ed"):
        entries = value.get(section, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            quotes = entry.get("q", [])
            if isinstance(quotes, str):
                quotes = [quotes]
            if isinstance(quotes, list):
                entry["q"] = [catalog.get(quote, quote) if isinstance(quote, str) else quote for quote in quotes]
    return value


def make_packet_identity(
    *,
    job_id: str,
    analysis_id: int,
    candidate_id: int,
    cv_version_id: int | None,
    source_hash: str,
    text: str,
    prompt_version: str,
    provider: str,
    model: str,
    packet_chars: int,
    block_chars: int,
    budget: int,
) -> dict[str, Any]:
    return {
        "job_id": str(job_id),
        "analysis_id": int(analysis_id),
        "candidate_id": int(candidate_id),
        "cv_version_id": int(cv_version_id) if cv_version_id is not None else None,
        "source_hash": source_hash,
        "text_hash": _sha256(text),
        "prompt_version": prompt_version,
        "provider": provider.strip().lower(),
        "model": model,
        "packet_chars": int(packet_chars),
        "block_chars": int(block_chars),
        "budget": int(budget),
    }


def count_completed_packets(manifest: dict[str, Any] | None) -> int:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("packets"), dict):
        return 0
    return sum(
        1 for packet in manifest["packets"].values()
        if isinstance(packet, dict) and packet.get("status") == "completed"
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _packet_id(identity: dict[str, Any], start: int, end: int, path: str, packet_text: str) -> str:
    stable = {
        "identity": identity,
        "start": start,
        "end": end,
        "path": path,
        "packet_text_hash": _sha256(packet_text),
    }
    digest = hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"p-{digest[:32]}"


def _packet(identity: dict[str, Any], index: int, path: str, start: int, end: int, text: str) -> dict[str, Any]:
    packet_text = text[start:end]
    return {
        "id": _packet_id(identity, start, end, path, packet_text),
        "index": index,
        "path": path,
        "start": start,
        "end": end,
        "text_hash": _sha256(packet_text),
        "status": "pending",
        "children": [],
        "result": None,
        "provider_model": None,
    }


def new_manifest(text: str, identity: dict[str, Any]) -> dict[str, Any]:
    packet_limit = min(int(identity["packet_chars"]), int(identity["block_chars"]))
    if packet_limit < 1:
        raise PacketsIdentityError()
    roots: list[str] = []
    packets: dict[str, dict[str, Any]] = {}
    start = 0
    index = 0
    while start < len(text):
        end = min(len(text), start + packet_limit)
        if end < len(text):
            lower = start + max(1, int(packet_limit * 0.7))
            # Prefer a paragraph boundary, then a sentence boundary. If the
            # source has neither, split at the first word boundary in the last
            # 30%, so a trailing technology/name is not severed mid-token.
            newline = text.rfind("\n", lower, end)
            if newline >= lower:
                end = newline + 1
            else:
                boundary = next((pos + 1 for pos in range(lower, end)
                                 if text[pos] in ".!?;" and text[pos + 1].isspace()), None)
                if boundary is None:
                    boundary = next((pos + 1 for pos in range(lower, end)
                                     if text[pos].isspace()), None)
                if boundary is not None:
                    end = boundary
        index += 1
        packet = _packet(identity, index, f"r{index}", start, end, text)
        packets[packet["id"]] = packet
        roots.append(packet["id"])
        start = end
    return {
        "schema_version": PACKET_MANIFEST_VERSION,
        "identity": copy.deepcopy(identity),
        "roots": roots,
        "packets": packets,
        "consolidation": {"status": "pending", "result": None, "provider_model": None, "plan": None},
    }


def validate_manifest(
    manifest: Any,
    text: str,
    expected_identity: dict[str, Any],
) -> dict[str, Any]:
    """Validate identity, exact coverage, deterministic nodes and every cached result."""
    if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "identity", "roots", "packets", "consolidation"}:
        raise PacketsIdentityError()
    if manifest.get("schema_version") not in {PACKET_MANIFEST_VERSION, _PREVIOUS_PACKET_MANIFEST_VERSION}:
        raise PacketsIdentityError()
    if _canonical(manifest.get("identity")) != _canonical(expected_identity):
        raise PacketsIdentityError()
    if expected_identity.get("text_hash") != _sha256(text):
        raise PacketsIdentityError()
    roots, packets = manifest.get("roots"), manifest.get("packets")
    if not isinstance(roots, list) or not isinstance(packets, dict):
        raise PacketsIdentityError()
    expected_roots = new_manifest(text, expected_identity)
    if roots != expected_roots["roots"]:
        raise PacketsIdentityError()
    for root_id in roots:
        root = packets.get(root_id)
        expected_root = expected_roots["packets"].get(root_id)
        if not isinstance(root, dict) or not isinstance(expected_root, dict):
            raise PacketsIdentityError()
        if any(root.get(key) != expected_root[key] for key in ("index", "path", "start", "end", "text_hash")):
            raise PacketsIdentityError()
    consolidation = manifest.get("consolidation")
    migrate_pending_manifest = False
    if (
        manifest.get("schema_version") == _PREVIOUS_PACKET_MANIFEST_VERSION
        and isinstance(consolidation, dict)
        and consolidation.get("status") == "pending"
        and consolidation.get("result") is None
        and consolidation.get("provider_model") is None
        and (
            set(consolidation) == {"status", "result", "provider_model"}
            or (
                set(consolidation) == {"status", "result", "provider_model", "plan"}
                and consolidation.get("plan") is None
            )
        )
    ):
        # Previous packet runs already validated every leaf against its exact
        # range. Upgrade their unfinished consolidation without discarding it.
        consolidation.setdefault("plan", None)
        migrate_pending_manifest = True
    if not isinstance(consolidation, dict) or set(consolidation) != {"status", "result", "provider_model", "plan"}:
        raise PacketsIdentityError()
    if consolidation["status"] not in {"pending", "completed"}:
        raise PacketsIdentityError()
    if consolidation["status"] == "pending" and any(
        consolidation[key] is not None for key in ("result", "provider_model", "plan")
    ):
        raise PacketsIdentityError()
    if consolidation["status"] == "completed" and (
        not isinstance(consolidation["result"], dict) or not isinstance(consolidation["plan"], dict)
    ):
        raise PacketsIdentityError()
    if consolidation["provider_model"] is not None and not isinstance(consolidation["provider_model"], str):
        raise PacketsIdentityError()

    seen: set[str] = set()
    leaf_ranges: list[tuple[int, int, str, dict[str, Any] | None]] = []

    def visit(packet_id: Any, *, expected_index: int, expected_path: str, parent_range: tuple[int, int] | None = None) -> dict:
        if not isinstance(packet_id, str) or packet_id in seen:
            raise PacketsIdentityError()
        packet = packets.get(packet_id)
        if not isinstance(packet, dict):
            raise PacketsIdentityError()
        required = {"id", "index", "path", "start", "end", "text_hash", "status", "children", "result", "provider_model"}
        if set(packet) != required or packet.get("id") != packet_id:
            raise PacketsIdentityError()
        seen.add(packet_id)
        start, end = packet.get("start"), packet.get("end")
        if (
            isinstance(start, bool) or not isinstance(start, int)
            or isinstance(end, bool) or not isinstance(end, int)
            or isinstance(packet.get("index"), bool) or not isinstance(packet.get("index"), int)
            or start < 0 or end <= start or end > len(text)
            or packet.get("index") != expected_index or packet.get("path") != expected_path
        ):
            raise PacketsIdentityError()
        if parent_range is not None and (start < parent_range[0] or end > parent_range[1]):
            raise PacketsIdentityError()
        packet_text = text[start:end]
        if packet.get("text_hash") != _sha256(packet_text):
            raise PacketsIdentityError()
        if packet_id != _packet_id(expected_identity, start, end, expected_path, packet_text):
            raise PacketsIdentityError()
        status, children, result = packet.get("status"), packet.get("children"), packet.get("result")
        if not isinstance(children, list):
            raise PacketsIdentityError()
        if status == "pending":
            if children or result is not None or packet.get("provider_model") is not None:
                raise PacketsIdentityError()
            leaf_ranges.append((start, end, "pending", None))
        elif status == "completed":
            if children or not isinstance(result, dict):
                raise PacketsIdentityError()
            try:
                _expanded, compact = validate_fragment(result, packet_text)
            except Exception as exc:
                raise PacketsIdentityError() from None
            if compact != result:
                raise PacketsIdentityError()
            if packet.get("provider_model") is not None and not isinstance(packet["provider_model"], str):
                raise PacketsIdentityError()
            leaf_ranges.append((start, end, "completed", result))
        elif status == "split":
            if len(children) != 2 or result is not None or packet.get("provider_model") is not None:
                raise PacketsIdentityError()
            left, right = children
            left_node = packets.get(left) if isinstance(left, str) else None
            right_node = packets.get(right) if isinstance(right, str) else None
            if not isinstance(left_node, dict) or not isinstance(right_node, dict):
                raise PacketsIdentityError()
            middle = left_node.get("end")
            if (
                left_node.get("start") != start or right_node.get("start") != middle
                or left_node.get("end") != middle or right_node.get("end") != end
                or isinstance(middle, bool) or not isinstance(middle, int) or middle <= start or middle >= end
            ):
                raise PacketsIdentityError()
            visit(left, expected_index=expected_index, expected_path=expected_path + "a", parent_range=(start, middle))
            visit(right, expected_index=expected_index, expected_path=expected_path + "b", parent_range=(middle, end))
        else:
            raise PacketsIdentityError()
        return packet

    previous_end = 0
    for index, root_id in enumerate(roots, 1):
        root = visit(root_id, expected_index=index, expected_path=f"r{index}")
        if root["start"] != previous_end:
            raise PacketsIdentityError()
        previous_end = root["end"]
    if previous_end != len(text) or seen != set(packets):
        if text or roots or packets:
            raise PacketsIdentityError()
    if consolidation["status"] == "completed":
        ordered_leaves = sorted(leaf_ranges, key=lambda item: item[0])
        if (text and (not ordered_leaves or any(status != "completed" for _, _, status, _ in ordered_leaves))):
            raise PacketsIdentityError()
        cursor = 0
        fragments: list[dict[str, Any]] = []
        for start, end, _status, result in ordered_leaves:
            if start != cursor or result is None:
                raise PacketsIdentityError()
            cursor = end
            fragments.append(result)
        if cursor != len(text):
            raise PacketsIdentityError()
        try:
            rebuilt, effective_plan = validate_and_build_profile_with_effective_plan(
                consolidation["plan"], fragments, int(expected_identity["candidate_id"]),
            )
            rebuilt = validate_expanded_profile(rebuilt, text, int(expected_identity["candidate_id"]))
            expanded = validate_expanded_profile(
                consolidation["result"], text, int(expected_identity["candidate_id"]),
            )
        except Exception:
            raise PacketsIdentityError() from None
        if (
            expanded != consolidation["result"]
            or _canonical(rebuilt) != _canonical(expanded)
            or _canonical(effective_plan) != _canonical(consolidation["plan"])
        ):
            raise PacketsIdentityError()
        if consolidation["provider_model"] is not None and not isinstance(consolidation["provider_model"], str):
            raise PacketsIdentityError()
    if migrate_pending_manifest:
        # Mark the schema upgrade only after identity, graph, ranges and every
        # completed packet have passed validation.
        manifest["schema_version"] = PACKET_MANIFEST_VERSION
    return manifest


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _split_text_exact(text: str, minimum_chars: int = 40) -> tuple[str, str]:
    if len(text) < minimum_chars * 2:
        raise ProviderResponseTruncated("El paquete mínimo del CV excede el límite de salida y no puede dividirse más.")
    midpoint = len(text) // 2
    lower, upper = minimum_chars, len(text) - minimum_chars
    candidates = [i for i in range(lower, upper) if text[i] in "\r\n"]
    if not candidates:
        candidates = [i for i in range(lower, upper) if text[i].isspace()]
    split_at = min(candidates, key=lambda i: abs(i - midpoint)) if candidates else midpoint
    left, right = text[:split_at], text[split_at:]
    if len(left) < minimum_chars or len(right) < minimum_chars or left + right != text:
        raise ProviderResponseTruncated("El paquete mínimo del CV excede el límite de salida y no puede dividirse más.")
    return left, right


def _single_fragment_plan(fragments: list[dict[str, Any]], facts: dict[str, list]) -> dict | None:
    """Skip semantic grouping only when one leaf has no potentially repeated facts."""
    if len(fragments) != 1:
        return None

    def normalized(value: Any) -> str | None:
        if value is None:
            return None
        value = re.sub(r"\s+", " ", str(value)).strip().casefold()
        return None if value in {"", "unknown"} else value

    identity_fields = {
        "x": ("t", "o"), "ed": ("l", "f", "i"),
        "l": ("n",), "c": ("n", "i", "d", "x"),
    }
    for alias, rows in facts.items():
        for index, first in enumerate(rows):
            for second in rows[index + 1:]:
                if alias == "sk":
                    same_name = normalized(first.get("n")) == normalized(second.get("n"))
                    concept = normalized(first.get("ci"))
                    if same_name or (concept is not None and concept == normalized(second.get("ci"))):
                        return None
                else:
                    # A missing identity component does not prove records are
                    # different. Leave these ambiguous merges to consolidation.
                    if all(
                        normalized(first.get(key)) is None
                        or normalized(second.get(key)) is None
                        or normalized(first.get(key)) == normalized(second.get(key))
                        for key in identity_fields[alias]
                    ):
                        return None
    return {
        "h": fragments[0].get("h"), "s": fragments[0].get("s"),
        **{alias: [[row["id"]] for row in rows] for alias, rows in facts.items()},
    }


async def process_packetized_cv_text(
    text: str,
    candidate_id: int,
    *,
    identity: dict[str, Any],
    checkpoint: dict[str, Any] | None,
    on_checkpoint,
    on_heartbeat,
    call_provider,
) -> tuple[dict[str, Any], str | None]:
    previous_schema = checkpoint.get("schema_version") if isinstance(checkpoint, dict) else None
    manifest = new_manifest(text, identity) if checkpoint is None else validate_manifest(checkpoint, text, identity)

    async def save() -> None:
        if on_checkpoint is None:
            return
        value = copy.deepcopy(manifest)
        if inspect.iscoroutinefunction(on_checkpoint):
            result = await on_checkpoint(value)
        else:
            import asyncio
            result = await asyncio.to_thread(on_checkpoint, value)
        if inspect.isawaitable(result):
            await result

    if checkpoint is None or previous_schema != manifest["schema_version"]:
        await save()

    consolidation = manifest["consolidation"]
    if consolidation["status"] == "completed":
        return copy.deepcopy(consolidation["result"]), consolidation.get("provider_model")

    async def process_packet(packet_id: str) -> None:
        packet = manifest["packets"][packet_id]
        if packet["status"] == "split":
            await process_packet(packet["children"][0])
            await process_packet(packet["children"][1])
            return
        if packet["status"] == "completed":
            return
        packet_text = text[packet["start"]:packet["end"]]
        quote_catalog = _quote_catalog(packet_text)
        system_prompt = load_prompt("cv_extraction.v2.packets-soft.v8.system.md") + (
            "\nTarea: extraé hechos solo de este paquete y respondé con aliases compactos, sin identidad ni habilidades rechazadas."
        )
        payload = json.dumps({
                    "packet_id": packet_id,
                    "packet_index": packet["index"],
                    "text": packet_text,
                    "source_quotes": quote_catalog,
                    "fragment_schema": _fragment_schema(),
                }, ensure_ascii=False)
        try:
            for attempt in range(2):
                raw = await call_provider(
                    system_prompt, payload,
                    f"cv-packet-{identity['job_id']}-{packet_id}-validation-{attempt}",
                    on_heartbeat=on_heartbeat,
                )
                provider_model = raw.pop("__provider_model", None)
                try:
                    _expanded, compact = validate_fragment(_resolve_quote_ids(raw, quote_catalog), packet_text)
                    break
                except ValueError:
                    if attempt:
                        raise
                    logger.info("CV packet validation retry id=%s", packet_id)
                    system_prompt += (
                        "\nLa respuesta anterior no pasó la validación. Volvé a extraer desde el texto original: "
                        "respetá los tipos y límites de todos los arrays; cada cita q debe ser una subcadena literal "
                        "de ese texto de hasta 160 caracteres, sin cambios de conjugación ni ortografía. "
                        "No inventes evidencia para completar campos."
                        " El nombre n debe ser legible en español; los identificadores soft.* corresponden a ci, nunca a n."
                        " Las blandas deben pertenecer a las diez categorías permitidas. No llames skill al cargo, curso o título;"
                        " sí conservá una tecnología o conocimiento explícito dentro de ese texto: 'programador en Python' respalda Python."
                        " No omitas herramientas nombradas en un stack ni tecnologías distintas por un ci genérico."
                        " Cada estudio ed debe traer q con citas del programa y su estado; conservá en curso/incompleto y no supongas graduación."
                    )
        except ProviderResponseTruncated:
            left, right = _split_text_exact(packet_text)
            middle = packet["start"] + len(left)
            left_node = _packet(identity, packet["index"], packet["path"] + "a", packet["start"], middle, text)
            right_node = _packet(identity, packet["index"], packet["path"] + "b", middle, packet["end"], text)
            packet["status"] = "split"
            packet["children"] = [left_node["id"], right_node["id"]]
            manifest["packets"][left_node["id"]] = left_node
            manifest["packets"][right_node["id"]] = right_node
            logger.info("CV packet split id=%s children=2 chars=%d", packet_id, len(packet_text))
            await save()  # Persist parent/children before making either child request.
            await process_packet(left_node["id"])
            await process_packet(right_node["id"])
            return

        packet["result"] = compact
        packet["status"] = "completed"
        packet["provider_model"] = provider_model
        logger.info("CV packet completed id=%s index=%d chars=%d", packet_id, packet["index"], len(packet_text))
        await save()

    for root_id in manifest["roots"]:
        await process_packet(root_id)

    validate_manifest(manifest, text, identity)
    leaves = sorted(
        (packet for packet in manifest["packets"].values() if packet["status"] == "completed"),
        key=lambda packet: (packet["start"], packet["end"]),
    )
    if sum(1 for packet in manifest["packets"].values() if packet["status"] == "pending"):
        raise PacketsIdentityError()
    fragments = [packet["result"] for packet in leaves]
    facts = compact_facts_for_plan(fragments)
    logger.info("CV packet extraction ready packets=%d facts=%s", len(leaves), {key: len(value) for key, value in facts.items()})

    plan_raw = _single_fragment_plan(fragments, facts)
    provider_model = None
    if plan_raw is None:
        plan_raw = await call_provider(
            load_prompt("cv_consolidation.v2.refs.system.md"),
            json.dumps({
                "candidate_id": str(candidate_id),
                "candidate_profile_version": "candidate-profile.v2",
                "facts": facts,
                "plan_schema": ConsolidationPlan.model_json_schema(by_alias=True),
            }, ensure_ascii=False),
            f"cv-packet-{identity['job_id']}-consolidation-plan",
            on_heartbeat=on_heartbeat,
        )
        provider_model = plan_raw.pop("__provider_model", None)
    else:
        logger.info("CV consolidation completed locally packets=1")
    profile, effective_plan = validate_and_build_profile_with_effective_plan(plan_raw, fragments, candidate_id)
    profile = validate_expanded_profile(profile, text, candidate_id)
    manifest["consolidation"] = {
        "status": "completed",
        "result": profile,
        "provider_model": provider_model or next((packet.get("provider_model") for packet in reversed(leaves) if packet.get("provider_model")), None),
        "plan": effective_plan,
    }
    await save()
    return profile, manifest["consolidation"]["provider_model"]


def _fragment_schema() -> dict:
    from app.services.cv_compact_contract import fragment_schema
    return fragment_schema()
