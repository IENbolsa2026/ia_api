"""Keep unit tests independent of local provider credentials and live tuning."""

import pytest

from app.core.config import settings


@pytest.fixture(autouse=True)
def isolated_provider_defaults(monkeypatch):
    for name, value in {
        'LLM_PROVIDER': 'groq', 'GROQ_API_KEY': '', 'GEMINI_API_KEY': '', 'OPENAI_API_KEY': '',
        'CV_EXTRACTION_MAX_TOKENS': 950, 'CV_PACKET_CHARACTERS': 500, 'CV_BLOQUE_CARACTERES': 4000,
    }.items():
        monkeypatch.setattr(settings, name, value)
