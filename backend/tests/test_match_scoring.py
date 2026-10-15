import unittest
from uuid import uuid4


from app.services.match_scoring import build_result


class ScoringTests(unittest.TestCase):
    def _answers(self, exclusionary=1):
        answers = {
            key: {'score': 4, 'confidence': 0.8}
            for key in ('mandatory_skills', 'experience', 'education_and_languages', 'preferences_and_desirables')
        }
        answers['exclusionary_requirements'] = {'noul': exclusionary}
        return answers

    def test_unknown_requirement_reduces_coverage_without_becoming_non_compliance(self):
        request = {
            'request_id': str(uuid4()),
            'candidate_profile': {'skills': [{'name': 'Python', 'level': 'advanced'}]},
            'job_offer': {'requirements': [
                {'name': 'Python', 'minimum_level': 'intermediate', 'mandatory': True},
                {'name': 'Docker', 'minimum_level': 'beginner', 'mandatory': True},
            ]},
        }
        result = build_result(request, {'answers': self._answers()})
        self.assertEqual(result.match_percentage, 100)
        self.assertEqual(result.coverage_percentage, 50)
        self.assertEqual(result.assessments[1].status, 'unknown')

    def test_missing_exclusionary_requirement_caps_match(self):
        request = {
            'request_id': str(uuid4()),
            'candidate_profile': {'skills': []},
            'job_offer': {'requirements': [{'name': 'Python', 'mandatory': True, 'exclusionary': True}]},
        }
        result = build_result(request, {'answers': self._answers(exclusionary=0)})
        self.assertEqual(result.match_percentage, 40)

    def test_missing_provider_dimensions_are_rejected(self):
        with self.assertRaises(ValueError):
            build_result({'request_id': str(uuid4())}, {'answers': {}})

    def test_weighted_percentage_is_deterministic(self):
        answers = self._answers()
        answers['mandatory_skills']['score'] = 2
        answers['experience']['score'] = 0
        request = {'request_id': str(uuid4()), 'candidate_profile': {'skills': []}, 'job_offer': {'requirements': []}}
        result = build_result(request, {'answers': answers})
        self.assertEqual(result.match_percentage, 52.5)


if __name__ == '__main__':
    unittest.main()
