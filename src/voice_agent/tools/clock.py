"""Herramienta de fecha y hora.

Un modelo de lenguaje no sabe qué día es: su conocimiento se congeló al
entrenarlo y no tiene reloj. Si se le pregunta la hora, lo normal es que se
invente una o que responda con la fecha de su corte de entrenamiento. Esta
herramienta, trivial de implementar, elimina toda una clase de respuestas
falsas.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from loguru import logger
from pipecat.services.llm_service import FunctionCallParams

from voice_agent.resources import AppResources

DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
MESES = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)


async def obtener_fecha_hora(params: FunctionCallParams) -> None:
    """Consulta la fecha y la hora actuales.

    Úsala siempre que te pregunten qué día es, qué hora es, en qué mes o año
    estamos, o cuando necesites calcular algo relativo al momento presente,
    como cuántos días faltan para una fecha.
    """
    recursos: AppResources = params.app_resources
    ahora = datetime.now(ZoneInfo(recursos.settings.timezone))

    # Se devuelve tanto una versión ya redactada en español como los campos
    # sueltos. Lo primero evita que el modelo tenga que traducir "Monday" y se
    # equivoque; lo segundo le permite hacer cuentas si le hacen falta.
    legible = (
        f"{DIAS[ahora.weekday()]} {ahora.day} de {MESES[ahora.month - 1]} "
        f"de {ahora.year}, {ahora.hour:02d}:{ahora.minute:02d}"
    )
    logger.info(f"[herramienta] obtener_fecha_hora() -> {legible}")

    await params.result_callback(
        {
            "descripcion": legible,
            "fecha_iso": ahora.date().isoformat(),
            "hora_iso": ahora.strftime("%H:%M:%S"),
            "zona_horaria": recursos.settings.timezone,
        }
    )
