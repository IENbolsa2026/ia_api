"""Detección y extracción restringida de documentos de CV."""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from uuid import uuid4
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

import pdfplumber

from app.core.config import settings


class DocumentRejected(ValueError):
    """El archivo no cumple el formato o los límites seguros admitidos."""


BASE_FORMATS = ("pdf", "docx", "odt", "txt")


def legacy_converter_available() -> bool:
    docker = shutil.which(settings.CV_DOCKER_COMMAND)
    if not settings.CV_LEGACY_CONVERTER_IMAGE or not docker:
        return False
    try:
        result = subprocess.run(
            [docker, "image", "inspect", settings.CV_LEGACY_CONVERTER_IMAGE],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=3, check=False, shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def enabled_formats() -> tuple[str, ...]:
    return (*BASE_FORMATS, "doc", "rtf") if legacy_converter_available() else BASE_FORMATS


def detect_format(filename: str, data: bytes) -> str:
    """Compara extensión con firma y estructura inicial, sin confiar en MIME."""
    suffix = Path(filename or "").suffix.lower()
    if not data:
        raise DocumentRejected("El archivo está vacío.")
    if data.startswith(b"%PDF-"):
        detected = "pdf"
    elif data.startswith(b"PK\x03\x04"):
        detected = _detect_office_zip(data)
    elif data.startswith(b"{\\rtf"):
        detected = "rtf"
    elif data.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        detected = "doc"
    else:
        detected = _validate_text(data)
    if suffix != f".{detected}":
        raise DocumentRejected("La extensión del archivo no coincide con su contenido.")
    if detected in {"doc", "rtf"} and not legacy_converter_available():
        raise DocumentRejected(f"El formato {detected.upper()} requiere un conversor aislado configurado.")
    return detected


def _detect_office_zip(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = _validate_archive_metadata(archive)
            names = {info.filename for info in infos}
            if "word/document.xml" in names and "[Content_Types].xml" in names:
                return "docx"
            if "mimetype" in names:
                info = archive.getinfo("mimetype")
                if info.file_size <= 128 and archive.read(info) == b"application/vnd.oasis.opendocument.text":
                    return "odt"
    except (OSError, RuntimeError, zipfile.BadZipFile, KeyError, DocumentRejected):
        pass
    raise DocumentRejected("El archivo comprimido no es un DOCX u ODT válido.")


def _validate_text(data: bytes) -> str:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        try:
            text = data.decode("cp1252")
        except UnicodeDecodeError as fallback_exc:
            raise DocumentRejected("El archivo de texto debe usar UTF-8 o Windows-1252.") from fallback_exc
    if not text.strip() or "\x00" in text or any(ord(char) < 9 for char in text):
        raise DocumentRejected("El archivo de texto está vacío o contiene datos binarios.")
    return "txt"


def extract_document_text(data: bytes, document_format: str) -> str:
    if document_format == "pdf":
        text = _extract_pdf(data)
    elif document_format in {"docx", "odt"}:
        text = _extract_office(data, document_format)
    elif document_format == "txt":
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252")
    elif document_format in {"doc", "rtf"}:
        text = _convert_legacy_document(data, document_format)
    else:
        raise DocumentRejected("El formato de CV solicitado no está habilitado.")
    text = text.strip()
    if not text:
        raise DocumentRejected("No se pudo extraer texto. Si el PDF es escaneado, cargá una versión con texto seleccionable.")
    if len(text) > settings.CV_MAX_TEXT_CHARACTERS:
        raise DocumentRejected(f"El texto extraído supera el máximo de {settings.CV_MAX_TEXT_CHARACTERS:,} caracteres. Reducí el contenido y volvé a cargarlo.")
    return text


def _convert_legacy_document(data: bytes, document_format: str) -> str:
    if not legacy_converter_available():
        raise DocumentRejected(f"El formato {document_format.upper()} requiere un conversor aislado configurado.")
    try:
        with tempfile.TemporaryDirectory(prefix="cv-convert-") as temporary:
            root = Path(temporary)
            input_dir, output_dir = root / "input", root / "output"
            input_dir.mkdir()
            output_dir.mkdir()
            source = input_dir / f"document.{document_format}"
            source.write_bytes(data)
            docker = shutil.which(settings.CV_DOCKER_COMMAND)
            container_name = f"ien-cv-convert-{uuid4().hex}"
            command = [
                docker or settings.CV_DOCKER_COMMAND, "run", "--rm", "--name", container_name, "--network", "none",
                "--memory", "512m", "--cpus", "1", "--pids-limit", "64", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
                "-v", f"{input_dir.resolve()}:/input:ro",
                "-v", f"{output_dir.resolve()}:/output:rw",
                settings.CV_LEGACY_CONVERTER_IMAGE,
                "--headless", "-env:UserInstallation=file:///tmp/lo-profile",
                "--convert-to", "txt:Text", "--outdir", "/output", f"/input/document.{document_format}",
            ]
            try:
                result = subprocess.run(
                    command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=settings.CV_LEGACY_CONVERSION_TIMEOUT_SECONDS, check=False, shell=False,
                )
            finally:
                # --rm cubre la salida normal; esta limpieza también cubre timeout y
                # errores del cliente Docker sin usar nombres ni rutas del usuario.
                try:
                    subprocess.run(
                        [docker or settings.CV_DOCKER_COMMAND, "rm", "--force", container_name],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=5, check=False, shell=False,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
            converted = output_dir / "document.txt"
            if result.returncode != 0 or not converted.is_file():
                raise DocumentRejected("No se pudo convertir el documento. Guardalo como DOCX o PDF y volvé a cargarlo.")
            if converted.stat().st_size > settings.CV_MAX_TEXT_CHARACTERS * 4:
                raise DocumentRejected("El texto convertido supera el máximo permitido.")
            return converted.read_text(encoding="utf-8-sig", errors="strict")
    except subprocess.TimeoutExpired as exc:
        raise DocumentRejected("La conversión superó el tiempo máximo permitido.") from exc
    except (OSError, UnicodeError) as exc:
        raise DocumentRejected("No se pudo convertir el documento con el servicio configurado.") from exc


def _extract_pdf(data: bytes) -> str:
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if len(pdf.pages) > settings.CV_MAX_PDF_PAGES:
                raise DocumentRejected(f"El PDF supera el máximo de {settings.CV_MAX_PDF_PAGES} páginas.")
            pieces = [_extract_pdf_page(page) for page in pdf.pages]
    except DocumentRejected:
        raise
    except Exception as exc:
        raise DocumentRejected("El archivo no es un PDF válido o no se pudo leer.") from exc
    return "\n\n".join(pieces)


def _extract_pdf_page(page) -> str:
    """Read true side-by-side columns separately to preserve section context."""
    split = _pdf_column_break(page)
    if split is None:
        return page.extract_text() or ""
    x0, top, x1, bottom = page.bbox
    left = page.crop((x0, top, split, bottom)).extract_text() or ""
    right = page.crop((split, top, x1, bottom)).extract_text() or ""
    return "\n\n".join(part for part in (left, right) if part.strip())


def _pdf_column_break(page) -> float | None:
    """Find a central empty gutter shared by many simultaneous text lines.

    A single-column page has words crossing the middle on many lines. Requiring
    both a clear gutter and overlapping text on its sides avoids splitting it.
    """
    words = page.extract_words()
    if len(words) < 50:
        return None
    x0, _top, x1, _bottom = page.bbox
    width = x1 - x0
    rows: dict[int, list[dict]] = {}
    for word in words:
        rows.setdefault(round(word["top"] / 4), []).append(word)
    candidates = []
    for step in range(61):
        split = x0 + width * (0.38 + step * 0.004)
        crossing = sum(word["x0"] < split < word["x1"] for word in words)
        if crossing:
            continue
        left_count = sum(word["x1"] <= split for word in words)
        right_count = len(words) - left_count
        if min(left_count, right_count) < 25:
            continue
        simultaneous = 0
        for row in rows.values():
            left_edges = [word["x1"] for word in row if word["x1"] <= split]
            right_edges = [word["x0"] for word in row if word["x0"] >= split]
            if left_edges and right_edges and min(right_edges) - max(left_edges) >= 24:
                simultaneous += 1
        if simultaneous >= max(8, len(rows) // 5):
            candidates.append((simultaneous, -abs(split - (x0 + x1) / 2), split))
    return max(candidates)[2] if candidates else None


def _extract_office(data: bytes, document_format: str) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            _validate_archive_metadata(archive)
            if document_format == "docx":
                return _extract_docx(archive)
            return _extract_odt(archive)
    except DocumentRejected:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, KeyError, ElementTree.ParseError, DefusedXmlException) as exc:
        raise DocumentRejected("El documento está dañado o no tiene una estructura válida.") from exc


def _safe_xml(archive: zipfile.ZipFile, name: str) -> ElementTree.Element:
    info = archive.getinfo(name)
    max_xml_bytes = 10 * 1024 * 1024
    if info.file_size > max_xml_bytes:
        raise DocumentRejected("Una sección XML del documento supera 10 MiB.")
    with archive.open(info) as source:
        raw = source.read(max_xml_bytes + 1)
    if len(raw) > max_xml_bytes or re.search(br"<!\s*(DOCTYPE|ENTITY)", raw, re.IGNORECASE):
        raise DocumentRejected("El documento contiene XML excesivo o definiciones no permitidas.")
    return ElementTree.fromstring(raw)


def _extract_docx(archive: zipfile.ZipFile) -> str:
    names = set(archive.namelist())
    text: list[str] = []
    for name in names:
        if name.endswith(".rels"):
            xml = _safe_xml(archive, name)
            for node in xml:
                if node.attrib.get("TargetMode", "").lower() == "external":
                    relationship_type = node.attrib.get("Type", "").lower()
                    # Read packaged text only; never resolve hyperlink or attached
                    # template targets, including local files and network shares.
                    if not relationship_type.endswith(("/hyperlink", "/attachedtemplate")):
                        raise DocumentRejected("El documento contiene recursos externos no permitidos.")
    documents = [name for name in names if name == "word/document.xml" or re.fullmatch(r"word/(header|footer)\d+\.xml", name)]
    for document_name in sorted(documents):
        root = _safe_xml(archive, document_name)
        for node in root.iter():
            if node.tag.endswith("}t") and node.text:
                text.append(node.text)
            elif node.tag.endswith("}p"):
                text.append("\n")
            elif node.tag.endswith("}tab"):
                text.append("\t")
    return "".join(text)


def _extract_odt(archive: zipfile.ZipFile) -> str:
    names = set(archive.namelist())
    if any(name.casefold().startswith(("scripts/", "basic/")) for name in names):
        raise DocumentRejected("El documento contiene scripts no permitidos.")
    root = _safe_xml(archive, "content.xml")
    for node in root.iter():
        href = next((value for key, value in node.attrib.items() if key.endswith("}href")), None)
        if href and (href.startswith(("http://", "https://", "//", "file:")) or "://" in href):
            # External text links remain inert; remote media/objects are rejected.
            if not node.tag.endswith("}a"):
                raise DocumentRejected("El documento contiene imágenes u objetos externos no permitidos.")
        if node.tag.casefold().endswith(("}script", "}scriptdata")):
            raise DocumentRejected("El documento contiene scripts no permitidos.")
    text: list[str] = []
    def walk(node: ElementTree.Element) -> None:
        if node.text:
            text.append(node.text)
        for child in list(node):
            walk(child)
            if child.tail:
                text.append(child.tail)
            if child.tag.endswith("}p") or child.tag.endswith("}h"):
                text.append("\n")
    walk(root)
    return "".join(text)


def _validate_archive_metadata(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > settings.CV_MAX_ARCHIVE_ENTRIES:
        raise DocumentRejected("El documento contiene demasiados componentes internos.")
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise DocumentRejected("El documento contiene componentes internos duplicados.")
    total_size = 0
    for info in infos:
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or any(part == ".." for part in name.split("/")):
            raise DocumentRejected("El documento contiene una ruta interna no permitida.")
        if info.flag_bits & 0x1:
            raise DocumentRejected("No se admiten documentos cifrados o protegidos por contraseña.")
        total_size += info.file_size
        if info.file_size > 0 and (info.compress_size == 0 or info.file_size / info.compress_size > 100):
            raise DocumentRejected("El documento supera la relación de compresión permitida.")
        lowered = name.casefold()
        if any(token in lowered for token in ("vbaproject", "/embeddings/", "activex", "externallink", "macros/")):
            raise DocumentRejected("El documento contiene macros, objetos incrustados o referencias externas no permitidas.")
    if total_size > settings.CV_MAX_ARCHIVE_BYTES:
        raise DocumentRejected(f"El contenido descomprimido del documento supera {settings.CV_MAX_ARCHIVE_BYTES // (1024 * 1024)} MiB.")
    return infos
