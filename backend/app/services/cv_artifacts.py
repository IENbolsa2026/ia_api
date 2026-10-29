"""Almacenamiento privado de entrada CV con nombres internos opacos."""

from __future__ import annotations

import os
from pathlib import Path

from app.core.config import settings


def artifact_directory() -> Path:
    configured = Path(settings.CV_ARTIFACT_DIR)
    directory = configured if configured.is_absolute() else Path(__file__).resolve().parents[2] / configured
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return directory


def artifact_path(artifact_id: str, extension: str) -> Path:
    if not artifact_id.isascii() or not artifact_id.isalnum() or extension not in {"pdf", "docx", "odt", "txt", "doc", "rtf"}:
        raise ValueError("Identificador interno de archivo inválido.")
    return artifact_directory() / f"{artifact_id}.{extension}"


def delete_artifact(artifact_id: str, extension: str) -> None:
    try:
        artifact_path(artifact_id, extension).unlink(missing_ok=True)
    except OSError:
        # La limpieza se reintenta con el trabajo de mantenimiento operativo.
        pass
