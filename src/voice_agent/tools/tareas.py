"""Herramienta para guardar lo que responde la gente en las misiones.

Cuando una tarea programada es un cuestionario ("pregúntale a Nora cómo
durmió y si le duele algo"), las respuestas no pueden quedarse en el
historial de la conversación: el panel tiene que poder enseñarlas después.
El agente no puede escribir en la base de datos del panel —vive en otra
imagen y no importa Django—, así que hace lo mismo que con
`estado_arranque.json`: deja un fichero en el volumen compartido y el panel
lo lee.

Cada ejecución guarda su propio fichero en
`<DATA_DIR>/tareas/resultados/<id_tarea>/<fecha-hora>.json`. Uno por
ejecución y no un JSONL con append: la escritura atómica de `rutas.py` ya
resuelve las lecturas a medias, y el panel lista la carpeta sin más.
"""

from __future__ import annotations

from datetime import datetime

from loguru import logger
from pipecat.services.llm_service import FunctionCallParams

from voice_agent_core.rutas import dir_resultados_tareas, escribir_json_atomico
from voice_agent_core.tareas import PATRON_ID


async def guardar_respuestas(
    params: FunctionCallParams, id_tarea: str, respuestas: str, resumen: str = ""
) -> None:
    """Guarda las respuestas de un cuestionario de una misión programada.

    Úsala SOLO cuando una misión programada te haya dado un identificador de
    tarea y te pida guardar respuestas; nunca por iniciativa propia en una
    conversación normal. Llámala una sola vez, cuando ya tengas todas las
    respuestas o la conversación esté a punto de terminar.

    Args:
        id_tarea: El identificador exacto que te dio la misión, sin cambiarlo.
        respuestas: Lo respondido, redactado con claridad y en frases
            completas, pregunta por pregunta.
        resumen: Una sola frase con lo esencial, para la lista del panel.
    """
    id_limpio = id_tarea.strip().lower()
    if not PATRON_ID.fullmatch(id_limpio):
        # El id acaba siendo carpeta: uno inventado o mal copiado no puede
        # convertirse en una ruta rara.
        logger.warning(f"[herramienta] guardar_respuestas con id inválido: {id_tarea!r}")
        await params.result_callback(
            {
                "error": f"El id de tarea '{id_tarea}' no es válido.",
                "sugerencia": "Usa exactamente el identificador que te dio la misión.",
            }
        )
        return

    momento = datetime.now()
    carpeta = dir_resultados_tareas(params.app_resources.settings.data_dir) / id_limpio
    ruta = carpeta / f"{momento:%Y%m%d-%H%M%S}.json"
    contador = 1
    while ruta.exists():
        # Dos llamadas en el mismo segundo son raras, pero pisar la primera
        # sería perder respuestas en silencio.
        ruta = carpeta / f"{momento:%Y%m%d-%H%M%S}-{contador}.json"
        contador += 1

    datos = {
        "id_tarea": id_limpio,
        "momento": momento.isoformat(timespec="seconds"),
        "resumen": resumen.strip(),
        "respuestas": respuestas.strip(),
    }
    try:
        escribir_json_atomico(ruta, datos)
    except OSError as e:
        logger.error(f"[herramienta] guardar_respuestas no pudo escribir {ruta}: {e}")
        await params.result_callback(
            {
                "error": "No se pudieron guardar las respuestas en el disco.",
                "sugerencia": "Díselo a quien te habla y repite lo esencial en voz alta.",
            }
        )
        return

    logger.info(f"[herramienta] guardar_respuestas({id_limpio}) -> {ruta.name}")
    await params.result_callback({"guardado": True, "fichero": ruta.name})
