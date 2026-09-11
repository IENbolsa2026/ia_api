import logging
import re

from groq import Groq

from app.config import settings
from app.schemas.analisis import AnalisisResultado

logger = logging.getLogger(__name__)


class GroqError(Exception):
    """Error de dominio al analizar texto con Groq. No conoce nada de HTTP."""


PROMPT_SISTEMA = (
    "Sos un asistente experto en análisis de documentos PDF en español.\n"
    "Recibís el texto extraído de un documento y producís un análisis estructurado.\n"
    "Devolvés ÚNICAMENTE un JSON válido, sin texto adicional, sin markdown, con este esquema exacto:\n"
    "{\n"
    '  "resumen": "resumen ejecutivo en 3-5 oraciones",\n'
    '  "tipo_documento": "categoría genérica del documento",\n'
    '  "keywords": ["palabra1", "palabra2"],\n'
    '  "temas_principales": ["tema1", "tema2"],\n'
    '  "puntos_clave": ["punto1", "punto2"],\n'
    '  "datos_extraidos": {"campo": "valor"}\n'
    "}\n"
    "Reglas:\n"
    "- Todos los valores de texto van en español.\n"
    "- `datos_extraidos` lleva los datos salientes del documento (fechas, montos, nombres, números). "
    "Si no hay datos estructurados, va un objeto vacío {}.\n"
    "- Si el texto está truncado, seguí igual con lo que haya.\n"
    "- No inventes datos que no estén en el documento."
)


def _cliente(*, api_key: str | None = None) -> Groq:
    key = api_key or settings.GROQ_API_KEY
    if not key or key == "poner-aqui-tu-nueva-key":
        raise GroqError("GROQ_API_KEY no está configurada en el .env")
    return Groq(api_key=key, timeout=settings.GROQ_TIMEOUT)


def _extraer_json(contenido: str) -> str:
    """Limpia el JSON de la respuesta: quita fences de markdown y texto suelto."""
    texto = contenido.strip()
    texto = re.sub(r"^```(?:json)?\s*", "", texto, flags=re.IGNORECASE)
    texto = re.sub(r"\s*```$", "", texto)
    inicio = texto.find("{")
    fin = texto.rfind("}")
    if inicio == -1 or fin == -1 or fin < inicio:
        raise GroqError("Groq no devolvió un JSON válido.")
    return texto[inicio : fin + 1]


def _parsear(contenido: str, attempt: int = 1) -> AnalisisResultado:
    try:
        return AnalisisResultado.model_validate_json(_extraer_json(contenido))
    except Exception as exc:  # pydantic ValidationError o JSON malformado
        if attempt >= 2:
            raise GroqError(f"El JSON devuelto por Groq no cumple el esquema: {exc}") from exc
        # Un intento: mandar solo el fragmento que Groq no pudo estructurar,
        # pidiendo explícitamente que devuelva el JSON corregido.
        sugerencia = (
            PROMPT_SISTEMA
            + "\nTu respuesta anterior no era un JSON válido con el esquema pedido. "
            + "Corregila y devolvé SOLO el JSON.\n\n"
            + contenido[:4000]
        )
        return _parsear(_llamar(sugerencia), attempt=attempt + 1)


def _llamar(mensaje: str, *, api_key: str | None = None, model: str | None = None) -> str:
    client = _cliente(api_key=api_key)
    modelo = model or settings.GROQ_MODEL
    respuesta = client.chat.completions.create(
        model=modelo,
        temperature=0.2,
        max_tokens=settings.GROQ_MAX_TOKENS,
        messages=[
            {"role": "system", "content": PROMPT_SISTEMA},
            {"role": "user", "content": mensaje},
        ],
        response_format={"type": "json_object"},
    )
    contenido = respuesta.choices[0].message.content
    if not contenido:
        raise GroqError("Groq devolvió una respuesta vacía.")
    print(f"[FLUJO] groq_service._llamar: respuesta cruda de {len(contenido)} chars")
    return contenido


def analizar_texto(
    texto: str, *, api_key: str | None = None, model: str | None = None
) -> AnalisisResultado:
    """Analiza el texto extraído de un PDF y devuelve el AnalisisResultado validado."""
    if not texto.strip():
        raise GroqError("No hay texto para analizar.")
    modelo = model or settings.GROQ_MODEL
    print(f"[FLUJO] groq_service.analizar_texto: {len(texto)} chars → modelo {modelo}")
    return _parsear(_llamar(texto, api_key=api_key, model=model))


def analizar_prompt(
    prompt: str, *, api_key: str | None = None, model: str | None = None
) -> str:
    """Helper suelto para probar la skill con texto pegado a mano."""
    return _llamar(prompt, api_key=api_key, model=model)