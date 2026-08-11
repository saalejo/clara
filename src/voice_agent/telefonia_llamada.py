"""El pipeline de una llamada: el agente conversando por teléfono.

Cada llamada monta su propio pipeline sobre el `TransporteSCO` — STT, LLM y
TTS **nuevos**, no los del pipeline de la sala. No es un lujo: un procesador
de Pipecat pertenece a un pipeline y compartirlo entre dos es pedir frames
cruzados. El precio es cargar Whisper otra vez al descolgar; lo paga el
saludo, que suena mientras el modelo termina de entrar en memoria.

El pipeline corre a **16 kHz de punta a punta** (`FRECUENCIA_PIPELINE`): el
transporte traduce a esa frecuencia gane quien gane la negociación de códec
—mSBC ya viene así y el CVSD se remuestrea—. Por eso el VAD se construye aquí
a esa frecuencia en vez de reutilizar `build_vad`, que va a la del micrófono
de la sala.

La vida del pipeline es la de la llamada: `nucleo.cerrado` —el canal SCO
muerto— cancela el worker, y colgar desde el otro lado es la única manera
normal de terminar. No hay timeout de inactividad: quien llama puede callarse
un rato sin que el contestador le cuelgue.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from typing import Any

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import StartFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import NOT_GIVEN, LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregatorParams,
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.workers.runner import WorkerRunner

from voice_agent.fillers import FillerBank, FillerProcessor
from voice_agent.rag.retriever import Retriever
from voice_agent.resources import AppResources
from voice_agent.respaldo import resumen_de_respaldo
from voice_agent.services import build_llm, build_stt, build_tts, build_turn_strategies
from voice_agent.tareas_programadas import (
    MisionesLlamada,
    MisionPendiente,
    instruccion_mision_llamada,
)
from voice_agent.telefonia import ClienteTelefonia
from voice_agent.telefonia_audio import TransporteSCO
from voice_agent.telefonia_codec import FRECUENCIA_PIPELINE
from voice_agent.tools import herramientas_activas
from voice_agent.traza import TrazaLlamada
from voice_agent_core.config import Settings
from voice_agent_core.historial import FichaPaciente, HistorialPacientes
from voice_agent_core.runtime import RuntimeConfig
from voice_agent_core.telefonia import Llamada

#: Lo que se le añade al prompt del sistema durante una llamada. Sustituye al
#: párrafo de telefonía de la sala: aquí el agente ES la llamada y las
#: herramientas de marcar o colgar no están montadas en este pipeline. Las
#: demás (RAG, fecha, hardware) sí: el prompt de la sala las anuncia, y un
#: modelo al que le anuncias una herramienta que no existe dice tranquilamente
#: que la ha consultado y se queda callado esperándola.
PROMPT_LLAMADA = """

Estás atendiendo una llamada telefónica entrante: quien te habla está al otro
lado del teléfono. Lo habitual es que sea un paciente en seguimiento
postoperatorio o un familiar; preséntate, sé breve —es una llamada— y haz el
seguimiento igual que siempre: confirma quién llama y de qué cirugía se
trata, indaga, consulta tus protocolos, clasifica y registra. No prometas
acciones que no puedas hacer desde aquí."""

#: La variante para una llamada que marcó el planificador de tareas. Sustituye
#: a `PROMPT_LLAMADA` entera: "toma recado si preguntan por alguien" es el
#: guion de una entrante, y aquí es el agente quien llama y con un encargo.
PROMPT_LLAMADA_MISION = """

Estás en una llamada telefónica: quien te habla está al otro lado del
teléfono, no en la habitación. No prometas acciones que no puedas hacer desde
aquí."""

#: Se añade a una llamada ENTRANTE cuando la agenda del móvil identifica al
#: número. La identidad es una pista, no una verificación: puede contestar un
#: familiar desde el teléfono del paciente, y los nombres de agenda a veces
#: son apodos — por eso se ordena confirmar, no asumir.
PROMPT_IDENTIDAD_AGENDA = """

El teléfono identifica a quien llama: en la agenda figura como «{nombre}»
(número {numero}). Salúdalo por ese nombre y confirma enseguida que hablas
con esa persona — puede ser un familiar llamando desde su teléfono. Si el
nombre de la agenda no parece el de una persona, pregunta el nombre como
siempre. Una vez confirmado, no vuelvas a pedirle el nombre."""

#: Se añade cuando el historial conoce el número — en entrantes Y en misiones.
#: Mismo tono cauto que la identidad de agenda: el historial es del NÚMERO,
#: no de la persona, y desde un mismo teléfono puede hablar un familiar.
PROMPT_HISTORIAL_PREVIO = """

Historial de este número: {total} llamada(s) de seguimiento registradas. La
última, el {fecha}: {detalle}. Dale continuidad —pregunta cómo ha seguido
desde entonces en vez de empezar de cero— pero confirma antes que hablas con
la misma persona: el historial es del teléfono, no de quien contesta."""


def _describir_ficha(ficha: FichaPaciente) -> str:
    """Resume la última llamada de la ficha en una frase para el prompt."""
    ultima = ficha.ultima
    partes = []
    if ultima.paciente_y_procedimiento:
        partes.append(ultima.paciente_y_procedimiento)
    if ultima.nivel:
        partes.append(f"triaje {ultima.nivel}")
    if ultima.decision:
        partes.append(ultima.decision)
    if ultima.proximos_pasos:
        partes.append(f"próximos pasos: {ultima.proximos_pasos}")
    if not partes:
        return "sin detalles registrados (la llamada quedó sin resumen)"
    return "; ".join(partes)


def _prompt_de_llamada(
    runtime: RuntimeConfig,
    mision: MisionPendiente | None,
    llamada: Llamada | None = None,
    ficha: FichaPaciente | None = None,
) -> str:
    """Compone el prompt del sistema del pipeline de esta llamada.

    Función pura y separada para poder probarla sin montar un pipeline: es lo
    único que distingue a una llamada de misión de una entrante normal.

    Args:
        runtime: Configuración del panel, ya cargada.
        mision: La misión si esta llamada la marcó el planificador.
        llamada: La llamada según el puente, si se pudo consultar. En una
            entrante, su `nombre_agenda` permite saludar al paciente por su
            nombre; en una misión no se usa, porque la tarea ya trae el
            contacto.
        ficha: El historial del número, si ya llamó antes. Se añade tanto en
            entrantes como en misiones: es la memoria entre llamadas.
    """
    base = runtime.prompt.prompt_sistema_efectivo
    if mision is None:
        prompt = base + PROMPT_LLAMADA
        if llamada is not None and llamada.nombre_agenda:
            prompt += PROMPT_IDENTIDAD_AGENDA.format(
                nombre=llamada.nombre_agenda, numero=llamada.numero or "desconocido"
            )
    else:
        prompt = base + PROMPT_LLAMADA_MISION + instruccion_mision_llamada(mision.tarea)
    if ficha is not None:
        prompt += PROMPT_HISTORIAL_PREVIO.format(
            total=ficha.total_llamadas,
            fecha=ficha.ultima.momento[:10],
            detalle=_describir_ficha(ficha),
        )
    return prompt


async def atender_llamada(
    sock: socket.socket,
    metadatos: dict[str, Any],
    settings: Settings,
    runtime: RuntimeConfig,
    servicios: ServiciosDeLlamada,
    *,
    misiones: MisionesLlamada | None = None,
    telefonia: ClienteTelefonia | None = None,
) -> None:
    """Conversa por la llamada hasta que cuelguen.

    No propaga excepciones: la llama el `ClienteAudioSCO` y un fallo aquí no
    puede tumbar ni el bucle de recepción ni el agente.

    Args:
        sock: El socket SCO recién entregado por el puente.
        metadatos: Tarjeta, códec y MTU del handoff.
        settings: Configuración del agente.
        runtime: Configuración del panel, ya cargada.
        servicios: Los servicios precargados del pipeline de llamada.
        misiones: El registro de misiones de llamada pendientes, si este
            agente tiene planificador. La correlación con el SCO se resuelve
            dentro de `_conversar`, no aquí — ver su docstring.
        telefonia: El cliente del puente, para confirmar la misión contra su
            estado.
    """
    logger.info(f"Atendiendo el audio de la llamada {metadatos.get('tarjeta', '?')}")
    try:
        await _conversar(
            sock,
            int(metadatos.get("codec", 1)),
            settings,
            runtime,
            servicios,
            misiones=misiones,
            telefonia=telefonia,
        )
    except Exception as e:
        logger.exception(f"El pipeline de la llamada murió: {e}")
        with contextlib.suppress(OSError):
            sock.close()
    logger.info("Audio de la llamada terminado")
    servicios.precargar()


async def _conversar(
    sock: socket.socket,
    codec: int,
    settings: Settings,
    runtime: RuntimeConfig,
    servicios: ServiciosDeLlamada,
    *,
    misiones: MisionesLlamada | None = None,
    telefonia: ClienteTelefonia | None = None,
) -> None:
    """Monta el pipeline, saluda y corre hasta que el canal muera."""
    transporte = TransporteSCO(sock, codec)
    # El núcleo arranca YA: ceba con silencio y drena la recepción mientras
    # los modelos del pipeline cargan. Medido sin esto: el móvil reventó el
    # SCO con ECONNRESET antes de que el pipeline llegara a saludar.
    transporte.nucleo.arrancar()
    # La correlación con la misión va AQUÍ, después de arrancar el núcleo y
    # no antes de llamar a `atender_llamada`: confirmar el estado EN_CURSO
    # puede tardar varios reintentos (ver `MisionesLlamada.tomar_si_en_curso`)
    # y esa espera solo es segura una vez el socket ya se está drenando.
    # Retrasar el arranque del núcleo para esperar la confirmación es
    # exactamente lo que revienta el SCO con ECONNRESET.
    mision = await misiones.tomar_si_en_curso(telefonia) if misiones is not None else None

    # La identidad de quien llama, si la agenda del móvil la conoce. Nunca
    # puede impedir atender: si el puente no contesta a tiempo, la llamada
    # sigue y Clara pregunta el nombre como siempre.
    llamada_actual: Llamada | None = None
    if mision is None and telefonia is not None:
        try:
            estado = await telefonia.estado()
            llamada_actual = next(iter(estado.llamadas), None)
        except Exception as e:
            logger.warning(f"No se pudo consultar la identidad de la llamada: {e}")

    stt, llm, tts = await servicios.tomar()

    # La identidad de la llamada para el historial: en una misión el número
    # viene congelado en la tarea; en una entrante, del puente. La ficha se
    # consulta ANTES de registrar la llamada actual, para que el prompt hable
    # solo de las anteriores.
    if mision is not None:
        numero = mision.tarea.contacto_numero
        nombre = mision.tarea.contacto_nombre
        direccion = "mision"
    else:
        numero = llamada_actual.numero if llamada_actual is not None else ""
        nombre = (llamada_actual.nombre_agenda or "") if llamada_actual is not None else ""
        direccion = "entrante"

    ficha: FichaPaciente | None = None
    recursos = servicios.recursos
    if recursos is not None:
        # Una traza nueva por llamada: es lo que da a las alertas y resúmenes
        # de teléfono un id propio en vez de "sin-traza", y al historial la
        # clave con la que anotarles el triaje. El objeto de recursos es
        # compartido, pero solo hay una llamada SCO a la vez.
        recursos.traza = TrazaLlamada(settings.data_dir)
        recursos.ultima_alerta = None
        recursos.resumen_guardado = False
        recursos.numero_llamada = numero
        if recursos.historial is not None:
            ficha = recursos.historial.ficha(numero)
            recursos.historial.registrar_llamada(
                recursos.traza.id_llamada, numero, direccion, nombre=nombre
            )
            if ficha is not None:
                logger.info(
                    f"[historial] el {numero} ya llamó {ficha.total_llamadas} vez/veces; "
                    "el prompt lleva su ficha"
                )

    contexto = LLMContext(
        messages=[
            {
                "role": "system",
                "content": _prompt_de_llamada(runtime, mision, llamada_actual, ficha),
            }
        ],
        # Las mismas herramientas de la sala menos las de telefonía. Al pasarlas
        # aquí, Pipecat registra solo sus manejadores; llegan al RAG a través de
        # `app_resources`, que se le da al worker más abajo. El prompt de la
        # sala las anuncia, así que montarlas no es opcional: un modelo al que
        # le anuncias una herramienta ausente dice que la ha consultado y se
        # queda callado esperándola. Sin recursos (índice ausente) no se ofrece
        # ninguna, que es lo coherente con ese mismo principio.
        tools=herramientas_activas(runtime.herramientas_desactivadas, incluir_telefonia=False)
        if servicios.recursos is not None
        else NOT_GIVEN,
    )
    agregador = LLMContextAggregatorPair(
        contexto,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(
                sample_rate=FRECUENCIA_PIPELINE,
                params=VADParams(
                    confidence=settings.effective_vad_confidence,
                    start_secs=settings.effective_vad_start_secs,
                    stop_secs=settings.effective_vad_stop_secs,
                    min_volume=settings.effective_vad_min_volume,
                ),
            ),
            user_turn_strategies=build_turn_strategies(settings),
        ),
        assistant_params=LLMAssistantAggregatorParams(),
    )

    # El banco de muletillas se comparte entre llamadas (son bytes cacheados),
    # pero el procesador pertenece a un pipeline: uno nuevo por llamada.
    muletillas = None
    if servicios.banco is not None:
        muletillas = FillerProcessor(servicios.banco, settings, sample_rate=FRECUENCIA_PIPELINE)

    pipeline = Pipeline(
        [
            transporte.input(),
            stt,
            agregador.user(),
            llm,
            # Entre el LLM y el TTS, como en la sala: ver fillers.py.
            *([muletillas] if muletillas else []),
            tts,
            transporte.output(),
            agregador.assistant(),
        ]
    )

    worker = PipelineWorker(
        pipeline,
        # Sin timeout de inactividad: colgar es la única forma de terminar.
        idle_timeout_secs=None,
        params=PipelineParams(
            audio_in_sample_rate=FRECUENCIA_PIPELINE,
            audio_out_sample_rate=FRECUENCIA_PIPELINE,
            enable_metrics=True,
        ),
        # Lo que las herramientas encuentran en `FunctionCallParams`: el RAG
        # precargado y la configuración. Sin cliente de telefonía: sus
        # herramientas no están montadas en este pipeline.
        app_resources=servicios.recursos,
    )

    saludo = settings.telefonia_saludo_llamada

    # Solo para entrantes. En una llamada normal, quien descuelga habla
    # primero — y aquí el agente ES quien descuelga, así que le toca saludar
    # él, con un texto fijo: quien llama espera oír algo ya (ver el ajuste).
    #
    # En una llamada de MISIÓN el agente es quien ha llamado, y ahí el
    # convenio telefónico es el contrario: quien contesta habla primero
    # ("¿Aló?"), y quien llama espera a oírlo antes de presentarse. Saludar
    # aquí con un texto fijo y genérico —además ajeno a la misión— le cortaba
    # la palabra a quien acababa de descolgar. No hace falta más que omitir
    # este saludo: el prompt de la misión ya le pide presentarse "enseguida",
    # así que en cuanto STT transcribe el "¿Aló?" del otro lado, el turno de
    # usuario dispara el LLM y el agente responde con la presentación.
    if mision is None:

        @worker.event_handler("on_pipeline_started")  # type: ignore[untyped-decorator]
        async def _saludar(_worker: PipelineWorker, _frame: StartFrame) -> None:
            # El SCO se abre antes de que el operador termine de enrutar el
            # audio hasta quien llama: saludar de inmediato es que solo oiga
            # el final de la frase. La espera cubre ese enrutado.
            await asyncio.sleep(settings.telefonia_saludo_espera_secs)
            logger.info(f"Saludando por la llamada: {saludo!r}")
            await _worker.queue_frames([TTSSpeakFrame(text=saludo, append_to_context=True)])

    async def _vigilar_cuelgue() -> None:
        await transporte.nucleo.cerrado.wait()
        logger.info("El canal SCO murió; desmontando el pipeline de la llamada")
        await worker.cancel()

    vigia = asyncio.create_task(_vigilar_cuelgue())
    try:
        await WorkerRunner().run(worker)
    finally:
        vigia.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await vigia
        await transporte.nucleo.parar()
        # Por teléfono se cuelga sin despedirse aún más que en el navegador —
        # y la llamada puede caerse sola por cobertura—, así que el respaldo
        # corre aquí igual: si el modelo no llegó a `finalizar_llamada`, la
        # transcripción y la última alerta quedan en disco y en el historial.
        if recursos is not None:
            resumen_de_respaldo(recursos, contexto)


class ServiciosDeLlamada:
    """Los servicios del pipeline de llamada, cargados ANTES de que suene.

    Construir un servicio de Pipecat es cargar su modelo — Whisper y Piper lo
    hacen en el constructor — y medido en la placa eso son más de 36 segundos
    con el agente de sala en marcha: quien llamaba colgó antes del saludo. La
    precarga mueve ese coste al arranque; cada llamada toma el juego listo y
    dispara en segundo plano la construcción del siguiente, así que los
    procesadores nunca se reutilizan entre pipelines.

    La carga corre en un hilo (`asyncio.to_thread`) para no congelar la
    conversación de la sala mientras tanto.
    """

    def __init__(
        self,
        settings: Settings,
        runtime: RuntimeConfig,
        retriever: Retriever | None = None,
        historial: HistorialPacientes | None = None,
    ) -> None:
        """Prepara el almacén sin cargar nada todavía.

        Args:
            settings: Configuración del agente.
            runtime: Configuración del panel, ya cargada.
            retriever: Buscador ya construido, para compartirlo entre los
                pipelines del proceso. Dos `PersistentClient` de Chroma
                abriéndose a la vez en hilos distintos corrompen su caché
                interna; el buscador es de solo lectura y compartirlo es
                seguro.
            historial: La memoria entre llamadas por número, compartida por
                el proceso; las herramientas la reciben en los recursos.
        """
        self._settings = settings
        self._runtime = runtime
        self._retriever = retriever
        self._historial = historial
        self._listos: tuple[Any, Any, Any] | None = None
        self._cargando: asyncio.Task[None] | None = None
        self._banco: FillerBank | None = None
        self._recursos: AppResources | None = None

    @property
    def banco(self) -> FillerBank | None:
        """El banco de muletillas, si las muletillas están activadas y cargadas."""
        return self._banco

    @property
    def recursos(self) -> AppResources | None:
        """Lo que las herramientas de la llamada reciben en `app_resources`."""
        return self._recursos

    def precargar(self) -> None:
        """Lanza la carga en segundo plano si no hay ya un juego listo."""
        if self._listos is not None or (self._cargando is not None and not self._cargando.done()):
            return
        self._cargando = asyncio.create_task(self._cargar())

    async def _cargar(self) -> None:
        def construir() -> tuple[Any, Any, Any]:
            # A la frecuencia del pipeline de llamada (16 kHz gane el códec
            # que gane; el transporte traduce): Deepgram recibe el flujo crudo
            # y con la frecuencia equivocada la voz le llega a media velocidad.
            stt = build_stt(self._settings, sample_rate=FRECUENCIA_PIPELINE)
            llm = build_llm(self._settings)
            tts = build_tts(self._settings, sample_rate=FRECUENCIA_PIPELINE)
            # El banco se prepara aquí y no en `_conversar` porque sintetizar
            # con Piper bloquea, y durante una llamada ese coste lo pagaría
            # quien está al teléfono. Va después de `build_tts` porque es el
            # servicio quien descarga la voz. Una sola vez: los bytes cacheados
            # sirven para todas las llamadas, solo el procesador es por llamada.
            if self._settings.filler_enabled and self._banco is None:
                banco = FillerBank(self._settings, sample_rate=FRECUENCIA_PIPELINE)
                banco.preparar(self._runtime.prompt.muletillas)
                self._banco = banco
            # El RAG también se precarga: abrir el índice durante la llamada
            # se lo cobraría la primera consulta. Uno solo para todas las
            # llamadas — es de solo lectura. Sin cliente de telefonía: las
            # herramientas de marcar o colgar no van en este pipeline. Si el
            # índice falta, el contestador sigue atendiendo, solo que sin
            # herramientas (y sin anunciarlas, que es lo importante).
            if self._recursos is None:
                try:
                    self._recursos = AppResources(
                        settings=self._settings,
                        retriever=self._retriever or Retriever(self._settings),
                        historial=self._historial,
                    )
                except Exception:
                    logger.exception("Sin RAG para las llamadas; se atenderá sin herramientas")
            return stt, llm, tts

        try:
            self._listos = await asyncio.to_thread(construir)
            await self._calentar_llm(self._listos[1])
            logger.info("Servicios del pipeline de llamada precargados")
        except Exception:
            logger.exception("No se pudieron precargar los servicios de llamada")

    async def _calentar_llm(self, llm: Any) -> None:
        """Abre de antemano la conexión HTTP al LLM, en frío antes de que suene.

        Medido en la placa con una llamada real: la primera respuesta de una
        conexión recién construida tardó 2,8 s de TTFB; la siguiente, con la
        misma conexión ya abierta, 0,8 s — la diferencia es DNS y el apretón
        de manos TLS, no generación. El cliente del SDK guarda conexiones
        vivas indefinidamente (`keepalive_expiry=None`, ver
        `BaseOpenAILLMService.create_client`), así que basta una petición
        mínima aquí para que la primera respuesta DE VERDAD, minutos después,
        la reutilice en vez de pagar ese coste con quien está al teléfono.

        Se cuela por `llm._client` —el `AsyncOpenAI` interno— porque Pipecat
        no ofrece un "calienta la conexión" público; es la única forma sin
        reimplementar el arranque del procesador fuera de un pipeline.

        Nunca lanza: si el proveedor del LLM no contesta durante la precarga,
        la llamada de verdad simplemente paga el coste que se quería evitar.
        """
        try:
            await llm._client.chat.completions.create(
                model=self._settings.llm_model_efectivo,
                messages=[{"role": "user", "content": "hola"}],
                max_tokens=1,
                timeout=10.0,
            )
        except Exception as e:
            logger.warning(f"No se pudo precalentar la conexión al LLM: {e}")

    async def tomar(self) -> tuple[Any, Any, Any]:
        """Entrega el juego listo (o espera la carga) y encarga el siguiente."""
        if self._listos is None:
            if self._cargando is None or self._cargando.done():
                self.precargar()
            assert self._cargando is not None
            await self._cargando
            if self._listos is None:
                raise RuntimeError("la precarga de servicios de llamada falló")
        trio = self._listos
        self._listos = None
        # La reposición NO se lanza aquí: cargar el juego de repuesto durante
        # la llamada compite por la CPU con el pipeline — medido: el saludo
        # tardó 18 segundos en sintetizarse. Se repone al colgar.
        return trio


__all__ = ["PROMPT_LLAMADA", "ServiciosDeLlamada", "atender_llamada"]
