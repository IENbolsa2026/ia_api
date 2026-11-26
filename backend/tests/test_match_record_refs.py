import json
from unittest.mock import patch
import httpx
from app.services.match_groq import _canonical_evidence_refs, GroqClient
from app.services.match_scoring import candidate_evidence_index


def test_languages_and_education_records_expand_only_to_existing_fields():
    profile={'languages':[{'name':'Español','level':'native'},{'name':'Inglés','level':'basic'}],
             'education':[{'level':'secondary','field':'Secundario','institution':'Instituto','completed':True}]}
    index=candidate_evidence_index(profile)
    refs=_canonical_evidence_refs(['/languages/1','/candidate_profile/education/0'],index)
    assert refs==['/languages/1/name','/languages/1/level','/education/0/level','/education/0/field','/education/0/institution']
    assert all(ref in index for ref in refs)


def test_wrong_indices_and_broad_groups_are_not_repaired_by_guessing():
    index={'/languages/0/name':'Inglés'}
    for ref in ['/languages/99','/languages','/preferences','/education/0','/skills/0/name/wrong']:
        assert _canonical_evidence_refs([ref],index)==[ref]


def test_large_record_is_not_silently_truncated():
    index={f'/skills/0/evidence/{i}':str(i) for i in range(9)}
    assert _canonical_evidence_refs(['/skills/0'],index)==['/skills/0']


def test_real_provider_record_spelling_is_validated_without_another_llm_call():
    profile={'languages':[{'name':'Español','level':'native'},{'name':'Inglés','level':'basic'}]}
    state={'candidate_profile':profile,'candidate_evidence_index':candidate_evidence_index(profile),
           'job_offer':{'requirements':[{'id':'language'}]}}
    response=httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'assessments':[
        {'requirement_id':'language','status':'met','explanation':'Idioma declarado.','evidence_refs':['/languages/1']}
    ]})}}]})
    with patch.object(GroqClient,'_require_key'),patch.object(GroqClient,'_model',return_value='synthetic-model'),patch.object(GroqClient,'_post',return_value=response) as post:
        result=GroqClient().evaluate_v2(state)
    assert result['assessments'][0]['evidence_refs']==['/languages/1/name','/languages/1/level']
    assert post.call_count==1
