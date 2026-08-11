"""La herramienta de consulta del historial de pacientes.

El prompt de una llamada ya lleva la ficha del número cuando existe (ver
`telefonia_llamada._prompt_de_llamada`), pero eso es solo la última llamada.
Esta herramienta deja que el modelo pida más cuando lo necesita: "¿qué le
dijimos la vez anterior?", "¿cuántas veces ha llamado esta semana?".

Como en todo el paquete, el docstring de la herramienta es el esquema que ve
el modelo: instrucciones, no documentación.
"""

from __future__ import annotations

from loguru import logger
from pipecat.services.llm_service import FunctionCallParams

from voice_agent.resources import AppResources


async def historial_paciente(params: FunctionCallParams, numero: str = "") -> None:
    """Consulta las llamadas anteriores registradas de un paciente por su número.

    Úsala cuando necesites saber si quien habla contigo ya llamó antes y qué
    pasó en esas llamadas: qué síntomas reportó, qué triaje se decidió y qué
    quedó pendiente. Sin argumentos consulta el número de la llamada en curso,
    que es lo habitual.

    Args:
        numero: Un número de teléfono concreto a consultar. Déjalo vacío para
            usar el de la llamada en curso.
    """
    recursos: AppResources = params.app_resources
    objetivo = numero.strip() or recursos.numero_llamada
    if recursos.historial is None or not objetivo:
        logger.info("[herramienta] historial_paciente sin historial o sin número")
        await params.result_callback(
            {
                "historial": "ninguno",
                "motivo": "Esta conversación no tiene un número de teléfono identificado.",
            }
        )
        return

    llamadas = recursos.historial.llamadas(objetivo, limite=10)
    # La llamada en curso también está registrada y todavía no cuenta como
    # "anterior": se aparta para no contársela al modelo como historial.
    id_actual = recursos.traza.id_llamada if recursos.traza else None
    anteriores = [ll for ll in llamadas if ll.id_llamada != id_actual]
    logger.info(f"[herramienta] historial_paciente({objetivo}) -> {len(anteriores)} anteriores")
    if not anteriores:
        await params.result_callback(
            {"historial": "ninguno", "motivo": f"El {objetivo} no tiene llamadas anteriores."}
        )
        return

    ficha = recursos.historial.ficha(objetivo)
    await params.result_callback(
        {
            "numero": objetivo,
            "nombre_conocido": ficha.nombre if ficha else "",
            "total_llamadas_anteriores": len(anteriores),
            "llamadas": [
                {
                    "fecha": ll.momento,
                    "tipo": "llamada que hicimos nosotros"
                    if ll.direccion == "mision"
                    else "nos llamó",
                    "triaje": ll.nivel or "sin registrar",
                    "paciente_y_procedimiento": ll.paciente_y_procedimiento,
                    "decision": ll.decision,
                    "proximos_pasos": ll.proximos_pasos,
                }
                for ll in anteriores
            ],
        }
    )


__all__ = ["historial_paciente"]
