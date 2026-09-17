"""Extracción de texto de PDFs dentro del worker de análisis."""

from io import BytesIO

import pdfplumber


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Devuelve el texto de un PDF o falla con un mensaje apto para el usuario."""

    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            text = "\n\n".join(page.extract_text() or "" for page in pdf.pages).strip()
    except Exception as exc:
        raise ValueError("El archivo recibido no es un PDF válido o no se pudo leer.") from exc

    if not text:
        raise ValueError("No se pudo extraer texto del PDF. Puede ser un PDF escaneado.")
    return text
