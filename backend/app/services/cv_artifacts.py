"""Almacenamiento privado de entrada CV con nombres internos opacos."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from app.core.config import settings


class CVArtifactError(RuntimeError):
    pass


def scanner_available() -> bool:
    return bool(settings.CV_SCAN_COMMAND and Path(settings.CV_SCAN_COMMAND).is_file())


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


def scan_artifact(path: Path) -> None:
    """Si se habilita, el escáner es obligatorio y cualquier problema falla cerrado."""
    if not settings.CV_SCAN_ENABLED:
        return
    executable = Path(settings.CV_SCAN_COMMAND) if scanner_available() else None
    if executable is None:
        raise CVArtifactError("El escaneo de seguridad está habilitado, pero no hay un escáner disponible.")
    try:
        result = subprocess.run(
            [str(executable), "-Scan", "-ScanType", "3", "-File", str(path), "-DisableRemediation"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=settings.CV_SCAN_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CVArtifactError("No se pudo completar el escaneo de seguridad del archivo.") from exc
    if result.returncode != 0:
        raise CVArtifactError("El escaneo de seguridad no pudo confirmar que el archivo puede procesarse.")


def delete_artifact(artifact_id: str, extension: str) -> None:
    try:
        artifact_path(artifact_id, extension).unlink(missing_ok=True)
    except OSError:
        # La limpieza se reintenta con el trabajo de mantenimiento operativo.
        pass
