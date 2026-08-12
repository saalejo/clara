"""Registro de las herramientas que el agente puede usar.

Todas se declaran como *direct functions* de Pipecat: funciones asíncronas cuyo
primer parámetro es `params: FunctionCallParams`, seguido de los argumentos que
el modelo debe rellenar. Pipecat deduce el esquema JSON que se le manda al
modelo a partir de la **firma tipada** y del **docstring**, así que no hay que
escribir el esquema a mano ni mantenerlo sincronizado con el código.

Para añadir una herramienta nueva basta con crear la función en un módulo de
este paquete e incluirla en `HERRAMIENTAS`. Ver `docs/herramientas.md`.
"""

from __future__ import annotations

from collections.abc import Collection

from loguru import logger
from pipecat.adapters.schemas.direct_function import DirectFunction, DirectFunctionWrapper
from pipecat.adapters.schemas.function_schema import FunctionSchema

from voice_agent.tools.clock import obtener_fecha_hora
from voice_agent.tools.evaluacion import finalizar_llamada, registrar_alerta
from voice_agent.tools.historial import historial_paciente
from voice_agent.tools.knowledge import buscar_en_documentos
from voice_agent.tools.misiones import HERRAMIENTAS_AGENDA
from voice_agent.tools.system import estado_del_sistema
from voice_agent.tools.tareas import guardar_respuestas
from voice_agent.tools.telefono import HERRAMIENTAS_TELEFONIA

# El tipo es el que espera `LLMContext(tools=...)`. Declararlo como
# `list[DirectFunction]` a secas no vale: las listas son invariantes en Python,
# así que mypy rechaza pasarla donde se espera la unión.
HERRAMIENTAS: list[FunctionSchema | DirectFunction] = [
    buscar_en_documentos,
    registrar_alerta,
    finalizar_llamada,
    obtener_fecha_hora,
    estado_del_sistema,
    guardar_respuestas,
    historial_paciente,
]

# Las de teléfono viven en `voice_agent.tools.telefono` y NO se añaden a la
# lista de arriba. Es deliberado: solo sirven si hay un puente de telefonía
# corriendo en la placa, así que se mezclan en `herramientas_activas` cuando
# `bot.py` confirma que lo hay. El efecto secundario importante es que, sin
# puente, el catálogo que ve el modelo es exactamente el de siempre.
#
# Las de agenda (`voice_agent.tools.misiones`) van igual, pero con su PROPIA
# bandera y no con la de telefonía. No es una duplicación gratuita: dentro de
# una llamada las de teléfono se montan a `False` a propósito, y programar una
# llamada para más tarde es justo lo que hace falta poder hacer ahí.


def nombre_de(herramienta: FunctionSchema | DirectFunction) -> str:
    """Devuelve el nombre con el que el modelo ve una herramienta.

    Para las *direct functions* es el nombre de la función, que es exactamente
    lo que Pipecat pone en el esquema; `tests/test_tools.py` lo comprueba.
    """
    if isinstance(herramienta, FunctionSchema):
        return herramienta.name
    return herramienta.__name__


def esquema_de(herramienta: FunctionSchema | DirectFunction) -> dict[str, object]:
    """Devuelve el esquema JSON tal y como se le manda al modelo.

    Lo usa el agente para publicárselo al panel, que no puede calcularlo por su
    cuenta: vive en otra imagen y no tiene Pipecat instalado.
    """
    if isinstance(herramienta, FunctionSchema):
        return dict(herramienta.to_default_dict())
    return dict(DirectFunctionWrapper(herramienta).to_function_schema().to_default_dict())


def herramientas_activas(
    desactivadas: Collection[str],
    *,
    incluir_telefonia: bool = False,
    incluir_agenda: bool = False,
) -> list[FunctionSchema | DirectFunction]:
    """Filtra el registro dejando fuera las herramientas que el panel apagó.

    Desactivar una herramienta es la forma que tiene el panel de "bloquearla":
    se hace aquí, antes de construir el contexto, de modo que el modelo no llega
    a saber que existe. Vetarla más tarde, cuando ya ha decidido llamarla, sería
    tarde y además confuso para él.

    Args:
        desactivadas: Nombres a excluir. Los que no correspondan a ninguna
            herramienta se ignoran sin ruido: pueden ser de una versión anterior
            o de un servidor MCP que ya no está.
        incluir_telefonia: Añadir las herramientas de teléfono. Va aparte y por
            defecto en `False` porque solo tienen sentido si hay un puente de
            telefonía en marcha; `bot.py` lo decide sondeándolo al arrancar. El
            filtro del panel se aplica igual a las dos listas, así que una
            herramienta de teléfono se puede apagar como cualquier otra.
        incluir_agenda: Añadir las herramientas de misiones programadas. Su
            propia bandera, porque hacen falta **dentro** de una llamada, donde
            `incluir_telefonia` va en `False`. Solo la encienden la sala y el
            pipeline de la llamada: en el del navegador se queda apagada a
            propósito, para que quien tenga el enlace no pueda agendar llamadas
            salientes a números arbitrarios desde la placa (`docs/seguridad.md`).

    Returns:
        Las herramientas que se le ofrecerán al modelo.
    """
    catalogo: list[FunctionSchema | DirectFunction] = list(HERRAMIENTAS)
    if incluir_telefonia:
        catalogo += HERRAMIENTAS_TELEFONIA
    if incluir_agenda:
        catalogo += HERRAMIENTAS_AGENDA

    if not desactivadas:
        return catalogo

    activas = [h for h in catalogo if nombre_de(h) not in desactivadas]
    apagadas = sorted({nombre_de(h) for h in catalogo} & set(desactivadas))
    if apagadas:
        logger.info(f"Herramientas desactivadas desde el panel: {', '.join(apagadas)}")
    if not activas:
        # No es un error: un agente sin herramientas sigue conversando. Pero es
        # lo bastante raro como para que convenga verlo en el log antes de
        # ponerse a investigar por qué no consulta nada.
        logger.warning("No queda ninguna herramienta activa; el modelo conversará sin ellas.")
    return activas


__all__ = [
    "HERRAMIENTAS",
    "HERRAMIENTAS_AGENDA",
    "HERRAMIENTAS_TELEFONIA",
    "buscar_en_documentos",
    "esquema_de",
    "estado_del_sistema",
    "finalizar_llamada",
    "guardar_respuestas",
    "herramientas_activas",
    "historial_paciente",
    "nombre_de",
    "obtener_fecha_hora",
    "registrar_alerta",
]
