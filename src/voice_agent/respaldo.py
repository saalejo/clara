"""El resumen de respaldo: lo que queda cuando la llamada muere sin despedida.

Los pacientes cuelgan sin avisar, una llamada telefónica puede
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


def transcripcion_de(contexto: LLMContext, *, quien_habla: str = "paciente") -> list[str]:
    """Extrae la conversación hablada del contexto, sin herramientas ni sistema.

    Args:
        contexto: El contexto del LLM al desmontar el pipeline.
        quien_habla: La etiqueta del interlocutor humano en la transcripción:
            "paciente" en el camino clínico, "visitante" en el comercial.
    """
    lineas: list[str] = []
    for mensaje in contexto.messages:
        rol = mensaje.get("role") if isinstance(mensaje, dict) else None
        contenido = mensaje.get("content") if isinstance(mensaje, dict) else None
        if rol in ("user", "assistant") and isinstance(contenido, str) and contenido.strip():
            quien = quien_habla if rol == "user" else "agente"
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
            # La cirugía sí puede constar aunque el modelo no llegara a
            # redactar nada: la trae el evento de llamada o la dijo el paciente
            # en el primer turno, y es lo que rearma la puerta si vuelve a
            # llamar. Vacía si la llamada se cortó antes de saberla.
            procedimiento=recursos.cirugia_paciente,
            cobertura=alerta.cobertura if alerta else "",
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
                procedimiento=resumen.procedimiento,
            )
        logger.info(f"Resumen de respaldo persistido: {ruta.name}")
    except Exception:
        logger.exception("No se pudo escribir el resumen de respaldo")


def cierre_de_prospecto(recursos: AppResources, contexto: LLMContext) -> None:
    """Anota la conversación comercial en el almacén, al desmontar el pipeline.

    Es el equivalente en modo prospectos de `resumen_de_respaldo`, y más
    sencillo a propósito: aquí no hay triaje que reconstruir. La transcripción
    se guarda siempre que se habló algo; si el modelo no llegó a
    `guardar_brief` —el visitante colgó sin despedirse— se deja un resumen
    mínimo para que la ficha diga algo la próxima vez.

    Nunca lanza: se ejecuta durante el desmontaje del pipeline y un fallo aquí
    no debe enmascarar el motivo real del cierre.
    """
    try:
        almacen = recursos.prospectos
        id_conversacion = recursos.traza.id_llamada if recursos.traza else ""
        if almacen is None or not id_conversacion:
            return
        transcripcion = transcripcion_de(contexto, quien_habla="visitante")
        if not transcripcion:
            return
        almacen.anotar_transcripcion(id_conversacion, "\n".join(transcripcion))
        # Que el VISITANTE haya hablado tras el aviso del saludo es la conducta
        # inequívoca del D. 1377 art. 7 — el saludo de Clara solo, no: con
        # `append_to_context` la transcripción nunca está vacía. El almacén
        # además solo lo anota si el aviso quedó registrado (sin aviso previo
        # no hay consentimiento que valga).
        if any(linea.startswith("visitante:") for linea in transcripcion):
            almacen.anotar_consentimiento(id_conversacion)
        if not recursos.brief_guardado:
            almacen.anotar_resumen(id_conversacion, "Terminó sin brief; ver la transcripción.")
        logger.info(f"Conversación de prospecto anotada: {id_conversacion}")
    except Exception:
        logger.exception("No se pudo anotar la conversación del prospecto")


__all__ = ["cierre_de_prospecto", "resumen_de_respaldo", "transcripcion_de"]
