"""La llamada de voz desde el navegador.

El agente de la sala habla por ALSA; aquí la "sala" es una pestaña del
navegador: el micrófono y el altavoz viajan por WebRTC (`SmallWebRTCTransport`,
sobre aiortc) y la señalización por HTTP (`POST /api/offer`). La interfaz que
ve el visitante es la UI precompilada de `pipecat-ai-small-webrtc-prebuilt`,
montada en `/`; lo que importa es el contrato funcional —iniciar llamada,
hablar, escuchar—, no el diseño visual.

Decisiones que no son obvias mirando el código:

* **Un pipeline por conexión, servicios precargados.** Como en la telefonía
  (`ServiciosDeLlamada`): un procesador de Pipecat pertenece a un pipeline, y
  construir Piper cuesta segundos que pagaría el saludo. El juego (STT, LLM,
  TTS) se carga al arrancar el servidor y se repone al colgar.
* **Una sola conexión a la vez** (`ConnectionMode.SINGLE`). La placa tiene
  3,8 GB de RAM y la evaluación es una sesión: dos pipelines simultáneos no
  aportan nada y compiten por la CPU con la síntesis de voz. Y precisamente
  porque solo cabe una, **no se desaloja a quien está hablando**: ver
  `_esperar_hueco`.
* **Detrás de una puerta.** Esto está publicado en internet; sin código de
  acceso, cualquiera gasta la cuota gratuita del modelo y ocupa la sesión.
  Ver `voice_agent.acceso` y `docs/seguridad.md`.
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
import re
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
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

from voice_agent.acceso import PuertaDeAcceso, enlace_de_whatsapp, es_https
from voice_agent.bot import _preparar_telefonia
from voice_agent.fillers import FillerBank, FillerProcessor
from voice_agent.logging import setup_logging
from voice_agent.metrica import MetricsRecorder, anotar_evento
from voice_agent.misiones_agente import AlmacenMisiones
from voice_agent.rag.retriever import Retriever
from voice_agent.resources import AppResources
from voice_agent.respaldo import cierre_de_prospecto, resumen_de_respaldo
from voice_agent.services import (
    build_llm,
    build_stt,
    build_tts,
    build_turn_strategies,
    build_vad,
)
from voice_agent.tareas_programadas import MisionesLlamada, ProgramadorTareas, SalaActual
from voice_agent.telefonia_audio import ClienteAudioSCO
from voice_agent.telefonia_llamada import ServiciosDeLlamada, atender_llamada
from voice_agent.tools import herramientas_activas
from voice_agent.traza import TrazaLlamada
from voice_agent_core.config import Settings, get_settings
from voice_agent_core.historial import HistorialPacientes
from voice_agent_core.limitador import LimitadorDeIntentos, ip_del_cliente
from voice_agent_core.prospectos import AlmacenProspectos, FichaProspecto
from voice_agent_core.runtime import RuntimeConfig, cargar_runtime
from voice_agent_core.rutas import ruta_historial, ruta_log_agente, ruta_prospectos

#: Lo que se añade al prompt del sistema en una llamada por navegador. Igual
#: que en telefonía, sustituye el contexto de "estás en la habitación": quien
#: habla está al otro lado de una llamada.
PROMPT_LLAMADA_WEB = """

Estás atendiendo una llamada de voz: quien te habla está al otro lado de la
línea, no en la habitación. Sé breve —es una llamada, no una charla— y no
prometas acciones que no puedas hacer desde aquí."""

#: Nombre de la galleta que identifica al prospecto entre visitas.
NOMBRE_GALLETA_PROSPECTO = "vd_prospecto"

#: Vida de la galleta. Constante y no campo de `Settings` a propósito: es un
#: detalle del reconocimiento de visitas, no un ajuste que operar desde el
#: panel, y cada campo nuevo de `Settings` arrastra formulario y tests.
DIAS_GALLETA_PROSPECTO = 180

#: Un id de prospecto legítimo es el `uuid4().hex` que emitió este servidor.
#: La galleta no va firmada —es un token-capacidad inadivinable, y firmarla
#: ataría su validez al código de acceso, que rota—, así que lo único que hay
#: que parar es la basura: un valor inventado con otra pinta no abre ficha.
_ID_PROSPECTO = re.compile(r"^[0-9a-f]{32}$")

#: Se añade al prompt del sistema cuando el navegador ya tiene ficha: es la
#: versión comercial de `PROMPT_HISTORIAL_PREVIO` en telefonía, con la misma
#: cautela — la memoria es del dispositivo, no de la persona.
PROMPT_PROSPECTO_RECURRENTE = """

Memoria comercial de este navegador: {total} conversación(es) registradas. La
última, el {fecha}: {detalle}. Dale continuidad —retoma lo pendiente en vez de
empezar de cero— pero confirma antes con quién hablas: el navegador es del
dispositivo, no de la persona, y puede ser alguien distinto."""


def modo_prospectos(runtime: RuntimeConfig) -> bool:
    """Decide si el pipeline web trabaja en modo comercial (prospectos).

    Se decide por la configuración de herramientas y no por el nombre del
    perfil, que es solo informativo: un perfil con `guardar_brief` encendida y
    `finalizar_llamada` apagada es un perfil comercial, se llame como se
    llame. Con los valores de fábrica —sin `runtime.json`, todas encendidas—
    cae en clínico, así que la degradación de `cargar_runtime` conserva el
    comportamiento de siempre.
    """
    apagadas = set(runtime.herramientas_desactivadas)
    return "guardar_brief" not in apagadas and "finalizar_llamada" in apagadas


def galleta_de_prospecto(cookies: Mapping[str, str]) -> tuple[str, bool]:
    """Resuelve el id del prospecto de esta visita.

    Returns:
        El id (el de la galleta si es legítimo, o uno nuevo) y si hay que
        emitir la galleta en la respuesta.
    """
    valor = (cookies.get(NOMBRE_GALLETA_PROSPECTO) or "").strip()
    if _ID_PROSPECTO.fullmatch(valor):
        return valor, False
    return uuid4().hex, True


def _describir_ficha_prospecto(ficha: FichaProspecto) -> str:
    """Resume la ficha en una frase para el prompt, como `_describir_ficha`."""
    partes = []
    if ficha.prospecto.nombre or ficha.prospecto.empresa:
        quien = " — ".join(p for p in (ficha.prospecto.nombre, ficha.prospecto.empresa) if p)
        partes.append(f"habló {quien}")
    brief = ficha.ultimo_brief
    if brief is not None:
        if brief.necesidad:
            partes.append(f"necesitaba {brief.necesidad}")
        if brief.proximos_pasos:
            partes.append(f"quedó pendiente: {brief.proximos_pasos}")
    elif ficha.ultima.resumen:
        partes.append(ficha.ultima.resumen)
    if not partes:
        return "sin detalles registrados (la conversación quedó sin resumen)"
    return "; ".join(partes)


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

    def __init__(
        self,
        settings: Settings,
        runtime: RuntimeConfig,
        retriever: Retriever | None = None,
        prospectos: AlmacenProspectos | None = None,
    ) -> None:
        """Prepara el almacén sin cargar nada todavía.

        Args:
            settings: Configuración del agente.
            runtime: Configuración del panel, ya cargada.
            retriever: Buscador ya construido, para compartirlo con otros
                pipelines del mismo proceso. Dos `PersistentClient` de Chroma
                abriéndose a la vez en hilos distintos corrompen su caché
                interna ("Could not connect to tenant default_tenant") — pasó
                al precargar web y telefonía en paralelo.
            prospectos: La memoria comercial, solo en modo prospectos. Que
                esté aquí o no es lo que decide el modo de cada conversación.
        """
        self._settings = settings
        self._runtime = runtime
        self._retriever = retriever
        self._prospectos = prospectos
        self._listos: tuple[Any, Any, Any] | None = None
        self._cargando: asyncio.Task[None] | None = None
        self._banco: FillerBank | None = None
        self._recursos: AppResources | None = None

    @property
    def banco(self) -> FillerBank | None:
        """El banco de muletillas, si están activadas y cargadas."""
        return self._banco

    @property
    def prospectos(self) -> AlmacenProspectos | None:
        """La memoria comercial compartida, o `None` fuera del modo prospectos."""
        return self._prospectos

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
                        settings=self._settings,
                        retriever=self._retriever or Retriever(self._settings),
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


def _hay_llamada_viva(handler: SmallWebRTCRequestHandler) -> bool:
    """¿Hay alguien realmente al teléfono ahora mismo?

    `is_connected()` de pipecat no mira el estado de aiortc —que tarda decenas
    de segundos en enterarse de una pestaña cerrada— sino la hora del último
    ping del cliente: es `False` a los tres segundos de que el navegador deje
    de dar señales. Eso es justo lo que distingue una sesión zombi (pestaña
    recargada, portátil suspendido) de una llamada viva.
    """
    return any(conexion.is_connected() for conexion in handler._pcs_map.values())


async def _esperar_hueco(
    handler: SmallWebRTCRequestHandler, cerrojo: asyncio.Lock, espera_secs: float
) -> bool:
    """Espera, poco, a que la sesión anterior demuestre que ya no está.

    Rechazar en seco reintroduciría por la puerta de atrás el fallo que
    arregló el "gana la última conexión": al recargar la pestaña, la sesión
    previa tarda unos segundos en delatarse y quien recarga se comería un 409.
    Sondeando el latido unos segundos, la pestaña recargada solo nota un
    arranque un pelín más lento, y a quien intenta echar a otro se le dice que
    no.

    El cerrojo evita que veinte peticiones a la vez sean veinte tareas
    dormidas: solo se admite una espera simultánea.
    """
    if not _hay_llamada_viva(handler):
        return True
    if espera_secs <= 0 or cerrojo.locked():
        return False
    async with cerrojo:
        limite = time.monotonic() + espera_secs
        while time.monotonic() < limite:
            await asyncio.sleep(0.5)
            if not _hay_llamada_viva(handler):
                return True
    return False


async def _vigilar_duracion(
    worker: PipelineWorker,
    conexion: SmallWebRTCConnection,
    settings: Settings,
    id_llamada: str,
) -> None:
    """Avisa, se despide y cuelga cuando la llamada se pasa de larga.

    El aviso va como `TTSSpeakFrame`, igual que el saludo de `_saludar`:
    cortar sin avisar en una llamada clínica es maleducado y encima parece una
    avería. No se encola un `EndFrame` sino que se desconecta el transporte,
    porque lo que se está defendiendo aquí es la CPU de la placa y el corte
    tiene que llegar aunque el pipeline esté atascado.

    Al colgar por aquí, el `finally` de `_conversar` sigue anotando el fin y
    guardando el resumen: una llamada cortada por tiempo deja su traza igual.
    """
    aviso = settings.web_llamada_max_secs - settings.web_aviso_previo_secs
    if aviso > 0:
        await asyncio.sleep(aviso)
        await worker.queue_frames([TTSSpeakFrame(text=settings.web_aviso_cierre)])
    await asyncio.sleep(min(settings.web_aviso_previo_secs, settings.web_llamada_max_secs))
    await worker.queue_frames([TTSSpeakFrame(text=settings.web_despedida_cierre)])
    anotar_evento(settings.data_dir, "llamada_cortada_por_tiempo", id_llamada=id_llamada)
    await asyncio.sleep(settings.web_cierre_gracia_secs)
    logger.info("Llamada cortada por alcanzar la duración máxima")
    await conexion.disconnect()  # type: ignore[no-untyped-call]


async def _conversar(
    conexion: SmallWebRTCConnection, servicios: ServiciosWeb, id_prospecto: str = ""
) -> None:
    """Monta el pipeline sobre la conexión WebRTC y conversa hasta que cuelguen.

    No propaga excepciones: corre como tarea suelta y un fallo aquí no debe
    tumbar el servidor HTTP.

    Args:
        conexion: La conexión WebRTC recién negociada.
        servicios: Los servicios precargados del pipeline web.
        id_prospecto: El id de la galleta del visitante, solo en modo
            prospectos; vacío en el clínico.
    """
    settings = servicios.settings
    runtime = servicios.runtime
    try:
        stt, llm, tts = await servicios.tomar()

        # Recursos POR LLAMADA: el retriever es compartido (solo lectura),
        # pero la traza documental pertenece a esta conversación — es lo que
        # permite verificar qué documento respaldó cada respuesta.
        recursos = None
        ficha_prospecto: FichaProspecto | None = None
        if servicios.recursos is not None:
            recursos = AppResources(
                settings=settings,
                retriever=servicios.recursos.retriever,
                traza=TrazaLlamada(settings.data_dir),
                prospectos=servicios.prospectos,
                id_prospecto=id_prospecto,
            )
            if servicios.prospectos is not None and id_prospecto:
                # La ficha se lee ANTES de registrar la conversación en curso:
                # así el "ya hubo N conversaciones" del prompt cuenta solo las
                # anteriores. Registrar al principio y no al colgar es la
                # doctrina de `registrar_llamada`: una caída también cuenta.
                ficha_prospecto = servicios.prospectos.ficha(id_prospecto)
                if recursos.traza is not None:
                    servicios.prospectos.registrar_conversacion(
                        recursos.traza.id_llamada, id_prospecto
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

        prompt_sistema = runtime.prompt.prompt_sistema_efectivo + PROMPT_LLAMADA_WEB
        if ficha_prospecto is not None:
            prompt_sistema += PROMPT_PROSPECTO_RECURRENTE.format(
                total=ficha_prospecto.total_conversaciones,
                fecha=ficha_prospecto.ultima.momento[:10],
                detalle=_describir_ficha_prospecto(ficha_prospecto),
            )
        contexto = LLMContext(
            messages=[{"role": "system", "content": prompt_sistema}],
            # Sin recursos (índice ausente) no se ofrece ninguna herramienta:
            # anunciar una herramienta que no puede funcionar hace que el
            # modelo diga que la ha consultado (ver docs/herramientas.md).
            #
            # `incluir_agenda=False` está escrito a mano aunque sea el valor
            # por defecto, porque aquí NO es un descuido: este pipeline es el
            # de un desconocido con un enlace, y programar llamadas es marcar
            # números arbitrarios desde la placa, en diferido. Ver
            # `docs/seguridad.md` antes de encenderlo.
            tools=herramientas_activas(
                runtime.herramientas_desactivadas,
                incluir_telefonia=False,
                incluir_agenda=False,
            )
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
            # Antes era None ("colgar es la única forma normal de terminar; el
            # jurado puede quedarse callado pensando"). Ya no: una pestaña
            # abierta y olvidada mantiene viva la conexión de streaming con
            # Deepgram, que se factura por tiempo conectado y tiene cuota
            # gratuita. Cinco minutos de silencio absoluto son de sobra para
            # pensar una respuesta.
            idle_timeout_secs=settings.web_inactividad_secs or None,
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
            # El registro del aviso de privacidad (D. 1377 art. 8) se anota al
            # pronunciarlo, no al colgar: es el hecho que hay que poder probar
            # aunque la llamada se corte a medias. El texto exacto y no una
            # versión: el saludo es editable desde el panel.
            if (
                recursos is not None
                and recursos.prospectos is not None
                and recursos.traza is not None
            ):
                recursos.prospectos.anotar_aviso(recursos.traza.id_llamada, saludo)

        @transporte.event_handler("on_client_disconnected")  # type: ignore[untyped-decorator]
        async def _colgar(_transporte: SmallWebRTCTransport, _cliente: Any) -> None:
            logger.info("Cliente WebRTC desconectado; desmontando el pipeline")
            await worker.cancel()

        logger.info("Pipeline web montado; esperando audio del navegador")
        anotar_evento(settings.data_dir, "llamada_inicio", id_llamada=id_llamada)
        vigilante: asyncio.Task[None] | None = None
        if settings.web_llamada_max_secs > 0:
            vigilante = asyncio.create_task(
                _vigilar_duracion(worker, conexion, settings, id_llamada)
            )
        try:
            await WorkerRunner().run(worker)
        finally:
            if vigilante is not None:
                vigilante.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await vigilante
            anotar_evento(settings.data_dir, "llamada_fin", id_llamada=id_llamada)
            if recursos is not None:
                if recursos.prospectos is not None:
                    # Modo prospectos: la conversación se anota en su almacén
                    # y NO se escribe el respaldo clínico — sembraría
                    # `data/evaluaciones/` de falsos pacientes que la página
                    # Pacientes del panel enseñaría.
                    cierre_de_prospecto(recursos, contexto)
                else:
                    resumen_de_respaldo(recursos, contexto)
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
        # El buscador se construye UNA vez, antes de las dos precargas (web y
        # telefonía): abrir dos clientes de Chroma en paralelo corrompe su
        # caché interna, y además el índice es de solo lectura.
        retriever_compartido: Retriever | None = None
        try:
            retriever_compartido = await asyncio.to_thread(Retriever, config)
        except Exception:
            logger.exception("Sin RAG; el agente atenderá sin herramientas")
        # La memoria comercial solo se abre en modo prospectos: en el perfil
        # clínico el fichero ni se crea, y la conversación web se comporta
        # exactamente como siempre.
        almacen_prospectos: AlmacenProspectos | None = None
        if modo_prospectos(runtime):
            almacen_prospectos = AlmacenProspectos(ruta_prospectos(config.data_dir))
            logger.info("Modo prospectos activo: las conversaciones web dejan ficha comercial")
        servicios = ServiciosWeb(
            config, runtime, retriever=retriever_compartido, prospectos=almacen_prospectos
        )
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
        # Solo una petición espera a la vez a que se libere la sesión.
        _app.state.cerrojo_sesion = asyncio.Lock()
        # La red que queda si el código de acceso se filtra: el enlace
        # compartido no se convierte en barra libre de minutos de
        # transcripción. Es de llamadas ACEPTADAS, no de intentos, así que un
        # "olvidar" no tendría sentido aquí.
        # El castigo dura lo mismo que la ventana: alcanzado el tope, hay que
        # esperar una hora desde la última llamada aceptada. Con un castigo de
        # 0 el cubo se daría por vencido al instante y no frenaría nada.
        _app.state.cuota_llamadas = LimitadorDeIntentos(
            max_intentos=config.web_llamadas_max_por_hora,
            ventana_secs=3600.0,
            bloqueo_secs=3600.0,
        )

        # --- Telefonía: la otra superficie, coexistiendo con el navegador ---
        # Mismo patrón que `bot.ejecutar` sin la sala: si el puente Bluetooth
        # contesta, este proceso atiende también las llamadas del móvil
        # emparejado (contestador clínico) y ejecuta las llamadas de misión
        # programadas. Sin puente, la interfaz web queda exactamente como
        # antes: una línea en el log y ni un traceback. Todo en el MISMO
        # proceso a propósito: el modelo de embeddings tiene `lru_cache`, así
        # que el pipeline de llamada reutiliza el que ya cargó el web.
        telefonia = await _preparar_telefonia(config)
        audio_llamadas: ClienteAudioSCO | None = None
        programador: asyncio.Task[None] | None = None
        if telefonia is not None:
            misiones = MisionesLlamada()
            # Sin sala montada: las misiones de tipo sala se aplazan solas.
            sala = SalaActual()
            # La memoria entre llamadas por número de teléfono. Solo el camino
            # telefónico la recibe: una conexión de navegador no tiene número
            # con el que abrir una ficha.
            historial = HistorialPacientes(ruta_historial(config.data_dir))
            # La agenda de misiones puntuales, compartida por el planificador y
            # el pipeline de la llamada. Al pipeline del NAVEGADOR no llega a
            # propósito: quien tiene el enlace no debe poder agendar llamadas
            # salientes desde la placa (ver docs/seguridad.md).
            almacen = AlmacenMisiones(config)
            await almacen.cargar()
            await almacen.caducar_vencidas()
            servicios_llamada = ServiciosDeLlamada(
                config,
                runtime,
                retriever=retriever_compartido,
                historial=historial,
                almacen=almacen,
            )
            servicios_llamada.precargar()

            async def _al_llegar_audio(sock: Any, metadatos: dict[str, Any]) -> None:
                await atender_llamada(
                    sock,
                    metadatos,
                    config,
                    runtime,
                    servicios_llamada,
                    misiones=misiones,
                    telefonia=telefonia,
                )

            audio_llamadas = ClienteAudioSCO(
                config.telefonia_socket.with_name("telefonia-audio.sock"),
                _al_llegar_audio,
            )
            audio_llamadas.arrancar()
            programador = asyncio.create_task(
                ProgramadorTareas(config, sala, telefonia, misiones, almacen=almacen).correr()
            )
            logger.info("Telefonía activa: contestador clínico y llamadas de misión")

        logger.info("Interfaz de llamada lista")
        try:
            yield
        finally:
            if programador is not None:
                programador.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await programador
            if audio_llamadas is not None:
                await audio_llamadas.parar()

    app = FastAPI(title="Llamada de voz — Voz Digital", lifespan=_vida)

    # La puerta se instala aquí y no junto a las rutas: un middleware envuelve
    # al router entero, así que el `mount("/")` del final también queda detrás.
    # Sin código configurado no se monta nada y el servidor se comporta como
    # siempre, que es lo que mantiene `make run-web` usable sin ceremonia.
    codigo = config.web_codigo_acceso.get_secret_value() if config.web_codigo_acceso else ""
    if codigo:
        app.add_middleware(
            PuertaDeAcceso,
            codigo=codigo,
            duracion_secs=config.web_acceso_horas * 3600,
            limitador=LimitadorDeIntentos(
                max_intentos=config.web_acceso_max_intentos,
                bloqueo_secs=float(config.web_acceso_bloqueo_secs),
            ),
            al_evento=lambda tipo, ip: anotar_evento(config.data_dir, tipo, ip=ip),
            # Vacío si no hay número configurado, que es lo normal: la portada
            # simplemente no ofrece el botón.
            whatsapp=enlace_de_whatsapp(config.web_whatsapp, config.web_whatsapp_mensaje),
        )
        logger.info("Interfaz de llamada protegida por código de acceso")
    else:
        logger.warning(
            "WEB_CODIGO_ACCESO está vacío: la interfaz de llamada queda ABIERTA a "
            "cualquiera que conozca la URL, y sin la portada de la puerta tampoco "
            "hay aviso escrito de privacidad antes de conectar (queda solo el "
            "hablado del saludo). En la placa esto no debería pasar."
        )

    # `response_model=None` es obligatorio: el tipo de retorno es una unión con
    # `JSONResponse` (los rechazos por ocupado o por cuota) y FastAPI intenta
    # derivar de él un modelo de Pydantic. Sin esto, `crear_app` lanza
    # `FastAPIError` AL ARRANCAR, no al llamar a la ruta.
    @app.post("/api/offer", response_model=None)
    async def _ofertar(
        request: Request, respuesta: Response
    ) -> dict[str, str] | JSONResponse | None:
        """Negocia la sesión WebRTC con el navegador y arranca la conversación.

        El parámetro `respuesta` lo inyecta FastAPI: sus cabeceras se fusionan
        con el `dict` que se devuelve en el camino bueno, que es donde se emite
        la galleta del prospecto. En los rechazos (`JSONResponse` directa) no
        se fusiona nada, y está bien así: a quien no se atiende no se le
        identifica.
        """
        # Una oferta SDP legítima no llega a 10 KB. Leer megabytes en memoria
        # para descartarlos después sería regalarle la RAM de la placa a quien
        # ya tenga la galleta.
        if int(request.headers.get("content-length") or 0) > 64 * 1024:
            return JSONResponse({"error": "oferta_demasiado_grande"}, status_code=413)

        cuerpo = await request.json()
        oferta = SmallWebRTCRequest.from_dict(cuerpo)
        handler: SmallWebRTCRequestHandler = request.app.state.handler
        servicios: ServiciosWeb = request.app.state.servicios

        # Antes ganaba SIEMPRE la última conexión: cualquier oferta sin pc_id
        # desconectaba la sesión en curso. Eso arreglaba un fallo real —una
        # pestaña recargada dejaba un zombi en el handler que rechazaba toda
        # oferta nueva con un 400, y había que reiniciar el servicio— pero
        # permitía que un tercero le cortara la palabra al que estuviera
        # hablando, en bucle. Ahora solo se desaloja a quien ya no da señales
        # de vida; a quien está hablando se le respeta y al recién llegado se
        # le dice que espere.
        if oferta.pc_id is None:
            # La cuota se mira ANTES de esperar hueco y antes de desmontar
            # nada: es la comprobación más barata y no tiene sentido esperar
            # ocho segundos para acabar rechazando.
            cuota: LimitadorDeIntentos = request.app.state.cuota_llamadas
            ip = ip_del_cliente(request.headers, request.client.host if request.client else None)
            if cuota.max_intentos > 0 and not cuota.permitido(ip):
                logger.warning(f"Cuota de llamadas agotada para {ip}")
                anotar_evento(servicios.settings.data_dir, "cuota_agotada", ip=ip)
                return JSONResponse(
                    {
                        "error": "cuota_agotada",
                        "mensaje": (
                            "Se alcanzó el número de llamadas por hora de esta "
                            "demostración. Vuelve a intentarlo más tarde."
                        ),
                    },
                    status_code=429,
                    headers={"Retry-After": "600"},
                )

            hueco = await _esperar_hueco(
                handler,
                request.app.state.cerrojo_sesion,
                servicios.settings.web_espera_sesion_secs,
            )
            if not hueco:
                logger.info("Oferta rechazada: hay una llamada en curso")
                return JSONResponse(
                    {
                        "error": "ocupado",
                        "mensaje": (
                            "Clara está atendiendo otra llamada. Inténtalo de nuevo en un minuto."
                        ),
                    },
                    status_code=409,
                    headers={"Retry-After": "30"},
                )
            for conexion_previa in list(handler._pcs_map.values()):
                logger.info(f"Desconectando la sesión previa {conexion_previa.pc_id}")
                await conexion_previa.disconnect()  # type: ignore[no-untyped-call]
            handler._pcs_map.clear()

            # Solo se cuenta la llamada que de verdad se va a atender.
            if cuota.max_intentos > 0:
                cuota.anotar_fallo(ip)

        # La identidad del visitante entre visitas, solo en modo prospectos.
        # La galleta se emite aquí y no en la puerta de acceso: la puerta
        # protege también al perfil clínico, que no debe identificar a nadie.
        id_prospecto = ""
        if servicios.prospectos is not None:
            id_prospecto, emitir = galleta_de_prospecto(request.cookies)
            if emitir:
                respuesta.set_cookie(
                    NOMBRE_GALLETA_PROSPECTO,
                    id_prospecto,
                    max_age=DIAS_GALLETA_PROSPECTO * 86400,
                    httponly=True,
                    samesite="lax",
                    secure=es_https(request),
                    path="/",
                )

        async def _al_conectar(conexion: SmallWebRTCConnection) -> None:
            # La conversación corre en su propia tarea: esta petición HTTP
            # tiene que devolver la respuesta SDP ya, no al colgar.
            llamadas: set[asyncio.Task[None]] = request.app.state.llamadas
            tarea = asyncio.create_task(_conversar(conexion, servicios, id_prospecto))
            llamadas.add(tarea)
            tarea.add_done_callback(llamadas.discard)

        return await handler.handle_web_request(oferta, _al_conectar)

    @app.get("/salud")
    async def _salud() -> dict[str, str]:
        """Sonda de vida para el túnel."""
        return {"estado": "ok"}

    # La UI va al final: monta en `/` y se quedaría con todas las rutas que se
    # declaren después. La puerta no se ve afectada por esto: un middleware
    # envuelve al router entero, no compite con sus rutas.
    app.mount("/", SmallWebRTCPrebuiltUI)

    return app


__all__ = [
    "DIAS_GALLETA_PROSPECTO",
    "NOMBRE_GALLETA_PROSPECTO",
    "PROMPT_LLAMADA_WEB",
    "PROMPT_PROSPECTO_RECURRENTE",
    "ServiciosWeb",
    "crear_app",
    "galleta_de_prospecto",
    "modo_prospectos",
]
