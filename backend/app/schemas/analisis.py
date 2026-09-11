from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EstadoTarea(str, Enum):
    pendiente = "PENDIENTE"
    procesando = "PROCESANDO"
    completado = "COMPLETADO"
    error = "ERROR"


class AnalisisResultado(BaseModel):
    """Contrato fijo que devuelve Groq. No cambiar sin migrar Django."""

    model_config = ConfigDict(extra="ignore")

    resumen: str = Field(description="Resumen ejecutivo del documento, en español.")
    tipo_documento: str = Field(
        description="Tipo/categoría del documento (p.ej. contrato, informe, factura)."
    )
    keywords: list[str] = Field(
        default_factory=list, description="Lista de palabras clave del documento."
    )
    temas_principales: list[str] = Field(
        default_factory=list, description="Temas principales tratados en el documento."
    )
    puntos_clave: list[str] = Field(
        default_factory=list, description="Puntos o conclusiones clave del documento."
    )
    datos_extraidos: dict[str, Any] = Field(
        default_factory=dict,
        description="Datos estructurados salientes (fechas, montos, nombres, etc.).",
    )
    fecha_analisis: datetime = Field(
        default_factory=datetime.now, description="Momento en que se generó el análisis."
    )


class ProcesarPdfResponse(BaseModel):
    """Respuesta inmediata del POST /procesar-pdf (202 Accepted)."""

    job_id: str
    estado: EstadoTarea
    mensaje: str = "El PDF fue recibido y se está procesando."


class WebSocketMessage(BaseModel):
    """Mensajes que viajan por el WS /ws/{job_id}."""

    tipo: str = Field(description="progreso | resultado | error | snapshot")
    job_id: str
    estado: EstadoTarea
    progreso: int = Field(default=0, ge=0, le=100)
    mensaje: str | None = None
    resultado: AnalisisResultado | None = None
    error: str | None = None


class DjangoWebhookPayload(BaseModel):
    """Cuerpo que se envía al webhook de Django en cada notificación."""

    job_id: str
    filename: str
    estado: EstadoTarea
    resultado: AnalisisResultado | None = None
    error: str | None = None
    timestamp: datetime = Field(default_factory=datetime.now)


class JobSnapshot(BaseModel):
    """Estado actual de una tarea (para el snapshot inicial del WS)."""

    job_id: str
    filename: str
    estado: EstadoTarea
    progreso: int = 0
    resultado: AnalisisResultado | None = None
    error: str | None = None