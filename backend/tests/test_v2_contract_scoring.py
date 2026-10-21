import unittest
from datetime import date
from uuid import uuid4

from pydantic import ValidationError

from ien_contracts.models import MatchRequest, ScoringConfig
from app.services.match_scoring import build_result


WEIGHTS = {
    "technical_skills": 3500, "soft_skills": 1500, "experience": 2000,
    "education": 1000, "languages": 500, "logistics": 500, "desirables": 1000,
}


def valid_match_request(requirements=None):
    return {
        "protocol_version": "match.v2",
        "request_id": str(uuid4()),
        "candidate_profile": {
            "schema_version": "candidate-profile.v2", "profile_id": "candidate-1",
            "skills": [{"name": "Conducción profesional", "kind": "technical", "origin": "declared",
                        "evidence": ["Licencia profesional declarada"], "source_quotes": ["Professional driver"]}],
        },
        "job_offer": {"schema_version": "job-offer.v2", "offer_id": "offer-1", "title": "Chofer",
                       "requirements": requirements or [{"id": "r1", "category": "technical_skills", "name": "Licencia profesional", "mandatory": True}]},
        "metadata": {"candidate_profile_hash": "a" * 64, "candidate_profile_version": 1,
                     "offer_profile_hash": "b" * 64, "offer_profile_version": 1,
                     "rubric_version": "v2", "matcher_prompt_version": "v2", "model": "fixture",
                     "evaluation_date": date(2026, 10, 16)},
        "scoring_config": {"version": "test-1", "weights": WEIGHTS},
    }


class V2ContractScoringTests(unittest.TestCase):
    def test_weights_reject_booleans_strings_and_wrong_sum(self):
        for value in (True, "3500"):
            weights = {**WEIGHTS, "technical_skills": value}
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ScoringConfig.model_validate({"version": "v2", "weights": weights})
        weights = {**WEIGHTS, "technical_skills": 3499}
        with self.assertRaises(ValidationError):
            ScoringConfig.model_validate({"version": "v2", "weights": weights})

    def test_v2_requires_explicit_unique_ids_and_desirables_for_optional_criteria(self):
        payload = valid_match_request([{"id": "r1", "category": "technical_skills", "name": "X", "mandatory": True}])
        payload["job_offer"]["requirements"].append({"id": "r2", "category": "technical_skills", "name": "Y", "mandatory": False})
        with self.assertRaises(ValidationError):
            MatchRequest.model_validate(payload)
        payload["job_offer"]["requirements"][1]["category"] = "desirables"
        MatchRequest.model_validate(payload)

    def test_declared_credentials_need_real_evidence_and_source_quotes(self):
        payload = valid_match_request()
        payload["candidate_profile"]["credentials"] = [{"name": "CDL", "evidence": [" "], "source_quotes": [""]}]
        with self.assertRaises(ValidationError):
            MatchRequest.model_validate(payload)
        payload["candidate_profile"]["credentials"] = [{"name": "CDL", "evidence": ["Licencia registrada"]}]
        with self.assertRaises(ValidationError):
            MatchRequest.model_validate(payload)

    def test_v2_scoring_uses_cited_profile_facts_and_reallocates_absent_dimensions(self):
        request = valid_match_request()
        request["job_offer"]["requirements"].append({"id": "r2", "category": "desirables", "name": "Inglés", "mandatory": False})
        result = build_result(request, {"assessments": [
            {"requirement_id": "r1", "status": "met", "explanation": "El perfil incluye evidencia declarada.", "evidence_refs": ["/skills/0/evidence/0"]},
            {"requirement_id": "r2", "status": "unknown", "explanation": "No hay datos de idioma.", "evidence_refs": []},
        ]})
        self.assertEqual(result.schema_version, "match-result.v2")
        self.assertEqual(result.match_percentage, 77.78)
        self.assertIsNone(result.confidence_percentage)
        self.assertFalse(result.confidence_available)
        self.assertAlmostEqual(result.breakdown.technical_skills.effective_weight_bp, 7777.7778, places=2)
        self.assertTrue(result.breakdown.desirables.applicable)

    def test_v2_rejects_unindexed_evidence_and_not_met_without_evidence(self):
        request = valid_match_request()
        assessment = {"requirement_id": "r1", "status": "not_met", "explanation": "No cumple.", "evidence_refs": []}
        with self.assertRaisesRegex(ValueError, "necesita evidencia"):
            build_result(request, {"assessments": [assessment]})


if __name__ == "__main__":
    unittest.main()
