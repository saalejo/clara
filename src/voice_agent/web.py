"""La llamada de voz desde el navegador (compuerta G4 del reto).

El agente de la sala habla por ALSA; aquí la "sala" es una pestaña del
navegador: el micrófono y el altavoz viajan por WebRTC (`SmallWebRTCTransport`,
sobre aiortc) y la señalización por HTTP (`POST /api/offer`). La interfaz que
ve el jurado es la UI precompilada de `pipecat-ai-small-webrtc-prebuilt`,
montada en `/`; el diseño visual no puntúa en el reto, el contrato funcional
—iniciar llamada, hablar, escuchar— sí.

Decisiones que no son obvias mirando el código:

* **Un pipeline por conexión, servicios precargados.** Como en la telefonía
  (`ServiciosDeLlamada`): un procesador de Pipecat pertenece a un pipeline, y
  construir Piper cuesta segundos que pagaría el saludo. El juego (STT, LLM,
  TTS) se carga al arrancar el servidor y se repone al colgar.
* **Una sola conexión a la vez** (`ConnectionMode.SINGLE`). La placa tiene
  3,8 GB de RAM y la evaluación es una sesión: dos pipelines simultáneos no
  aportan nada y compiten por la CPU con la síntesis de voz.
* **Sin compuerta de micrófono ni semidúplex.** El navegador aplica
  cancelación de eco acústico en `getUserMedia` (activada por defecto), así
  que aquí sí se puede interrumpir al agente (*barge-in*), que es la
  interacción natural. La compuerta es un remedio para el altavoz de la sala,
  no para esto.
* **El VAD va en `LLMUserAggregatorParams`**, no en `TransportParams`. La
  trampa está documentada en `build_vad`: Pydantic descarta el campo en
  silencio y el agente jamás detecta voz.

El servidor se arranca con `make run-web` (uvicorn con `--factory`). Para que
el micrófono funcione, la página debe servirse por HTTPS u `http://localhost`:
`getUserMedia` no existe en orígenes inseguros — de ahí el túnel de
Cloudflare del despliegue (ver README).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request
from loguru import logger
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import NOT_GIVEN, LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregatorParams,
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    ConnectionMode,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner
from pipecat_ai_small_webrtc_prebuilt.frontend import SmallWebRTCPrebuiltUI

from voice_agent.fillers import FillerBank, FillerProcessor
from voice_agent.logging import setup_logging
from voice_agent.metrica import MetricsRecorder, anotar_evento
from voice_agent.rag.retriever import Retriever
from voice_agent.resources import AppResources
from voice_agent.services import (
    build_llm,
    build_stt,
    build_tts,
    build_turn_strategies,
    build_vad,
)
from voice_agent.tools import herramientas_activas
from voice_agent.traza import TrazaLlamada
from voice_agent_core.config import Settings, get_settings
from voice_agent_core.evaluaciones import ResumenLlamada
from voice_agent_core.runtime import RuntimeConfig, cargar_runtime
from voice_agent_core.rutas import dir_resumenes, escribir_json_atomico, ruta_log_agente

#: Lo que se añade al prompt del sistema en una llamada por navegador. Igual
#: que en telefonía, sustituye el contexto de "estás en la habitación": quien
#: habla está al otro lado de una llamada.
PROMPT_LLAMADA_WEB = """

Estás atendiendo una llamada de voz: quien te habla está al otro lado de la
línea, no en la habitación. Sé breve —es una llamada, no una charla— y no
prometas acciones que no puedas hacer desde aquí."""


def _servidores_ice(settings: Settings) -> list[IceServer]:
    """Convierte las URLs de configuración en objetos ICE de aiortc.

    La conversión vive aquí y no en `voice_agent_core.config` a propósito:
    `IceServer` es un alias de `RTCIceServer` (aiortc), y el paquete `core` no
    puede depender de aiortc — lo importa el panel, cuya imagen no lo lleva.
    Las credenciales TURN se aplican solo a las URLs `turn:`/`turns:`; un
    servidor STUN no las necesita y aiortc las rechazaría.
    """
    servidores: list[IceServer] = []
    for url in settings.ice_servers_urls:
        if url.startswith(("turn:", "turns:")) and settings.turn_username is not None:
            credencial = settings.turn_credential
            servidores.append(
                IceServer(
                    urls=url,
                    username=settings.turn_username,
                    credential=credencial.get_secret_value() if credencial else None,
                )
            )
        else:
            servidores.append(IceServer(urls=url))
    return servidores


class ServiciosWeb:
    """Los servicios del pipeline de navegador, cargados antes de la llamada.

    El mismo patrón que `ServiciosDeLlamada` en la telefonía, sin la parte de
    SCO: construir Piper es cargar su modelo ONNX y hacerlo al descolgar se lo
    cobraría el saludo. Cada conexión toma el juego listo; al cerrarse se
    encarga el siguiente.
    """

    def __init__(self, settings: Settings, runtime: RuntimeConfig) -> None:
        """Prepara el almacén sin cargar nada todavía."""
        self._settings = settings
        self._runtime = runtime
        self._listos: tuple[Any, Any, Any] | None = None
        self._cargando: asyncio.Task[None] | None = None
        self._banco: FillerBank | None = None
        self._recursos: AppResources | None = None

    @property
    def banco(self) -> FillerBank | None:
        """El banco de muletillas, si están activadas y cargadas."""
        return self._banco

    @property
    def recursos(self) -> AppResources | None:
        """Lo que las herramientas reciben en `app_resources`."""
        return self._recursos

    @property
    def runtime(self) -> RuntimeConfig:
        """La configuración del panel con la que arrancó el servidor."""
        return self._runtime

    @property
    def settings(self) -> Settings:
        """La configuración efectiva del agente."""
        return self._settings

    def precargar(self) -> None:
        """Lanza la carga en segundo plano si no hay ya un juego listo."""
        if self._listos is not None or (self._cargando is not None and not self._cargando.done()):
            return
        self._cargando = asyncio.create_task(self._cargar())

    async def _cargar(self) -> None:
        def construir() -> tuple[Any, Any, Any]:
            stt = build_stt(self._settings)
            llm = build_llm(self._settings)
            tts = build_tts(self._settings)
            if self._settings.filler_enabled and self._banco is None:
                banco = FillerBank(self._settings)
                banco.preparar(self._runtime.prompt.muletillas)
                self._banco = banco
            # El RAG se abre una vez y se comparte: es de solo lectura y la
            # lista de colecciones se relee en cada consulta, así que un
            # reindexado desde el panel se ve sin reiniciar (compuerta G5).
            if self._recursos is None:
                try:
                    self._recursos = AppResources(
                        settings=self._settings, retriever=Retriever(self._settings)
                    )
                except Exception:
                    logger.exception("Sin RAG para la llamada web; se atenderá sin herramientas")
            return stt, llm, tts

        try:
            self._listos = await asyncio.to_thread(construir)
            logger.info("Servicios del pipeline web precargados")
        except Exception:
            logger.exception("No se pudieron precargar los servicios del pipeline web")

    async def tomar(self) -> tuple[Any, Any, Any]:
        """Entrega el juego listo (o espera la carga); la reposición es al colgar."""
        if self._listos is None:
            if self._cargando is None or self._cargando.done():
                self.precargar()
            assert self._cargando is not None
            await self._cargando
            if self._listos is None:
                raise RuntimeError("la precarga de servicios del pipeline web falló")
        trio = self._listos
        self._listos = None
        return trio


def _transcripcion_de(contexto: LLMContext) -> list[str]:
    """Extrae la conversación hablada del contexto, sin herramientas ni sistema."""
    lineas: list[str] = []
    for mensaje in contexto.messages:
        rol = mensaje.get("role") if isinstance(mensaje, dict) else None
        contenido = mensaje.get("content") if isinstance(mensaje, dict) else None
        if rol in ("user", "assistant") and isinstance(contenido, str) and contenido.strip():
            quien = "paciente" if rol == "user" else "agente"
            lineas.append(f"{quien}: {contenido.strip()}")
    return lineas


def _resumen_de_respaldo(recursos: AppResources, contexto: LLMContext) -> None:
    """Persiste un resumen mínimo si la llamada terminó sin despedida.

    Los pacientes (y los jueces) cuelgan sin avisar, y "qué queda al terminar
    la llamada" no puede depender de que al modelo le dé tiempo a llamar a
    `finalizar_llamada`. Este respaldo no redacta nada: deja los hechos que el
    sistema ya tiene —la última alerta, la traza documental y la transcripción
    cruda— para que el equipo médico no pierda la llamada.

    Nunca lanza: se ejecuta durante el desmontaje del pipeline y un fallo aquí
    no debe enmascarar el motivo real del cierre.
    """
    try:
        transcripcion = _transcripcion_de(contexto)
        if not transcripcion or recursos.resumen_guardado:
            return
        alerta = recursos.ultima_alerta
        momento = datetime.now()
        resumen = ResumenLlamada(
            id_llamada=recursos.traza.id_llamada if recursos.traza else "sin-traza",
            momento=momento.isoformat(timespec="seconds"),
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
        logger.info(f"Resumen de respaldo persistido: {ruta.name}")
    except Exception:
        logger.exception("No se pudo escribir el resumen de respaldo")


async def _conversar(conexion: SmallWebRTCConnection, servicios: ServiciosWeb) -> None:
    """Monta el pipeline sobre la conexión WebRTC y conversa hasta que cuelguen.

    No propaga excepciones: corre como tarea suelta y un fallo aquí no debe
    tumbar el servidor HTTP.
    """
    settings = servicios.settings
    runtime = servicios.runtime
    try:
        stt, llm, tts = await servicios.tomar()

        # Recursos POR LLAMADA: el retriever es compartido (solo lectura),
        # pero la traza documental pertenece a esta conversación — es lo que
        # permite verificar qué documento respaldó cada respuesta.
        recursos = None
        if servicios.recursos is not None:
            recursos = AppResources(
                settings=settings,
                retriever=servicios.recursos.retriever,
                traza=TrazaLlamada(settings.data_dir),
            )

        transporte = SmallWebRTCTransport(
            webrtc_connection=conexion,
            params=TransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=settings.audio_sample_rate,
                audio_out_sample_rate=settings.audio_sample_rate,
            ),
        )

        contexto = LLMContext(
            messages=[
                {
                    "role": "system",
                    "content": runtime.prompt.prompt_sistema_efectivo + PROMPT_LLAMADA_WEB,
                }
            ],
            # Sin recursos (índice ausente) no se ofrece ninguna herramienta:
            # anunciar una herramienta que no puede funcionar hace que el
            # modelo diga que la ha consultado (ver docs/herramientas.md).
            tools=herramientas_activas(runtime.herramientas_desactivadas, incluir_telefonia=False)
            if recursos is not None
            else NOT_GIVEN,
        )
        agregador = LLMContextAggregatorPair(
            contexto,
            user_params=LLMUserAggregatorParams(
                # El VAD va AQUÍ, no en TransportParams. Ver build_vad().
                vad_analyzer=build_vad(settings),
                user_turn_strategies=build_turn_strategies(settings),
                # Sin estrategias de silenciado: el navegador cancela el eco y
                # el barge-in es deseable en una llamada.
                user_mute_strategies=[],
            ),
            assistant_params=LLMAssistantAggregatorParams(),
        )

        muletillas = None
        if servicios.banco is not None:
            muletillas = FillerProcessor(servicios.banco, settings)

        # Tras la salida de audio: por ahí pasan los frames de habla del bot
        # (es el hueco donde el pipeline de sala pone su controlador de
        # compuerta), que son los que cierran la medición voz-a-voz.
        id_llamada = recursos.traza.id_llamada if recursos and recursos.traza else "sin-id"
        metricas = MetricsRecorder(settings.data_dir, id_llamada)

        pipeline = Pipeline(
            [
                transporte.input(),
                stt,
                agregador.user(),
                llm,
                *([muletillas] if muletillas else []),
                tts,
                transporte.output(),
                metricas,
                agregador.assistant(),
            ]
        )

        worker = PipelineWorker(
            pipeline,
            # Colgar es la única forma normal de terminar; el jurado puede
            # quedarse callado pensando sin que se le cuelgue.
            idle_timeout_secs=None,
            params=PipelineParams(
                audio_in_sample_rate=settings.audio_sample_rate,
                audio_out_sample_rate=settings.audio_sample_rate,
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
            app_resources=recursos,
        )

        saludo = runtime.prompt.saludo_inicial

        @transporte.event_handler("on_client_connected")  # type: ignore[untyped-decorator]
        async def _saludar(_transporte: SmallWebRTCTransport, _cliente: Any) -> None:
            # El agente habla primero: en una llamada, el silencio inicial se
            # confunde con una conexión rota. `append_to_context` deja el
            # saludo en el historial para que la conversación sea coherente.
            logger.info("Cliente WebRTC conectado; saludando")
            await worker.queue_frames([TTSSpeakFrame(text=saludo, append_to_context=True)])

        @transporte.event_handler("on_client_disconnected")  # type: ignore[untyped-decorator]
        async def _colgar(_transporte: SmallWebRTCTransport, _cliente: Any) -> None:
            logger.info("Cliente WebRTC desconectado; desmontando el pipeline")
            await worker.cancel()

        logger.info("Pipeline web montado; esperando audio del navegador")
        anotar_evento(settings.data_dir, "llamada_inicio", id_llamada=id_llamada)
        try:
            await WorkerRunner().run(worker)
        finally:
            anotar_evento(settings.data_dir, "llamada_fin", id_llamada=id_llamada)
            if recursos is not None:
                _resumen_de_respaldo(recursos, contexto)
    except Exception:
        logger.exception("El pipeline de la llamada web murió")
    finally:
        logger.info("Llamada web terminada")
        servicios.precargar()


def crear_app(settings: Settings | None = None) -> FastAPI:
    """Construye la aplicación FastAPI de la interfaz de llamada.

    Es una factoría (uvicorn la arranca con `--factory`) para que importar el
    módulo no cargue configuración ni modelos: los tests pueden importar
    `web.py` sin tocar el entorno, y los servicios pesados se cargan en el
    `lifespan`, cuando el servidor arranca de verdad.

    Args:
        settings: Configuración explícita, para los tests. En producción se
            resuelve con `get_settings()`.

    Returns:
        La aplicación lista para servir con uvicorn.
    """
    config = settings if settings is not None else get_settings()

    @contextlib.asynccontextmanager
    async def _vida(_app: FastAPI) -> AsyncIterator[None]:
        # A fichero además de a consola: el panel sigue el log desde ahí.
        setup_logging(config.log_level, archivo=ruta_log_agente(config.data_dir))
        runtime = cargar_runtime(config.data_dir)
        servicios = ServiciosWeb(config, runtime)
        servicios.precargar()
        _app.state.servicios = servicios
        # Las conversaciones en curso. Guardar la referencia es obligatorio
        # (asyncio solo retiene débilmente las tareas) y además permite saber
        # qué hay vivo.
        _app.state.llamadas = set()
        _app.state.handler = SmallWebRTCRequestHandler(
            ice_servers=_servidores_ice(config),
            connection_mode=ConnectionMode.SINGLE,
        )
        logger.info("Interfaz de llamada lista")
        yield

    app = FastAPI(title="Llamada de voz — seguimiento postoperatorio", lifespan=_vida)

    @app.post("/api/offer")
    async def _ofertar(request: Request) -> dict[str, str] | None:
        """Negocia la sesión WebRTC con el navegador y arranca la conversación."""
        cuerpo = await request.json()
        oferta = SmallWebRTCRequest.from_dict(cuerpo)
        handler: SmallWebRTCRequestHandler = request.app.state.handler
        servicios: ServiciosWeb = request.app.state.servicios

        # Gana la última conexión. En modo SINGLE, una sesión anterior que no
        # llegó a cerrarse (una pestaña recargada, un portátil suspendido)
        # queda zombi en el handler y rechaza toda oferta nueva con un 400 —
        # pasó en una prueba real: recargar la página bloqueaba la interfaz
        # hasta reiniciar el servicio. Si llega una oferta sin pc_id (una
        # sesión nueva de verdad), la anterior está muerta o va a morir:
        # desconectarla desmonta su pipeline por el evento "closed" y deja el
        # sitio libre. Un juez que recarga la pestaña siempre puede volver.
        if oferta.pc_id is None:
            for conexion_previa in list(handler._pcs_map.values()):
                logger.info(f"Desconectando la sesión previa {conexion_previa.pc_id}")
                await conexion_previa.disconnect()  # type: ignore[no-untyped-call]
            handler._pcs_map.clear()

        async def _al_conectar(conexion: SmallWebRTCConnection) -> None:
            # La conversación corre en su propia tarea: esta petición HTTP
            # tiene que devolver la respuesta SDP ya, no al colgar.
            llamadas: set[asyncio.Task[None]] = request.app.state.llamadas
            tarea = asyncio.create_task(_conversar(conexion, servicios))
            llamadas.add(tarea)
            tarea.add_done_callback(llamadas.discard)

        return await handler.handle_web_request(oferta, _al_conectar)

    @app.get("/salud")
    async def _salud() -> dict[str, str]:
        """Sonda de vida para el túnel y el README del jurado."""
        return {"estado": "ok"}

    # La UI va al final: monta en `/` y se quedaría con todas las rutas que se
    # declaren después.
    app.mount("/", SmallWebRTCPrebuiltUI)

    return app


__all__ = ["PROMPT_LLAMADA_WEB", "ServiciosWeb", "crear_app"]
