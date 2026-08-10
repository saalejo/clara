"""Herramienta de consulta a la base de conocimiento (el RAG).

Es la herramienta más importante del agente: convierte el corpus de `corpus/`
en algo que el modelo puede consultar bajo demanda, en vez de tener que llevarlo
entero en el prompt del sistema.
"""

from __future__ import annotations

from loguru import logger
from pipecat.services.llm_service import FunctionCallParams

from voice_agent.resources import AppResources


async def buscar_en_documentos(params: FunctionCallParams, consulta: str) -> None:
    """Busca información en la base de conocimiento del agente.

    Úsala siempre que te pregunten por algo que pueda estar documentado: la
    placa NanoPi, cómo funciona este agente, su configuración, su rendimiento,
    o cualquier tema del que existan documentos indexados. No respondas de
    memoria sobre esos temas: consulta primero.

    Args:
        consulta: La pregunta o los términos a buscar, en español y con
            palabras completas. Reformula lo que te han preguntado de forma
            autónoma y comprensible por sí misma, sin pronombres que dependan
            del turno anterior.
    """
    # NOTA IMPORTANTE PARA QUIEN LEA ESTO
    # -----------------------------------
    # El docstring de arriba no es solo documentación: Pipecat lo analiza para
    # construir el esquema JSON de la herramienta que se le manda al modelo. La
    # descripción sale del cuerpo del docstring y la de cada argumento, de su
    # entrada en la sección `Args`. Los tipos salen de las anotaciones de la
    # firma. Por eso está redactado como instrucciones dirigidas al modelo y no
    # como notas para el programador: cambiarlo cambia el comportamiento del
    # agente.
    recursos: AppResources = params.app_resources

    logger.info(f"[herramienta] buscar_en_documentos('{consulta}')")
    contexto = recursos.retriever.buscar_como_texto(consulta)

    await params.result_callback({"resultados": contexto})
