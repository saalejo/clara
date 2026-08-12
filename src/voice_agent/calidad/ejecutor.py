"""Orquesta un ensayo: hace conversar al paciente con Clara y lo juzga.

Un ensayo corre en un `data_dir` **aislado** —`data/calidad/sandbox/<id>/`— para
que las alertas y resúmenes de prueba no se cuelen en las páginas de Evaluaciones
y Pacientes del panel, que leen del `data_dir` real. El buscador del RAG, en
cambio, es el real (su índice vive en el `data_dir` real), así que Clara consulta
el conocimiento de verdad. El expediente del ensayo y la métrica de gasto sí van
al `data_dir` real, donde el panel los lee.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from google.genai import types
from loguru import logger

from voice_agent.calidad.arnes import ArnesClara
from voice_agent.calidad.cliente import ClienteGemini, ClienteLLM
from voice_agent.calidad.juez import chequeo_determinista, juzgar
from voice_agent.calidad.simulador import MARCA_COLGAR, SimuladorPaciente
from voice_agent.metrica import anotar_evento
from voice_agent.rag.retriever import Retriever
from voice_agent.resources import AppResources
from voice_agent.tools import herramientas_activas
from voice_agent.traza import TrazaLlamada
from voice_agent.web import PROMPT_LLAMADA_WEB
from voice_agent_core.calidad import (
    Escenario,
    EstadoLote,
    ResultadoEscenario,
    Turno,
    UsoLLM,
    escenario_por_id,
)
from voice_agent_core.config import LLMBackend, Settings
from voice_agent_core.evaluaciones import Alerta, ResumenLlamada
from voice_agent_core.runtime import RuntimeConfig, cargar_runtime
from voice_agent_core.rutas import (
    dir_alertas,
    dir_resultados_calidad,
    dir_resumenes,
    dir_sandbox_calidad,
    escribir_json_atomico,
    ruta_lote_calidad,
)

#: Pausa entre llamadas al LLM. El nivel gratuito de Gemini comparte cuota con el
#: agente en vivo, así que se respira entre peticiones para no agotarla.
PAUSA_ENTRE_LLAMADAS_S = 2.0

#: Tope de duración de un ensayo. Si Clara y el paciente se enredan, se corta y el
#: ensayo queda como error en vez de colgar el lote entero.
TIMEOUT_ESCENARIO_S = 180

#: Turno sembrado para que el historial de Clara empiece por un turno de usuario
#: (Gemini lo exige) sin que ella tenga que saludar dos veces.
_APERTURA = "(La llamada se acaba de conectar.)"


def _leer_alertas(data_dir: Path) -> list[Alerta]:
    """Lee las alertas que Clara dejó en el sandbox, tolerando ficheros a medias."""
    carpeta = dir_alertas(data_dir)
    if not carpeta.is_dir():
        return []
    alertas: list[Alerta] = []
    for ruta in sorted(carpeta.glob("*.json")):
        try:
            alertas.append(Alerta.model_validate_json(ruta.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            logger.warning(f"No se pudo leer la alerta {ruta}: {e}")
    return alertas


def _leer_resumen(data_dir: Path) -> ResumenLlamada | None:
    """Lee el último resumen del sandbox, si Clara llegó a guardarlo."""
    carpeta = dir_resumenes(data_dir)
    if not carpeta.is_dir():
        return None
    for ruta in sorted(carpeta.glob("*.json"), reverse=True):
        try:
            return ResumenLlamada.model_validate_json(ruta.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning(f"No se pudo leer el resumen {ruta}: {e}")
    return None


async def ejecutar_escenario(
    cliente: ClienteLLM,
    settings: Settings,
    runtime: RuntimeConfig,
    retriever: Retriever,
    escenario: Escenario,
) -> ResultadoEscenario:
    """Ensaya un escenario de principio a fin y devuelve su expediente.

    Nunca lanza: un fallo se registra como `estado="error"` en el propio
    resultado, para que un ensayo roto no tumbe el lote. Escribe el expediente en
    `data/calidad/resultados/` del `data_dir` real y anota el gasto en métricas.
    """
    inicio = datetime.now()
    id_ejecucion = f"calidad-{escenario.id}-{inicio:%Y%m%d-%H%M%S}"
    sandbox = dir_sandbox_calidad(settings.data_dir) / id_ejecucion
    resultado = ResultadoEscenario(
        id_ejecucion=id_ejecucion,
        escenario_id=escenario.id,
        categoria=str(escenario.categoria),
        momento=inicio.isoformat(timespec="seconds"),
    )
    turnos: list[Turno] = []
    uso_juez = UsoLLM()
    arnes: ArnesClara | None = None
    simulador: SimuladorPaciente | None = None
    try:
        settings_sandbox = settings.model_copy(update={"data_dir": sandbox})
        recursos = AppResources(
            settings=settings_sandbox,
            retriever=retriever,
            traza=TrazaLlamada(sandbox, id_llamada=id_ejecucion),
            historial=None,
        )
        prompt_sistema = runtime.prompt.prompt_sistema_efectivo + PROMPT_LLAMADA_WEB
        herramientas = herramientas_activas(
            runtime.herramientas_desactivadas, incluir_telefonia=False
        )
        arnes = ArnesClara(cliente, settings, prompt_sistema, herramientas, recursos)
        simulador = SimuladorPaciente(cliente, settings, escenario)

        async with asyncio.timeout(TIMEOUT_ESCENARIO_S):
            saludo = runtime.prompt.saludo_inicial
            turnos.append(Turno(rol="clara", texto=saludo))
            dialogo: list[tuple[str, str]] = [("clara", saludo)]
            historial_clara: list[types.Content] = [
                types.Content(role="user", parts=[types.Part.from_text(text=_APERTURA)]),
                types.Content(role="model", parts=[types.Part.from_text(text=saludo)]),
            ]

            for _ in range(escenario.max_turnos):
                texto_paciente = await simulador.replicar(dialogo)
                await asyncio.sleep(PAUSA_ENTRE_LLAMADAS_S)
                if MARCA_COLGAR in texto_paciente or not texto_paciente:
                    break
                dialogo.append(("paciente", texto_paciente))
                turnos.append(Turno(rol="paciente", texto=texto_paciente))
                historial_clara.append(
                    types.Content(role="user", parts=[types.Part.from_text(text=texto_paciente)])
                )

                texto_clara, turnos_tool = await arnes.responder(historial_clara)
                await asyncio.sleep(PAUSA_ENTRE_LLAMADAS_S)
                turnos.extend(turnos_tool)
                if texto_clara:
                    dialogo.append(("clara", texto_clara))
                    turnos.append(Turno(rol="clara", texto=texto_clara))
                if any(t.texto == "finalizar_llamada" for t in turnos_tool):
                    break

        alertas = _leer_alertas(sandbox)
        resumen = _leer_resumen(sandbox)
        documentos = recursos.traza.documentos_consultados if recursos.traza else []

        veredicto = chequeo_determinista(escenario, alertas)
        if veredicto is None:
            veredicto = await juzgar(
                cliente, settings, escenario, turnos, alertas, resumen, documentos, uso=uso_juez
            )

        resultado.turnos = turnos
        resultado.veredicto = veredicto
        resultado.alertas = alertas
        resultado.resumen = resumen
        resultado.documentos_consultados = documentos
    except Exception as e:
        # Un ensayo roto se registra como error, no se propaga: así no tumba el
        # resto del lote.
        logger.exception(f"El ensayo {id_ejecucion} falló: {e}")
        resultado.estado = "error"
        resultado.error = str(e)
        resultado.turnos = turnos

    resultado.uso = _total_uso(arnes, simulador, uso_juez, inicio)
    _persistir(settings, resultado)
    return resultado


def _total_uso(
    arnes: ArnesClara | None,
    simulador: SimuladorPaciente | None,
    uso_juez: UsoLLM,
    inicio: datetime,
) -> UsoLLM:
    """Suma el gasto de Clara, el paciente y el juez, y mide la duración."""
    total = UsoLLM(duracion_s=round((datetime.now() - inicio).total_seconds(), 1))
    for parte in (arnes.uso if arnes else None, simulador.uso if simulador else None, uso_juez):
        if parte is None:
            continue
        total.llamadas += parte.llamadas
        total.tokens_entrada += parte.tokens_entrada
        total.tokens_salida += parte.tokens_salida
    return total


def _persistir(settings: Settings, resultado: ResultadoEscenario) -> None:
    """Escribe el expediente en el `data_dir` real y anota la métrica de gasto."""
    ruta = dir_resultados_calidad(settings.data_dir) / f"{resultado.id_ejecucion}.json"
    try:
        escribir_json_atomico(ruta, resultado.model_dump(mode="json"))
    except OSError as e:
        logger.error(f"No se pudo escribir el resultado {ruta}: {e}")
    anotar_evento(
        settings.data_dir,
        "calidad_ejecucion",
        id_ejecucion=resultado.id_ejecucion,
        escenario=resultado.escenario_id,
        estado=resultado.estado,
        aprobado=resultado.veredicto.aprobado if resultado.veredicto else None,
        tokens_entrada=resultado.uso.tokens_entrada,
        tokens_salida=resultado.uso.tokens_salida,
        duracion_s=resultado.uso.duracion_s,
    )


def _publicar_lote(settings: Settings, estado: EstadoLote) -> None:
    """Deja el progreso del lote donde el panel lo lee, sin lanzar si falla."""
    try:
        escribir_json_atomico(ruta_lote_calidad(settings.data_dir), estado.model_dump(mode="json"))
    except OSError as e:
        logger.warning(f"No se pudo publicar el progreso del lote: {e}")


def _escribir_error(settings: Settings, escenario: Escenario, motivo: str) -> None:
    """Registra un ensayo que ni siquiera pudo arrancar, para que se vea en el panel."""
    momento = datetime.now()
    resultado = ResultadoEscenario(
        id_ejecucion=f"calidad-{escenario.id}-{momento:%Y%m%d-%H%M%S}",
        escenario_id=escenario.id,
        categoria=str(escenario.categoria),
        momento=momento.isoformat(timespec="seconds"),
        estado="error",
        error=motivo,
    )
    _persistir(settings, resultado)


async def ejecutar_lote(settings: Settings, ids: list[str], *, id_lote: str) -> None:
    """Ejecuta en secuencia los escenarios pedidos, publicando el progreso.

    Valida los ids contra el catálogo (los desconocidos se ignoran con un aviso).
    Solo soporta Gemini: con Groq registra cada escenario como error explicándolo.
    """
    escenarios = [e for e in (escenario_por_id(i) for i in ids) if e is not None]
    faltan = set(ids) - {e.id for e in escenarios}
    if faltan:
        logger.warning(f"Se ignoran escenarios desconocidos: {', '.join(sorted(faltan))}")

    total = len(escenarios)
    completados = 0
    _publicar_lote(settings, EstadoLote(id_lote=id_lote, total=total))
    try:
        if not escenarios:
            return
        if settings.llm_backend is LLMBackend.GROQ:
            for escenario in escenarios:
                _escribir_error(
                    settings,
                    escenario,
                    "El arnés de calidad solo soporta Gemini (LLM_BACKEND=gemini).",
                )
            return
        try:
            runtime = cargar_runtime(settings.data_dir)
            retriever = Retriever(settings)
            cliente: ClienteLLM = ClienteGemini(settings.require_llm_api_key())
        except Exception as e:
            # Preparar el runner puede fallar (sin índice, sin clave); se registra
            # cada escenario como error en vez de reventar el lote.
            logger.exception(f"No se pudo preparar el runner de calidad: {e}")
            for escenario in escenarios:
                _escribir_error(settings, escenario, f"No se pudo preparar el runner: {e}")
            return

        for escenario in escenarios:
            _publicar_lote(
                settings,
                EstadoLote(
                    id_lote=id_lote, total=total, completados=completados, en_curso=escenario.id
                ),
            )
            logger.info(f"Ensayando escenario '{escenario.id}' ({completados + 1}/{total})")
            await ejecutar_escenario(cliente, settings, runtime, retriever, escenario)
            completados += 1
    finally:
        _publicar_lote(
            settings,
            EstadoLote(id_lote=id_lote, total=total, completados=completados, terminado=True),
        )
