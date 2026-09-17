import asyncio
import json

from groq import Groq

from app.core.config import settings


client = Groq(api_key=settings.GROQ_API_KEY)


SYSTEM_PROMPT = """
Sos un experto en evaluación de perfiles técnicos (CVs).
Tu tarea: dado un fragmento de un CV, extraer SOLO las tecnologías/habilidades técnicas mencionadas
con su nivel estimado y evidencia textual breve.
Respondé EXCLUSIVAMENTE en JSON válido con este esquema:
{
  "habilidades": [
    {"tecnologia": "Python", "nivel": "Intermedio", "evidencia": "3 años en backend"},
    {"tecnologia": "FastAPI", "nivel": "Principiante", "evidencia": "Proyecto SysTUP"}
  ]
}
No agregues texto extra, ni markdown, ni explicaciones.
"""


CONSOLIDATION_PROMPT = """
Recibiste múltiples extracciones de habilidades de distintos bloques de un mismo CV.
Tu tarea: CONSOLIDAR en una evaluación final única, eliminando duplicados, subiendo nivel si hay más evidencia,
y generando un resumen ejecutivo.
Respondé EXCLUSIVAMENTE en JSON válido con este esquema:
{
  "resumen_ejecutivo": "Perfil backend con foco en Python y APIs...",
  "habilidades_analizadas": [
    {"tecnologia": "Python", "nivel_evaluado": "Intermedio", "justificacion_evidencia": "Proyecto SysTUP y beca activa de investigación (3 años)."},
    {"tecnologia": "FastAPI", "nivel_evaluado": "Principiante", "justificacion_evidencia": "Proyecto SysTUP (6 meses)."}
  ]
}
No agregues texto extra.
"""


def split_text_into_blocks(text: str, max_chars: int) -> list[str]:
    """Divide el texto en bloques compatibles con el limite de contexto del modelo.

    Si encuentra un salto de linea dentro del limite, corta alli para no partir
    una frase o seccion del CV innecesariamente.
    """
    if len(text) <= max_chars:
        return [text]
    blocks = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            newline = text.rfind("\n", start, end)
            if newline != -1 and newline > start:
                end = newline + 1
        blocks.append(text[start:end].strip())
        start = end
    return blocks


def call_groq(messages: list[dict], max_tokens: int) -> dict:
    """Llamada bloqueante a Groq que se ejecuta en un thread separado.

    El SDK de Groq es sincronico. Por eso las funciones async no lo llaman
    directo: lo delegan a un executor y asi no bloquean su event loop.
    """
    resp = client.chat.completions.create(
        model=settings.CV_MODEL,
        messages=messages,
        max_tokens=max_tokens,
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    content = resp.choices[0].message.content
    return json.loads(content)


async def extract_block(block: str, block_num: int, total: int) -> dict:
    """Extrae habilidades de un bloque sin bloquear el event loop async."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"--- BLOQUE {block_num} de {total} ---\n{block}"},
    ]
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, call_groq, messages, settings.GROQ_MAX_TOKENS
    )
    await asyncio.sleep(settings.GROQ_PAUSA_SEGUNDOS)
    return result


async def consolidate_extractions(extractions: list[dict]) -> dict:
    """Pide al modelo una respuesta final a partir de los bloques extraidos."""
    combined = {"extracciones": extractions}
    messages = [
        {"role": "system", "content": CONSOLIDATION_PROMPT},
        {"role": "user", "content": json.dumps(combined, ensure_ascii=False)},
    ]
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, call_groq, messages, settings.GROQ_MAX_TOKENS
    )
    return result


async def process_cv_text(text: str) -> dict:
    """Pipeline completo: dividir, extraer cada bloque y consolidar el resultado.

    Esta funcion no conoce HTTP ni Celery. Esa separacion permite reutilizar la
    logica de IA desde una tarea, un test o una interfaz futura.
    """
    blocks = split_text_into_blocks(text, settings.CV_BLOQUE_CARACTERES)
    total = len(blocks)

    extractions = []
    for i, block in enumerate(blocks, 1):
        if not block:
            continue
        result = await extract_block(block, i, total)
        extractions.append(result)

    if not extractions:
        return {
            "resumen_ejecutivo": "CV vacío o sin contenido técnico detectable.",
            "habilidades_analizadas": [],
        }

    final = await consolidate_extractions(extractions)
    return final
