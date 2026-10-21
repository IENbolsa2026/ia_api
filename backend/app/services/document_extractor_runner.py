"""Ejecuta el parser en un proceso separado con límites de tiempo y memoria."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from app.core.config import settings
from app.services.document_text import DocumentRejected


def extract_in_subprocess(path: Path, document_format: str) -> str:
    command = [sys.executable, "-m", "app.services.document_worker", str(path), document_format]
    safe_system_keys = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PYTHONPATH"}
    child_env = {key: value for key, value in os.environ.items() if key.upper() in safe_system_keys}
    child_env.update({
        "CV_EXTRACTOR_PROCESS": "1",
        "CV_MAX_TEXT_CHARACTERS": str(settings.CV_MAX_TEXT_CHARACTERS),
        "CV_MAX_PDF_PAGES": str(settings.CV_MAX_PDF_PAGES),
        "CV_MAX_ARCHIVE_BYTES": str(settings.CV_MAX_ARCHIVE_BYTES),
        "CV_MAX_ARCHIVE_ENTRIES": str(settings.CV_MAX_ARCHIVE_ENTRIES),
        "CV_EXTRACT_TIMEOUT_SECONDS": str(settings.CV_EXTRACT_TIMEOUT_SECONDS),
        "CV_LEGACY_CONVERTER_IMAGE": settings.CV_LEGACY_CONVERTER_IMAGE,
        "CV_DOCKER_COMMAND": settings.CV_DOCKER_COMMAND,
        "CV_LEGACY_CONVERSION_TIMEOUT_SECONDS": str(settings.CV_LEGACY_CONVERSION_TIMEOUT_SECONDS),
        "JWT_SECRET_KEY": "",
        "SERVICE_USER": "",
        "SERVICE_PASSWORD": "",
    })
    process = None
    try:
        process = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[2],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            env=child_env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        stdout, _ = process.communicate(timeout=settings.CV_EXTRACT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        if process and process.poll() is None:
            process.kill()
        if process:
            process.communicate()
        raise DocumentRejected("La extracción superó el tiempo máximo permitido.") from exc
    except OSError as exc:
        if process and process.poll() is None:
            process.kill()
            process.communicate()
        raise DocumentRejected("No se pudo iniciar el servicio de extracción del CV.") from exc
    try:
        if len(stdout) > settings.CV_MAX_TEXT_CHARACTERS * 4 + 4096:
            raise DocumentRejected("El servicio de extracción devolvió más texto del permitido.")
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DocumentRejected("El servicio de extracción devolvió una respuesta inválida.") from exc
    if process.returncode != 0 or "text" not in payload:
        raise DocumentRejected(payload.get("error", "No se pudo extraer el texto del documento."))
    if not isinstance(payload["text"], str) or len(payload["text"]) > settings.CV_MAX_TEXT_CHARACTERS:
        raise DocumentRejected("El servicio de extracción devolvió más texto del permitido.")
    return payload["text"]
