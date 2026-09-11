from io import BytesIO

from pypdf import PdfReader

from app.config import settings


class PdfError(Exception):
    """Error de dominio al leer/extraer un PDF. No conoce nada de HTTP."""


def extraer_texto(pdf_bytes: bytes, *, max_paginas: int | None = None) -> str:
    """Extrae el texto plano de un PDF a partir de sus bytes.

    Pura, sin IO de red ni FastAPI: podés probarla con un script suelto.
    """
    if not pdf_bytes:
        raise PdfError("El PDF viene vacío.")

    try:
        reader = PdfReader(BytesIO(pdf_bytes))
    except Exception as exc:  # pypdf lanza PdfStreamError/OSError variados
        raise PdfError(f"No se pudo abrir el PDF: {exc}") from exc
    print(f"[FLUJO] pdf_service.extraer_texto: pypdf abrió {len(reader.pages)} páginas")

    paginas = reader.pages if max_paginas is None else reader.pages[:max_paginas]
    fragmentos = []
    for indice, pagina in enumerate(paginas, start=1):
        texto = (pagina.extract_text() or "").strip()
        if texto:
            fragmentos.append(f"--- Página {indice} ---\n{texto}")

    if not fragmentos:
        raise PdfError("El PDF no tiene texto extraíble (¿está escaneado?).")

    return "\n\n".join(fragmentos)


def resumir_texto(texto: str, *, limite_caracteres: int | None = None) -> str:
    """Recorta el texto al límite de contexto del modelo, sin partir saltos de página."""
    limite = limite_caracteres or settings.PDF_MAX_CHARACTERES
    if len(texto) <= limite:
        print(f"[FLUJO] pdf_service.resumir_texto: {len(texto)} chars (dentro del límite {limite})")
        return texto
    recorte = texto[:limite]
    ultimo_salto = recorte.rfind("\n--- Página")
    corte = ultimo_salto if ultimo_salto > 0 else limite
    final = texto[:corte] + "\n\n[Texto truncado por superar el límite de contexto.]"
    print(f"[FLUJO] pdf_service.resumir_texto: {len(texto)} → {len(final)} chars (truncado a {corte})")
    return final