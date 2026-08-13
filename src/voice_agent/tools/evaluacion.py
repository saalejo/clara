"""Las herramientas del seguimiento clínico: escalar y cerrar la llamada.

Son el corazón de la lógica de decisión del reto. `registrar_alerta` persiste
la decisión de triaje **en el momento en que se toma** —una llamada que se cae
después de detectar una bandera roja tiene que dejar la alerta ya en disco— y
devuelve al modelo la instrucción exacta de qué comunicarle al paciente.
`finalizar_llamada` persiste el resumen estructurado con los cinco campos que
exige la rúbrica, fusionando las referencias que cita el modelo con la traza
real del RAG.

Como todas las herramientas, los docstrings son el esquema que ve el modelo:
están redactados como instrucciones para él, no como documentación.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from loguru import logger
from pipecat.services.llm_service import FunctionCallParams

from voice_agent.resources import AppResources
from voice_agent_core.cobertura import Cobertura, cargar_alias, resolver_cirugia
from voice_agent_core.evaluaciones import Alerta, NivelAlerta, ResumenLlamada
from voice_agent_core.rutas import dir_alertas, dir_resumenes, escribir_json_atomico

#: Qué se le comunica al paciente según el nivel. Devolverlo desde la
#: herramienta —en vez de confiar en que el prompt lo recuerde— hace que la
#: instrucción llegue justo cuando el modelo va a hablar del siguiente paso.
_INDICACION_POR_NIVEL: dict[NivelAlerta, str] = {
    NivelAlerta.ROJO: (
        "Dile al paciente, con calma y claridad, que por lo que describe debe "
        "acudir a urgencias ahora mismo o llamar a su línea de emergencias, y "
        "que el equipo médico queda avisado con esta alerta. No des a entender "
        "que puede esperar."
    ),
    NivelAlerta.AMARILLO: (
        "Dile al paciente que el equipo médico queda avisado y se pondrá en "
        "contacto en las próximas veinticuatro horas, y qué debe vigilar "
        "mientras tanto; si algo empeora, que acuda a urgencias sin esperar la "
        "llamada."
    ),
    NivelAlerta.VERDE: (
        "Tranquiliza al paciente: lo que describe es parte de una evolución "
        "normal. Recuérdale los cuidados en casa y los signos de alarma ante "
        "los que debe llamar o acudir a urgencias."
    ),
}


def _cobertura_actual(recursos: AppResources) -> str:
    """En qué estado deja la puerta de cobertura la cirugía de esta llamada.

    Se recalcula contra los temas de ahora en vez de guardarse al buscar,
    porque los temas se releen vivos: si el corpus creció a mitad de llamada,
    lo que quede escrito en el registro tiene que ser lo que valía al cerrarlo.

    Sin cirugía conocida devuelve "desconocida", que es lo que de verdad pasó.
    """
    if not recursos.cirugia_paciente:
        return str(Cobertura.DESCONOCIDA)
    temas = recursos.retriever.temas_disponibles()
    alias = cargar_alias(recursos.settings.data_dir)
    return str(resolver_cirugia(recursos.cirugia_paciente, temas, alias).estado)


def _ruta_sin_pisar(carpeta: Path, momento: datetime) -> Path:
    """Devuelve una ruta con marca de tiempo que no pise un fichero existente."""
    ruta = carpeta / f"{momento:%Y%m%d-%H%M%S}.json"
    contador = 1
    while ruta.exists():
        ruta = carpeta / f"{momento:%Y%m%d-%H%M%S}-{contador}.json"
        contador += 1
    return ruta


async def registrar_alerta(
    params: FunctionCallParams, nivel: str, sintomas: str, justificacion: str
) -> None:
    """Registra la decisión de triaje de esta llamada y avisa al equipo si procede.

    Llámala EN CUANTO tengas información suficiente para clasificar la
    situación del paciente, sin esperar al final de la llamada. Los niveles
    son: "rojo" para signos de alarma que requieren atención urgente,
    "amarillo" para hallazgos que necesitan valoración médica en las próximas
    veinticuatro horas, y "verde" para una evolución normal. Ante la duda
    entre dos niveles, elige siempre el más grave. Si durante la conversación
    aparece información que cambia la clasificación, vuelve a llamarla con el
    nivel nuevo.

    Args:
        nivel: El nivel de triaje: "verde", "amarillo" o "rojo".
        sintomas: Los síntomas y datos reportados que sustentan la decisión,
            en frases completas y con las cifras que el paciente haya dado.
        justificacion: Por qué ese nivel, citando el documento o protocolo de
            la base de conocimiento que lo respalda si lo consultaste.
    """
    recursos: AppResources = params.app_resources
    try:
        nivel_valido = NivelAlerta(nivel.strip().lower())
    except ValueError:
        logger.warning(f"[herramienta] registrar_alerta con nivel inválido: {nivel!r}")
        await params.result_callback(
            {
                "error": f"El nivel '{nivel}' no existe.",
                "sugerencia": "Usa exactamente 'verde', 'amarillo' o 'rojo'.",
            }
        )
        return

    momento = datetime.now()
    alerta = Alerta(
        id_llamada=recursos.traza.id_llamada if recursos.traza else "sin-traza",
        momento=momento.isoformat(timespec="seconds"),
        nivel=nivel_valido,
        sintomas=sintomas.strip(),
        justificacion=justificacion.strip(),
        # Los pone el sistema, no el modelo, igual que el `nivel` del resumen:
        # son hechos de la sesión, y pedírselos al modelo sería dejar que
        # reescribiera en el registro lo que la puerta de cobertura ya decidió.
        procedimiento=recursos.cirugia_paciente,
        cobertura=_cobertura_actual(recursos),
    )
    carpeta = dir_alertas(recursos.settings.data_dir)
    ruta = _ruta_sin_pisar(carpeta, momento)
    try:
        escribir_json_atomico(ruta, alerta.model_dump(mode="json"))
    except OSError as e:
        logger.error(f"[herramienta] registrar_alerta no pudo escribir {ruta}: {e}")
        await params.result_callback(
            {
                "error": "La alerta no se pudo guardar en el disco.",
                "sugerencia": (
                    "Si el nivel es rojo, dile igualmente al paciente que acuda a "
                    "urgencias ahora: la indicación clínica no depende del registro."
                ),
            }
        )
        return

    # Para el resumen de respaldo si el paciente cuelga sin despedirse.
    recursos.ultima_alerta = alerta

    # El historial por número, si esta llamada tiene ficha. Nunca lanza y
    # sobre una llamada sin registrar (navegador) no hace nada.
    if recursos.historial is not None:
        recursos.historial.anotar_alerta(alerta.id_llamada, str(nivel_valido))

    logger.info(f"[herramienta] registrar_alerta({nivel_valido}) -> {ruta.name}")
    await params.result_callback(
        {
            "registrada": True,
            "nivel": str(nivel_valido),
            "que_decirle_al_paciente": _INDICACION_POR_NIVEL[nivel_valido],
        }
    )


async def finalizar_llamada(
    params: FunctionCallParams,
    paciente_y_procedimiento: str,
    sintomas: str,
    decision: str,
    proximos_pasos: str,
    referencias: str = "",
) -> None:
    """Guarda el resumen estructurado de la llamada antes de despedirte.

    Llámala SIEMPRE al final de la llamada, cuando ya te estés despidiendo:
    es lo que deja constancia del seguimiento para el equipo médico. Antes de
    llamarla asegúrate de haber registrado el triaje con `registrar_alerta`.

    Args:
        paciente_y_procedimiento: Quién es el paciente y de qué cirugía se le
            hace seguimiento, con los días transcurridos si los sabes.
        sintomas: Los síntomas y datos que reportó, en frases completas.
        decision: El nivel de triaje decidido y qué se hizo: qué se le indicó
            al paciente y si se avisó al equipo médico.
        proximos_pasos: Qué pasa después: quién contacta a quién, cuándo, y
            qué debe vigilar el paciente.
        referencias: Los documentos de la base de conocimiento en los que
            apoyaste tus indicaciones, por su nombre, si consultaste alguno.
    """
    recursos: AppResources = params.app_resources
    momento = datetime.now()
    resumen = ResumenLlamada(
        id_llamada=recursos.traza.id_llamada if recursos.traza else "sin-traza",
        momento=momento.isoformat(timespec="seconds"),
        # El color del triaje lo pone el sistema desde la alerta registrada,
        # no la redacción del modelo: es lo que exige que el registro de la
        # llamada deje la gravedad clara e inequívoca.
        nivel=str(recursos.ultima_alerta.nivel) if recursos.ultima_alerta else "",
        paciente_y_procedimiento=paciente_y_procedimiento.strip(),
        # La cirugía sola, sin pasar por la prosa del modelo: es la que se puede
        # cruzar con los temas del corpus y la que rearma la puerta cuando este
        # número vuelva a llamar.
        procedimiento=recursos.cirugia_paciente,
        cobertura=_cobertura_actual(recursos),
        sintomas=sintomas.strip(),
        decision=decision.strip(),
        referencias=referencias.strip(),
        proximos_pasos=proximos_pasos.strip(),
        # La traza real del RAG, no la memoria del modelo: es lo que resiste
        # una verificación contra la fuente.
        documentos_consultados=recursos.traza.documentos_consultados if recursos.traza else [],
    )
    carpeta = dir_resumenes(recursos.settings.data_dir)
    ruta = _ruta_sin_pisar(carpeta, momento)
    try:
        escribir_json_atomico(ruta, resumen.model_dump(mode="json"))
    except OSError as e:
        logger.error(f"[herramienta] finalizar_llamada no pudo escribir {ruta}: {e}")
        await params.result_callback(
            {
                "error": "El resumen no se pudo guardar en el disco.",
                "sugerencia": "Despídete con normalidad; el fallo es del sistema, no tuyo.",
            }
        )
        return

    recursos.resumen_guardado = True

    if recursos.historial is not None:
        recursos.historial.anotar_resumen(
            resumen.id_llamada,
            paciente_y_procedimiento=resumen.paciente_y_procedimiento,
            decision=resumen.decision,
            proximos_pasos=resumen.proximos_pasos,
            nivel=resumen.nivel,
            procedimiento=resumen.procedimiento,
        )

    logger.info(f"[herramienta] finalizar_llamada -> {ruta.name}")
    await params.result_callback({"guardado": True, "fichero": ruta.name})


__all__ = ["finalizar_llamada", "registrar_alerta"]
