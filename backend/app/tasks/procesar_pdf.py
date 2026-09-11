import asyncio
import logging

from app.core.connection_manager import manager
from app.schemas.analisis import EstadoTarea, WebSocketMessage
from app.services import django_client, groq_service, pdf_service

logger = logging.getLogger(__name__)


async def procesar_pdf(job_id: str, filename: str, pdf_bytes: bytes) -> None:
    """Secuencia real: extraer → analizar → avisar a Django.

    Es la única pieza que conoce las etapas; la llaman los routers.
    """
    if manager.get_job(job_id) is None:
        logger.warning("Job %s no existe; se aborta la tarea.", job_id)
        return

    print(f"\n[FLUJO] === TAREA procesar_pdf({job_id!r}) arranca en background ===")

    async def publicar(
        *,
        tipo: str = "progreso",
        estado: str,
        progreso: int,
        mensaje: str | None = None,
        resultado: dict | None = None,
        error: str | None = None,
    ) -> None:
        payload = WebSocketMessage(
            tipo=tipo,
            job_id=job_id,
            estado=estado,
            progreso=progreso,
            mensaje=mensaje,
            resultado=resultado,
            error=error,
        ).model_dump(mode="json")
        await manager.publicar(job_id, payload)

    try:
        await publicar(
            estado=EstadoTarea.procesando.value,
            progreso=5,
            mensaje="Leyendo el PDF…",
        )

        texto = await asyncio.to_thread(pdf_service.extraer_texto, pdf_bytes)
        texto = pdf_service.resumir_texto(texto)
        print(f"[FLUJO] etapa 1/3: texto extraído y recortado → {len(texto)} caracteres")

        await publicar(
            estado=EstadoTarea.procesando.value,
            progreso=40,
            mensaje="Texto extraído; consultando a Groq…",
        )

        resultado = await asyncio.to_thread(groq_service.analizar_texto, texto)
        print(f"[FLUJO] etapa 2/3: Groq respondió OK → resumen: {resultado.resumen[:70]!r}")
        resultado_json = resultado.model_dump(mode="json")

        await publicar(
            estado=EstadoTarea.procesando.value,
            progreso=90,
            mensaje="Análisis listo; avisando a Django…",
        )

        await asyncio.to_thread(
            django_client.notificar_resultado,
            job_id=job_id,
            filename=filename,
            resultado=resultado_json,
        )
        print("[FLUJO] etapa 3/3: resultado enviado a Django (o aviso omitido sin URL)")

        await publicar(
            tipo="resultado",
            estado=EstadoTarea.completado.value,
            progreso=100,
            resultado=resultado_json,
            mensaje="Análisis completado.",
        )
        print("[FLUJO] === TAREA terminada: COMPLETADO publicado por websocket ===\n")

    except (pdf_service.PdfError, groq_service.GroqError, django_client.DjangoClientError) as exc:
        logger.exception("Fallo el procesamiento del job %s", job_id)
        print(f"[FLUJO] === TAREA con ERROR: {exc} ===\n")
        try:
            await asyncio.to_thread(
                django_client.notificar_error,
                job_id=job_id,
                filename=filename,
                error=str(exc),
            )
        except django_client.DjangoClientError:
            logger.exception("Además, no se pudo notificar el error a Django.")
        await publicar(
            tipo="error",
            estado=EstadoTarea.error.value,
            progreso=100,
            error=str(exc),
            mensaje=str(exc),
        )