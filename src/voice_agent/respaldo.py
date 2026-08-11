"""El resumen de respaldo: lo que queda cuando la llamada muere sin despedida.

Los pacientes (y los jueces) cuelgan sin avisar, una llamada telefónica puede
caerse por cobertura, y "qué queda al terminar la llamada" no puede depender
de que al modelo le dé tiempo a llamar a `finalizar_llamada`. Este respaldo no
redacta nada: deja los hechos que el sistema ya tiene —la última alerta, la
traza documental y la transcripción cruda— para que el equipo médico no pierda
la llamada.

Vive en su propio módulo porque lo usan los dos pipelines: el del navegador
(`web.py`) y el telefónico (`telefonia_llamada.py`), y el segundo no puede
importar del primero — `web.py` ya importa de él.
"""

from __future__ import annotations

from datetime import datetime

from loguru import logger
from pipecat.processors.aggregators.llm_context import LLMContext

from voice_agent.resources import AppResources
from voice_agent_core.evaluaciones import ResumenLlamada
from voice_agent_core.rutas import dir_resumenes, escribir_json_atomico


def transcripcion_de(contexto: LLMContext) -> list[str]:
    """Extrae la conversación hablada del contexto, sin herramientas ni sistema."""
    lineas: list[str] = []
    for mensaje in contexto.messages:
        rol = mensaje.get("role") if isinstance(mensaje, dict) else None
        contenido = mensaje.get("content") if isinstance(mensaje, dict) else None
        if rol in ("user", "assistant") and isinstance(contenido, str) and contenido.strip():
            quien = "paciente" if rol == "user" else "agente"
            lineas.append(f"{quien}: {contenido.strip()}")
    return lineas


def resumen_de_respaldo(recursos: AppResources, contexto: LLMContext) -> None:
    """Persiste un resumen mínimo si la llamada terminó sin despedida.

    Si el modelo ya guardó el suyo (`resumen_guardado`) o no llegó a hablarse
    nada, no hace falta y no escribe. También anota el historial del paciente:
    una llamada caída a medias tiene que contar la próxima vez que ese número
    llame.

    Nunca lanza: se ejecuta durante el desmontaje del pipeline y un fallo aquí
    no debe enmascarar el motivo real del cierre.
    """
    try:
        transcripcion = transcripcion_de(contexto)
        if not transcripcion or recursos.resumen_guardado:
            return
        alerta = recursos.ultima_alerta
        momento = datetime.now()
        resumen = ResumenLlamada(
            id_llamada=recursos.traza.id_llamada if recursos.traza else "sin-traza",
            momento=momento.isoformat(timespec="seconds"),
            nivel=str(alerta.nivel) if alerta else "",
            paciente_y_procedimiento=(
                "No registrado: la llamada terminó sin despedida. Ver transcripción."
            ),
            sintomas=alerta.sintomas if alerta else "Ver transcripción.",
            decision=(
                f"Triaje {alerta.nivel} registrado como alerta ({alerta.justificacion})"
                if alerta
                else "La llamada terminó antes de registrar un triaje."
            ),
            proximos_pasos=(
                "Revisar la alerta registrada y contactar al paciente."
                if alerta
                else "Revisar la transcripción y valorar si procede contactar al paciente."
            ),
            documentos_consultados=(
                recursos.traza.documentos_consultados if recursos.traza else []
            ),
            transcripcion=transcripcion,
        )
        carpeta = dir_resumenes(recursos.settings.data_dir)
        ruta = carpeta / f"{momento:%Y%m%d-%H%M%S}-respaldo.json"
        escribir_json_atomico(ruta, resumen.model_dump(mode="json"))
        if recursos.historial is not None:
            recursos.historial.anotar_resumen(
                resumen.id_llamada,
                paciente_y_procedimiento=resumen.paciente_y_procedimiento,
                decision=resumen.decision,
                proximos_pasos=resumen.proximos_pasos,
                nivel=resumen.nivel,
            )
        logger.info(f"Resumen de respaldo persistido: {ruta.name}")
    except Exception:
        logger.exception("No se pudo escribir el resumen de respaldo")


__all__ = ["resumen_de_respaldo", "transcripcion_de"]
