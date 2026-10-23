"""Acceptance checks for evidence-based v2 profiles, offers, matching and uploads.

These tests use synthetic snapshots and mocked model output only. They never call a
real model provider or depend on credentials.
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import unittest
import zipfile
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from pydantic import ValidationError
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import CVUploadGuard, app
from app.services import cv_pipeline
from app.services.cv_compact_contract import contract_profile_to_wire
from app.services.document_text import DocumentRejected, detect_format, extract_document_text
from app.services.match_scoring import build_result
from ien_contracts.models import CandidateProfile, JobOffer, MatchRequest, ScoringConfig


WEIGHTS = {
    "technical_skills": 3500,
    "soft_skills": 1500,
    "experience": 2000,
    "education": 1000,
    "languages": 500,
    "logistics": 500,
    "desirables": 1000,
}


def profile(**overrides):
    value = {
        "schema_version": "candidate-profile.v2",
        "profile_id": "42",
        "skills": [],
        "work_experience": [],
        "education": [],
        "languages": [],
        "preferences": {"locations": [], "work_modes": []},
        "credentials": [],
        "rejected_skills": [],
    }
    value.update(overrides)
    return value


def requirement(rid="r1", category="technical_skills", mandatory=True, **extra):
    return {"id": rid, "category": category, "source": "required_text", "name": "Requisito", "mandatory": mandatory, **extra}


def scoring_request(requirements, candidate=None, weights=None):
    return {
        "protocol_version": "match.v2",
        "request_id": str(uuid4()),
        "candidate_profile": candidate or profile(),
        "job_offer": {"schema_version": "job-offer.v2", "offer_id": "9", "title": "Puesto", "requirements": requirements},
        "scoring_config": {"version": "weights-1", "weights": weights or WEIGHTS},
    }


def judgment(rid, status="met", refs=None):
    return {"requirement_id": rid, "status": status, "explanation": "La evidencia respalda la evaluación.", "evidence_refs": refs or []}


def make_zip(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class ScoringConfigAcceptanceTests(unittest.TestCase):
    def test_weights_require_exact_integer_basis_points_and_total(self):
        self.assertEqual(ScoringConfig(version="v1", weights=WEIGHTS).weights, WEIGHTS)
        invalid = [
            {**WEIGHTS, "technical_skills": True},
            {**WEIGHTS, "technical_skills": "3500"},
            {**WEIGHTS, "technical_skills": float("nan")},
            {**WEIGHTS, "extra": 0},
            {key: value for key, value in WEIGHTS.items() if key != "desirables"},
            {**WEIGHTS, "technical_skills": 3499},
        ]
        for weights in invalid:
            with self.subTest(weights=weights), self.assertRaises((ValidationError, ValueError)):
                ScoringConfig(version="v1", weights=weights)

    def test_match_v2_requires_config_ids_categories_and_consistent_classification(self):
        metadata = {
            "candidate_profile_hash": "a" * 64, "candidate_profile_version": 1,
            "offer_profile_hash": "b" * 64, "offer_profile_version": 1,
            "rubric_version": "r1", "matcher_prompt_version": "m1", "model": "mock", "evaluation_date": "2026-10-16",
        }
        base = {
            "protocol_version": "match.v2", "request_id": str(uuid4()),
            "candidate_profile": profile(),
            "job_offer": {"schema_version": "job-offer.v2", "offer_id": "9", "title": "Puesto", "requirements": [requirement()]},
            "metadata": metadata, "scoring_config": {"version": "v1", "weights": WEIGHTS},
        }
        MatchRequest.model_validate(base)
        for index, change in enumerate((
            {"scoring_config": None},
            {"job_offer": {**base["job_offer"], "requirements": [requirement(category=None)]}},
            {"job_offer": {**base["job_offer"], "requirements": [requirement("same"), requirement("same")]}},
            {"job_offer": {**base["job_offer"], "requirements": [requirement(category="languages", mandatory=False)]}},
        )):
            candidate = {**base, **change}
            with self.subTest(index=index, change=change), self.assertRaises((ValidationError, ValueError)):
                MatchRequest.model_validate(candidate)


class MatchV2AcceptanceTests(unittest.TestCase):
    def test_fixed_judgments_are_keyed_by_id_and_require_exact_set(self):
        req = scoring_request([requirement("one"), requirement("two", "experience")])
        candidate = profile(work_experience=[{"job_title": "Operario", "highlights": ["Mantuvo maquinaria"]}])
        refs = ["/work_experience/0/highlights/0"]
        response = {"assessments": [judgment("two", refs=refs), judgment("one", refs=refs)]}
        result = build_result({**req, "candidate_profile": candidate}, response)
        self.assertEqual([item.requirement_id for item in result.assessments], ["one", "two"])
        for returned in ([judgment("one", refs=refs)], [judgment("one", refs=refs), judgment("one", refs=refs)],
                         [judgment("one", refs=refs), judgment("unknown", refs=refs)]):
            with self.subTest(returned=returned), self.assertRaises(ValueError):
                build_result({**req, "candidate_profile": candidate}, {"assessments": returned})

    def test_evidence_can_come_from_structured_fields_without_summary(self):
        candidate = profile(
            work_experience=[{"job_title": "Chofer", "duration_months": 24, "highlights": ["Planificó rutas"]}],
            education=[{"level": "technical", "field": "Logística", "completed": True}],
            languages=[{"name": "Inglés", "level": "professional"}],
            preferences={"locations": ["Córdoba"], "work_modes": ["onsite"]},
        )
        requirements = [requirement("exp", "experience"), requirement("edu", "education"),
                        requirement("lang", "languages"), requirement("pref", "logistics")]
        refs = ["/work_experience/0/highlights/0", "/education/0/field", "/languages/0/name", "/preferences/locations/0"]
        result = build_result(scoring_request(requirements, candidate), {"assessments": [judgment(r["id"], refs=[ref]) for r, ref in zip(requirements, refs)]})
        self.assertEqual(result.match_percentage, 100)

    def test_not_met_requires_contradictory_evidence_and_unknown_cannot_claim_match(self):
        candidate = profile(skills=[{"name": "Soldadura", "level": "beginner", "evidence": ["Realizó soldadura básica"], "source_quotes": ["basic welding"], "kind": "technical", "origin": "inferred"}])
        req = [requirement(minimum_level="expert")]
        request = scoring_request(req, candidate)
        evidence = ["/skills/0/evidence/0"]
        for assessment in (judgment("r1", "not_met"), judgment("r1", "met", ["/headline"])):
            with self.subTest(assessment=assessment), self.assertRaises(ValueError):
                build_result(request, {"assessments": [assessment]})
        result = build_result(request, {"assessments": [judgment("r1", "unknown")]})
        self.assertEqual(result.match_percentage, 0)
        self.assertEqual(result.coverage_percentage, 0)

    def test_unsolicited_dimension_does_not_create_free_points_and_zero_weight_keeps_mandatory_state(self):
        candidate = profile(skills=[{"name": "Python", "level": "advanced", "evidence": ["Automatizó reportes"], "source_quotes": ["Automated reports"], "kind": "technical", "origin": "inferred"}])
        req = [requirement(category="technical_skills"), requirement("exp", "desirables", mandatory=False)]
        refs = ["/skills/0/evidence/0"]
        result_judgments = [judgment("r1", refs=refs), judgment("exp", refs=["/skills/0/evidence/0"])]
        no_unsolicited = build_result(scoring_request(req, candidate), {"assessments": result_judgments})
        with_unsolicited = build_result(scoring_request(req, candidate), {"assessments": result_judgments})
        self.assertEqual(no_unsolicited.match_percentage, with_unsolicited.match_percentage)
        weights = {**WEIGHTS, "technical_skills": 0, "experience": 2000, "desirables": 4500}
        result = build_result(scoring_request(req, candidate, weights), {"assessments": [judgment("r1", "unknown"), judgment("exp", refs=["/skills/0/evidence/0"])]})
        self.assertEqual(result.match_percentage, 100)
        self.assertEqual(result.mandatory_status, "unknown")
        self.assertEqual(result.breakdown.technical_skills.configured_weight_bp, 0)

    def test_weight_change_is_reproducible_and_saved_in_result(self):
        candidate = profile(
            skills=[{"name": "Python", "level": "advanced", "evidence": ["Automatizó reportes"], "source_quotes": ["Automated reports"], "kind": "technical", "origin": "inferred"}],
            work_experience=[{"job_title": "Analista", "highlights": ["Armó reportes"]}],
        )
        req = [requirement("skill"), requirement("exp", "experience")]
        refs = {"skill": ["/skills/0/evidence/0"], "exp": ["/work_experience/0/highlights/0"]}
        response = {"assessments": [judgment("skill", refs=refs["skill"]), judgment("exp", "unknown")]}
        first = build_result(scoring_request(req, candidate), response)
        changed = {**WEIGHTS, "technical_skills": 7000, "experience": 0, "soft_skills": 0, "education": 0, "languages": 0, "logistics": 0, "desirables": 3000}
        second = build_result(scoring_request(req, candidate, changed), response)
        self.assertNotEqual(first.match_percentage, second.match_percentage)
        self.assertEqual(second.scoring_config.weights, changed)

    def test_requirement_points_sum_to_the_displayed_match(self):
        candidate = profile(skills=[{"name": "CRM", "level": "unknown", "evidence": ["Usó CRM"],
                                    "source_quotes": ["Usó CRM"], "kind": "technical", "origin": "declared"}])
        req = [requirement("crm"), requirement("excel"), requirement("exp", "experience")]
        response = {"assessments": [
            judgment("crm", "met", ["/skills/0/evidence/0"]),
            judgment("excel", "unknown"),
            judgment("exp", "partial", ["/skills/0/evidence/0"]),
        ]}
        weights = {key: 0 for key in WEIGHTS}
        weights.update(technical_skills=7000, experience=3000)
        result = build_result(scoring_request(req, candidate, weights), response)
        self.assertEqual(result.match_percentage, 50)
        self.assertEqual([a.points_awarded for a in result.assessments], [35, 0, 15])
        self.assertEqual([a.points_possible for a in result.assessments], [35, 35, 30])
        self.assertEqual(round(sum(a.points_awarded for a in result.assessments), 2), result.match_percentage)

    def test_course_alone_never_scores_as_technical_skill(self):
        candidate = profile(education=[{"level": "other", "field": "Curso de Excel", "completed": True}])
        req = scoring_request([requirement("excel", name="Manejo de Excel")], candidate)
        result = build_result(req, {"assessments": [judgment("excel", "partial", ["/education/0/field"])]})
        self.assertEqual(result.match_percentage, 0)
        self.assertEqual(result.assessments[0].status, "unknown")
        self.assertEqual(result.assessments[0].evidence_refs, [])


class ProfileAndOfferPipelineAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_rejects_unsupported_skill_evidence_quotes_and_user_declared_output(self):
        valid_skill = {"name": "Soldadura", "level": "intermediate", "evidence": ["Soldó piezas"], "source_quotes": ["Welded parts"], "kind": "technical", "origin": "inferred"}
        requests = []
        with self.assertRaises(ValidationError):
            CandidateProfile.model_validate({**profile(), "skills": [{**valid_skill, "source_quotes": []}]})
        with self.assertRaises(ValidationError):
            CandidateProfile.model_validate({**profile(), "skills": [{"name": "Inventada", "origin": "inferred"}]})

        async def call_with_skill(item):
            async def response(_system, payload, marker, max_output_tokens=950):
                request = json.loads(payload)
                requests.append(request)
                if marker.endswith("consolidation-plan"):
                    return {"h": "Soldador", "s": None, "sk": [["sk0"]], "x": [], "ed": [], "l": [], "c": []}
                return {"sk": contract_profile_to_wire(profile(skills=[item]))["sk"]}
            with patch.object(cv_pipeline, "_call_async", side_effect=response):
                return await cv_pipeline.process_cv_text("Welded parts", 42)

        with self.assertRaises(ValueError):
            await call_with_skill({**valid_skill, "source_quotes": ["Invented quote"]})
        translated = await call_with_skill(valid_skill)
        self.assertEqual(translated["skills"][0]["evidence"], ["Soldó piezas"])
        self.assertEqual(translated["skills"][0]["source_quotes"], ["Welded parts"])

        with self.assertRaises(ValueError):
            await call_with_skill({**valid_skill, "origin": "user_declared"})
        self.assertEqual(translated["rejected_skills"], [])
        self.assertTrue(all("rejected_skills" not in json.dumps(request) for request in requests))

    async def test_offer_model_cannot_swap_requirement_provenance_or_classification(self):
        snapshot = {"requirements": [requirement("a", "technical_skills", True), requirement("b", "desirables", False)]}
        model_output = {"schema_version": "job-offer.v2", "offer_id": "9", "title": "Puesto", "requirements": [
            {**snapshot["requirements"][1], "name": "Traducido B"},
            {**snapshot["requirements"][0], "name": "Traducido A"},
        ]}
        async def fake_call(*_args): return model_output
        with patch.object(cv_pipeline, "_call_async", side_effect=fake_call):
            result = await cv_pipeline.process_offer_snapshot(snapshot, 9)
        self.assertEqual([(r["id"], r["mandatory"], r["category"], r["source"]) for r in result["requirements"]],
                         [("a", True, "technical_skills", "required_text"), ("b", False, "desirables", "required_text")])

    async def test_normalization_changes_only_translatable_text(self):
        original = profile(headline="Logistics operator", skills=[{"name": "Forklift", "level": "advanced", "years_experience": 4, "evidence": ["Operated a forklift"], "source_quotes": ["Operated a forklift"], "kind": "technical", "origin": "declared"}], credentials=[{"name": "License X", "issued_at": "2022", "origin": "declared", "evidence": ["License X"], "source_quotes": ["License X"]}], rejected_skills=[{"name": "Hacking", "reason": "No se menciona en el CV"}])
        normalized_original = CandidateProfile.model_validate(original).model_dump(mode="json")
        translations = cv_pipeline._translatable_profile_fields(normalized_original)
        translations.update({"/headline": "Operador logístico", "/skills/0/name": "Autoelevador"})
        async def fake_call(*_args): return {"translations": translations}
        with patch.object(cv_pipeline, "_call_async", side_effect=fake_call):
            result = await cv_pipeline.normalize_candidate_profile(original, 42)
        self.assertEqual(result["headline"], "Operador logístico")
        self.assertEqual(result["skills"][0]["name"], "Autoelevador")
        for key in ("level", "years_experience", "evidence", "source_quotes", "origin", "kind"):
            self.assertEqual(result["skills"][0][key], original["skills"][0][key])
        self.assertEqual(result["credentials"], normalized_original["credentials"])
        self.assertEqual(result["rejected_skills"], normalized_original["rejected_skills"])


class DocumentUploadAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.token_patch = patch.object(settings, "SERVICE_API_TOKEN", "service-token")
        self.token_patch.start()
        self.addCleanup(self.token_patch.stop)
        self.client = TestClient(app)

    def test_text_and_unknown_extensions_are_detected_by_content(self):
        self.assertEqual(detect_format("cv.txt", "Experiencia en logística".encode("utf-8")), "txt")
        for filename, content in (("cv.exe", b"MZbinary"), ("cv.html", b"<html>CV</html>"), ("cv.txt", b"%PDF-1.4")):
            with self.subTest(filename=filename), self.assertRaises(DocumentRejected):
                detect_format(filename, content)

    def test_docx_reads_body_headers_and_footers_and_rejects_external_dtd_and_active_content(self):
        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        doc = f'<w:document xmlns:w="{ns}"><w:body><w:p><w:r><w:t>Body CV</w:t></w:r></w:p></w:body></w:document>'
        header = f'<w:hdr xmlns:w="{ns}"><w:p><w:r><w:t>Header CV</w:t></w:r></w:p></w:hdr>'
        footer = f'<w:ftr xmlns:w="{ns}"><w:p><w:r><w:t>Footer CV</w:t></w:r></w:p></w:ftr>'
        rels = '<Relationships><Relationship TargetMode="External" Target="https://invalid.test"/></Relationships>'
        safe = make_zip({"[Content_Types].xml": "<Types/>", "word/document.xml": doc, "word/header1.xml": header, "word/footer1.xml": footer})
        text = extract_document_text(safe, "docx")
        self.assertIn("Body CV", text); self.assertIn("Header CV", text); self.assertIn("Footer CV", text)
        for entries in (
            {"[Content_Types].xml": "<Types/>", "word/document.xml": doc, "word/_rels/document.xml.rels": rels},
            {"[Content_Types].xml": "<Types/>", "word/document.xml": '<!DOCTYPE x [<!ENTITY y "z">]><x>&y;</x>'},
            {"[Content_Types].xml": "<Types/>", "word/document.xml": doc, "word/vbaProject.bin": b"macro"},
            {"[Content_Types].xml": "<Types/>", "word/document.xml": doc, "word/media/packed.bin": b"A" * 500_000},
        ):
            with self.subTest(entries=list(entries)), self.assertRaises(DocumentRejected):
                extract_document_text(make_zip(entries), "docx")

    def test_odt_tail_text_is_preserved_and_scripts_are_rejected(self):
        content = '<office:document-content xmlns:office="urn:o" xmlns:text="urn:t"><office:body><text:p>Antes<text:span> medio</text:span> después</text:p></office:body></office:document-content>'
        odt = make_zip({"mimetype": "application/vnd.oasis.opendocument.text", "content.xml": content})
        self.assertIn("después", extract_document_text(odt, "odt"))
        active = make_zip({"mimetype": "application/vnd.oasis.opendocument.text", "content.xml": content, "Scripts/basic.xml": "x"})
        with self.assertRaises(DocumentRejected): extract_document_text(active, "odt")

    def test_upload_requires_auth_before_body_read_and_limits_chunked_body(self):
        async def exercise(headers, chunks):
            calls = 0
            responses = []
            async def receive():
                nonlocal calls
                calls += 1
                return chunks.pop(0)
            async def send(message): responses.append(message)
            async def downstream(scope, recv, snd):
                await recv()
                await snd({"type": "http.response.start", "status": 204, "headers": []})
            guard = CVUploadGuard(downstream)
            await guard({"type": "http", "method": "POST", "path": "/api/v1/cv-analyses", "headers": headers}, receive, send)
            return calls, responses
        calls, responses = asyncio.run(exercise([], [{"type": "http.request", "body": b"large", "more_body": False}]))
        self.assertEqual(calls, 0)
        self.assertEqual(responses[0]["status"], 401)

        limit_patch = patch.object(settings, "CV_MAX_PDF_BYTES", 4)
        overhead_patch = patch.object(settings, "CV_MAX_UPLOAD_BODY_OVERHEAD", 1)
        limit_patch.start(); overhead_patch.start()
        self.addCleanup(limit_patch.stop); self.addCleanup(overhead_patch.stop)
        headers = [(b"authorization", b"Bearer service-token")]
        calls, responses = asyncio.run(exercise(headers, [{"type": "http.request", "body": b"123", "more_body": True}, {"type": "http.request", "body": b"456", "more_body": False}]))
        self.assertEqual(responses[0]["status"], 413)

    def test_unauthenticated_upload_endpoint_does_not_parse_multipart(self):
        response = self.client.post("/api/v1/cv-analyses", content=b"not even multipart", headers={"Content-Type": "multipart/form-data; boundary=x"})
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
