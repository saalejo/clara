"""Montaje y ejecución del pipeline de voz.

Pipecat modela una conversación como una tubería de procesadores por la que
circulan *frames*: trozos de audio, transcripciones, texto generado, eventos de
inicio y fin de habla. Cada procesador transforma los frames que entiende y deja
pasar el resto.

El orden importa y no es arbitrario::

    transport.input()      captura del micrófono
    stt                    audio -> texto
    contexto.user()        añade lo dicho por el usuario al historial
    llm                    texto + historial + herramientas -> respuesta
    tts                    texto -> audio
    transport.output()     reproducción por el altavoz
    contexto.assistant()   añade la respuesta al historial

El agregador de contexto aparece partido en dos, antes y después del LLM, y esa
es la parte menos evidente del montaje. La mitad `user` tiene que ir justo
antes del LLM para que la pregunta esté ya en el historial cuando este arranca.
La mitad `assistant` va **al final**, después de la reproducción, y no
inmediatamente después del LLM: así, si a la persona le da por interrumpir a
mitad de la respuesta, lo que se guarda en el historial es lo que realmente
llegó a escuchar, no lo que el modelo había pensado decir. Sin eso, el agente
acabaría dando por dicho algo que nadie oyó.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import Sequence
from typing import Any

from loguru import logger
from pipecat.adapters.schemas.direct_function import DirectFunction
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.frames.frames import StartFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregatorParams,
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.mcp_service import MCPClient
from pipecat.workers.runner import WorkerRunner

from voice_agent import __version__
from voice_agent.audio_devices import (
    INTERVALO_SONDEO_SECS,
    AudioDeviceError,
    esperar_dispositivos,
    tarjetas_alsa,
)
from voice_agent.audio_gate import BotSpeechGateController, MicrophoneGate
from voice_agent.fillers import FillerBank, FillerProcessor
from voice_agent.hooks import construir_procesadores
from voice_agent.logging import log_startup_banner
from voice_agent.mcp import ResultadoMCP, cerrar_clientes, iniciar_clientes
from voice_agent.rag.retriever import Retriever
from voice_agent.resources import AppResources
from voice_agent.services import (
    build_llm,
    build_microphone_gate,
    build_mute_strategies,
    build_stt,
    build_transport,
    build_tts,
    build_turn_strategies,
    build_vad,
)
from voice_agent.tareas_programadas import MisionesLlamada, ProgramadorTareas, SalaActual
from voice_agent.telefonia import ClienteTelefonia
from voice_agent.telefonia_anuncios import escuchar_telefonia
from voice_agent.telefonia_audio import ClienteAudioSCO
from voice_agent.telefonia_llamada import ServiciosDeLlamada, atender_llamada
from voice_agent.tools import esquema_de, herramientas_activas, nombre_de
from voice_agent_core.config import ModoTelefonia, Settings
from voice_agent_core.estado import (
    ORIGEN_LOCAL,
    EstadoArranque,
    HerramientaExpuesta,
    escribir_estado,
)
from voice_agent_core.runtime import RuntimeConfig, cargar_runtime
from voice_agent_core.rutas import ruta_snapshot_settings

#: Lo que se le añade al prompt del sistema cuando hay teléfono, y solo
#: entonces. Va aquí y no en el prompt que gobierna el panel porque el panel no
#: sabe si hay puente: si lo pusiera siempre, un agente sin telefonía anunciaría
#: que sabe llamar por teléfono y —según avisa docs/herramientas.md— acabaría
#: diciendo que ha llamado.
PROMPT_TELEFONIA = """

Tienes un teléfono móvil conectado por Bluetooth y puedes usarlo. Reglas:
- Para llamar a alguien de la agenda usa `llamar_a_contacto`; nunca te inventes
  ni recuerdes un número de memoria.
- Antes de marcar, di en voz alta a quién vas a llamar y espera a que te lo
  confirmen. Solo entonces vuelves a llamar a la herramienta con `confirmado`.
- Si hay varias personas con el mismo nombre, pregunta cuál. No elijas tú.
- El audio de las llamadas va por el móvil, no por este altavoz."""


async def _preparar_telefonia(settings: Settings) -> ClienteTelefonia | None:
    """Decide si este agente tiene teléfono, y devuelve el cliente si lo tiene.

    El sondeo es lo que hace que la telefonía se encienda sola: si el puente
    está en marcha, sus herramientas aparecen; si no, el agente arranca
    exactamente como antes de que la telefonía existiera, con una línea en el
    log y ni un traceback.

    Es una comprobación barata —un `GET /salud` por un socket unix— y ocurre
    antes de cargar Whisper y Piper, así que no retrasa nada perceptible.
    """
    if settings.telefonia_modo is ModoTelefonia.OFF:
        return None

    cliente = ClienteTelefonia(settings.telefonia_socket, settings.telefonia_timeout_secs)

    if settings.telefonia_modo is ModoTelefonia.ON:
        # A propósito no se sondea: el modo `on` existe para poder arrancar el
        # puente DESPUÉS del agente. Las herramientas están, y mientras el
        # puente no conteste devuelven un error claro.
        logger.info("Telefonía forzada a 'on'; las herramientas de teléfono estarán disponibles")
        return cliente

    if await cliente.disponible():
        logger.info(f"Telefonía disponible en {settings.telefonia_socket}")
        return cliente

    logger.info(
        "Telefonía no disponible; el agente arranca sin herramientas de teléfono. "
        "Si esperabas tenerlas, comprueba: systemctl --user status voice-agent-telefonia"
    )
    return None


async def construir_worker(
    settings: Settings, runtime: RuntimeConfig, telefonia: ClienteTelefonia | None
) -> tuple[PipelineWorker, list[MCPClient], MicrophoneGate | None]:
    """Construye el pipeline completo listo para ejecutar.

    Es una corrutina porque conectar con los servidores MCP lo es, y hay que
    hacerlo aquí dentro: sus esquemas tienen que estar antes de crear el
    contexto.

    Args:
        settings: Configuración del agente.
        runtime: Prompt, alma, muletillas y demás ajustes que gobierna el panel.
            Se pasa explícitamente en vez de leerse aquí para que los tests
            puedan montar un pipeline con la configuración que quieran.
        telefonia: El cliente del puente de telefonía, o `None` si este agente
            no tiene teléfono. Se recibe hecho en vez de sondearse aquí porque
            su vida es más larga que la del pipeline: la sala puede montarse y
            desmontarse con cada conexión de la tarjeta de sonido, y el
            teléfono no tiene por qué enterarse.

    Returns:
        El `PipelineWorker`, los clientes MCP abiertos —que quien llame tiene
        que cerrar al terminar; ver el docstring de `voice_agent.mcp` para por
        qué el cierre no puede vivir dentro de un manejador de evento— y la
        compuerta de micrófono (o `None` en el perfil headset), que la
        telefonía retiene durante las llamadas.
    """
    settings.apply_model_cache_env()

    # Comprobación temprana. Arrancar el agente cuesta unos cuarenta segundos
    # entre abrir el índice del RAG y cargar Whisper y Piper; descubrir después
    # de todo eso que falta una variable de entorno es una pérdida de tiempo
    # gratuita. Se valida aquí lo que se puede validar sin coste.
    settings.require_llm_api_key()

    # --- Recursos compartidos ------------------------------------------------
    # Se construyen una sola vez, antes del pipeline: cargar el modelo de
    # embeddings y abrir el índice cuesta varios segundos y no puede pasar en
    # mitad de una conversación.
    recursos = AppResources(settings=settings, retriever=Retriever(settings), telefonia=telefonia)

    # --- Servicios -----------------------------------------------------------
    # La compuerta se crea antes que el transporte porque va instalada dentro,
    # como filtro de entrada. Ver audio_gate.py para el porqué de todo esto.
    gate = build_microphone_gate(settings)
    transport = build_transport(settings, gate)

    stt = build_stt(settings)
    llm = build_llm(settings)
    tts = build_tts(settings)

    # Las muletillas van DESPUÉS del servicio de síntesis, y el orden importa:
    # es `build_tts` quien descarga la voz si falta. Construir antes el banco
    # hacía que, al cambiar TTS_VOICE por una voz no descargada, el arranque
    # muriera con un "No such file or directory" antes de que nadie la bajase.
    # El banco sabe descargarla por su cuenta de todos modos, pero así no se
    # intenta dos veces. Ver fillers.py.
    muletillas = None
    if settings.filler_enabled:
        banco = FillerBank(settings)
        banco.preparar(runtime.prompt.muletillas)
        muletillas = FillerProcessor(banco, settings)

    # --- Contexto e historial ------------------------------------------------
    # Al pasar las herramientas aquí, Pipecat registra solo sus manejadores: no
    # hace falta llamar a `llm.register_function` para cada una.
    #
    # `prompt_sistema_efectivo` es prompt + alma ya combinados; en el panel son
    # dos campos separados para poder cambiar el carácter del agente sin tocar
    # sus reglas de funcionamiento.
    herramientas = herramientas_activas(
        runtime.herramientas_desactivadas, incluir_telefonia=telefonia is not None
    )

    # Los servidores MCP se conectan aquí y no antes ni después: `register_tools`
    # necesita el servicio LLM, que acaba de construirse, y sus esquemas tienen
    # que entrar en el `LLMContext` de abajo. Un servidor que falle se anota y se
    # sigue; ver el docstring de voice_agent.mcp.
    mcp = await iniciar_clientes(runtime, llm)
    if mcp.esquemas:
        herramientas = [*herramientas, *mcp.esquemas]

    # El prompt base NO menciona el teléfono. `docs/herramientas.md` avisa de
    # que anunciar una herramienta que no existe hace que el modelo diga
    # tranquilamente que la ha usado, así que el párrafo se añade solo cuando el
    # sondeo ha confirmado que hay puente. Sin telefonía, el prompt es byte a
    # byte el de siempre.
    prompt = runtime.prompt.prompt_sistema_efectivo
    if telefonia is not None:
        prompt += PROMPT_TELEFONIA

    contexto = LLMContext(
        messages=[{"role": "system", "content": prompt}],
        tools=herramientas,
    )

    # Los hooks van en dos puntos distintos porque los frames que les interesan
    # están en sitios distintos; si no hay ninguno configurado, no se inserta
    # nada. Ver el docstring de hooks.py.
    hooks_entrada, hooks_salida = construir_procesadores(runtime)

    agregador = LLMContextAggregatorPair(
        contexto,
        user_params=LLMUserAggregatorParams(
            # El VAD va aquí, no en el transporte. Ver build_vad() para el
            # porqué y para la trampa que esconde equivocarse.
            vad_analyzer=build_vad(settings),
            user_turn_strategies=build_turn_strategies(settings),
            user_mute_strategies=build_mute_strategies(settings),
        ),
        assistant_params=LLMAssistantAggregatorParams(),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            # Antes del agregador: es el último punto en el que reescribir o
            # descartar la transcripción tiene efecto, porque el siguiente paso
            # ya la mete en el historial de la conversación.
            *([hooks_entrada] if hooks_entrada else []),
            agregador.user(),
            llm,
            *([hooks_salida] if hooks_salida else []),
            # Entre el LLM y el TTS: desde aquí ve el primer token del modelo
            # (que el TTS consume), y su audio pregrabado cruza el TTS sin
            # resintetizarse. Ver el docstring de fillers.py.
            *([muletillas] if muletillas else []),
            tts,
            transport.output(),
            # Acciona la compuerta al oír los frames de habla del bot, que los
            # emite la salida de audio. Si no hay compuerta (perfil headset) no
            # se inserta nada.
            *([BotSpeechGateController(gate)] if gate else []),
            agregador.assistant(),
        ]
    )

    _publicar_estado(settings, runtime, herramientas, mcp)

    worker = PipelineWorker(
        pipeline,
        # Sin esto, Pipecat cancela el pipeline tras cinco minutos sin frames de
        # habla y el proceso termina —limpiamente, sin error—, de modo que el
        # servicio aparece como "inactive" sin nada en los logs que lo explique.
        # Tiene sentido para un bot de llamada, que debe colgar cuando la
        # conversación muere; aquí es justo lo contrario, porque el agente ha de
        # seguir escuchando indefinidamente aunque nadie le hable.
        idle_timeout_secs=None,
        params=PipelineParams(
            audio_in_sample_rate=settings.audio_sample_rate,
            audio_out_sample_rate=settings.audio_sample_rate,
            # Las métricas son las que hacen visible dónde se va el tiempo:
            # cada servicio informa de su tiempo hasta el primer byte y de su
            # tiempo total de proceso. Sin esto, "el agente va lento" no es
            # accionable. Ver docs/rendimiento.md.
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        # Esto es lo que hace que las herramientas puedan llegar al RAG sin
        # variables globales: llega intacto a cada FunctionCallParams.
        app_resources=recursos,
    )
    return worker, mcp.clientes, gate


def _publicar_estado(
    settings: Settings,
    runtime: RuntimeConfig,
    herramientas: Sequence[FunctionSchema | DirectFunction],
    mcp: ResultadoMCP,
) -> None:
    """Deja por escrito qué acabó cargando el agente, para que lo lea el panel.

    El panel corre en otra imagen, sin Pipecat, así que no puede calcular el
    esquema JSON de una herramienta ni sondear un servidor MCP de tipo stdio.
    Publicarlo desde aquí resulta además ser mejor que calcularlo allí: enseña
    lo que el agente tiene cargado **de verdad** en vez de lo que debería tener
    según la base de datos, y cuando ambas cosas no coinciden —que es justo
    cuando uno abre el panel— la diferencia es el diagnóstico.
    """
    # De qué servidor MCP vino cada herramienta, para que el panel lo distinga
    # de las locales sin tener que adivinarlo por el nombre.
    origen = {
        nombre: estado_srv.nombre
        for estado_srv in mcp.estados
        for nombre in estado_srv.herramientas
    }

    instantanea = ruta_snapshot_settings(settings.data_dir)
    estado = EstadoArranque.ahora(
        version_agente=__version__,
        runtime_version=runtime.version,
        snapshot_settings_mtime=instantanea.stat().st_mtime if instantanea.is_file() else None,
        tiene_llm_api_key=(
            settings.gemini_api_key is not None or settings.groq_api_key is not None
        ),
        tiene_deepgram_api_key=settings.deepgram_api_key is not None,
        herramientas=[
            HerramientaExpuesta(
                nombre=nombre_de(h),
                origen=origen.get(nombre_de(h), ORIGEN_LOCAL),
                descripcion=str(esquema_de(h).get("description", "")),
                esquema=esquema_de(h),
            )
            for h in herramientas
        ],
        mcp=mcp.estados,
        hooks_activos=[h.nombre for h in runtime.hooks if h.habilitado],
    )
    escribir_estado(settings.data_dir, estado)


async def _vigilar_tarjetas(
    worker: PipelineWorker,
    tarjetas: frozenset[str],
    *,
    intervalo_secs: float = INTERVALO_SONDEO_SECS,
) -> None:
    """Detiene el pipeline de la sala si cambia el juego de tarjetas de sonido.

    PortAudio no se entera de que su tarjeta ha desaparecido: la lectura del
    micrófono empieza a fallar con errores opacos y el pipeline queda en un
    estado indefinido. Vigilar `/proc/asound` da un diagnóstico inequívoco y
    permite parar de forma ordenada, en un par de segundos, en vez de esperar
    a que algo reviente.

    Args:
        worker: El worker de la sala a cancelar cuando pase.
        tarjetas: El juego de tarjetas presente cuando arrancó el pipeline.
        intervalo_secs: Cada cuánto se comprueba.
    """
    while tarjetas_alsa() == tarjetas:
        await asyncio.sleep(intervalo_secs)
    logger.warning("La tarjeta de sonido ha cambiado; se detiene el audio de la sala")
    await worker.cancel(reason="tarjeta de sonido desconectada")


async def _conversar_en_sala(
    settings: Settings,
    runtime: RuntimeConfig,
    telefonia: ClienteTelefonia | None,
    tarjetas: frozenset[str],
    sala: SalaActual | None = None,
) -> bool:
    """Monta el pipeline de la sala y conversa hasta que termine o se vaya la tarjeta.

    Args:
        settings: Configuración del agente.
        runtime: Configuración del panel, ya cargada.
        telefonia: Cliente del puente de telefonía, si este agente tiene.
        tarjetas: Las tarjetas presentes cuando se resolvieron los dispositivos.
        sala: El puente con el planificador de tareas: aquí se publica el
            worker mientras la sala está montada, para que las misiones de
            sala tengan dónde hablar. Opcional para no obligar a los tests.

    Returns:
        True si la sesión terminó porque la tarjeta cambió —y hay que volver a
        esperarla—, False si terminó por cualquier otra causa y toca salir.
    """
    try:
        worker, clientes_mcp, gate = await construir_worker(settings, runtime, telefonia)
    except AudioDeviceError as e:
        # La tarjeta estaba al resolver y se fue antes de terminar el montaje.
        logger.warning(f"La tarjeta de sonido desapareció durante el montaje: {e}")
        return True

    if sala is not None:
        sala.worker, sala.gate = worker, gate

    saludo = runtime.prompt.saludo_inicial

    @worker.event_handler("on_pipeline_started")  # type: ignore[untyped-decorator]
    async def _al_arrancar(_worker: PipelineWorker, _frame: StartFrame) -> None:
        """Hace que el agente hable primero.

        Sin esto arrancaría en silencio y no habría forma de saber si está vivo
        y escuchando. Con la tarjeta recién conectada cumple además de
        confirmación: si el agente saluda, el enchufe ha funcionado.

        Se envía un `TTSSpeakFrame` con un texto fijo en lugar de pedirle al
        modelo que improvise un saludo. Ver el comentario de `SALUDO_INICIAL`:
        la versión que usaba una instrucción de sistema provocaba un bucle de
        saludos. `append_to_context=True` deja constancia en el historial de lo
        que el agente dijo, para que la conversación siga siendo coherente.
        """
        logger.info("Pipeline arrancado. Saludando...")
        await _worker.queue_frames([TTSSpeakFrame(text=saludo, append_to_context=True)])

    # Los anuncios de llamada entrante corren en su propia tarea, escuchando el
    # canal de eventos del puente. Si no hay telefonía no se crea nada. Viven
    # lo que vive la sala porque hablan por su altavoz: sin sala no hay dónde
    # anunciar, y el contestador —que es de la llamada, no de la sala— sigue
    # aparte en `ejecutar`.
    anuncios: asyncio.Task[None] | None = None
    if telefonia is not None:
        anuncios = asyncio.create_task(
            escuchar_telefonia(
                worker,
                telefonia,
                settings.telefonia_anuncio,
                # El saludo al contestar es del pipeline de la LLAMADA (fase
                # 2): usa el mismo texto, y dejarlo también aquí hacía sonar
                # por el altavoz de la sala un saludo indistinguible del de la
                # llamada — se depuró como si fuera un cruce de audio. Vacío
                # desactiva la voz, no el aviso al modelo.
                "",
                # La compuerta se retiene mientras dura una llamada: la
                # persona habla por teléfono, no con la sala. En el perfil
                # headset no hay compuerta y no se retiene nada.
                gate,
            )
        )

    vigilante = asyncio.create_task(_vigilar_tarjetas(worker, tarjetas))

    logger.info("Agente en marcha. Habla por el micrófono. Ctrl+C para salir.")
    runner = WorkerRunner()
    try:
        await runner.run(worker)
    finally:
        # Sin sala montada, las misiones de sala vuelven a aplazarse.
        if sala is not None:
            sala.worker, sala.gate = None, None
        # Las cancelaciones van en el `finally` y no en un manejador del
        # worker, por la misma razón que el cierre de MCP de abajo: los
        # manejadores corren en otra tarea de asyncio.
        vigilante.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await vigilante
        if anuncios is not None:
            anuncios.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await anuncios
        # El cierre va aquí y no en un manejador `on_pipeline_finished` porque
        # los transportes de `mcp` usan grupos de tareas de anyio, que exigen
        # entrar y salir desde la misma tarea de asyncio. Desde un manejador del
        # worker —otra tarea— revienta con "Attempted to exit cancel scope in a
        # different task". Ver el docstring de voice_agent.mcp.
        await cerrar_clientes(clientes_mcp)

    # Se decide por el estado real de las tarjetas y no por quién canceló:
    # si la tarjeta murió, el pipeline pudo caerse solo antes de que el
    # vigilante lo viera, y aun así lo que toca es volver a esperarla.
    return tarjetas_alsa() != tarjetas


async def ejecutar(settings: Settings) -> None:
    """Arranca el agente y conversa hasta que se interrumpa el proceso.

    La telefonía y el audio de la sala tienen vidas separadas, y este es el
    sitio donde se ve: el contestador atiende por el enchufe Bluetooth y no
    necesita tarjeta de sonido, así que arranca siempre y no se toca después.
    La sala, en cambio, vive en un bucle *enchufar-conversar-desenchufar*: se
    espera a que la tarjeta USB exista, se conversa hasta que desaparezca, y
    se vuelve a esperar. Conectar o quitar la tarjeta es así una operación
    normal, no un fallo: sin ella el agente queda en modo solo-teléfono.

    Args:
        settings: Configuración del agente.
    """
    # La configuración del panel se lee una sola vez, al arrancar. Aplicar un
    # cambio pasa por reiniciar el servicio, y es deliberado: recargar en
    # caliente el prompt o las muletillas obligaría a invalidar el historial de
    # la conversación y el banco de audio pregrabado a mitad de un turno.
    runtime = cargar_runtime(settings.data_dir)
    log_startup_banner(settings, runtime)

    # Sin tarjeta, el agente puede pasarse horas esperando: los errores de
    # configuración tienen que salir ANTES de ponerse a esperar, no cuando por
    # fin alguien enchufa la tarjeta.
    settings.require_llm_api_key()

    telefonia = await _preparar_telefonia(settings)

    # El planificador de tareas vive aquí, como el contestador: una misión de
    # llamada no necesita tarjeta de sonido. Las de sala hablan a través de
    # `sala`, que `_conversar_en_sala` rellena mientras el pipeline existe.
    sala = SalaActual()
    misiones = MisionesLlamada()

    audio_llamadas: ClienteAudioSCO | None = None
    if telefonia is not None:
        # El audio de las llamadas (fase 2). El cliente reconecta solo, así
        # que da igual si el puente está en modo `agente` o no: sin socket de
        # audio, simplemente no llega ninguna llamada que atender.
        # Los modelos del pipeline de llamada cargan AHORA, no al descolgar:
        # ver ServiciosDeLlamada.
        servicios_llamada = ServiciosDeLlamada(settings, runtime)
        servicios_llamada.precargar()

        async def _al_llegar_audio(sock: socket.socket, metadatos: dict[str, Any]) -> None:
            # ¿Este audio es de una llamada que marcó el planificador? La
            # correlación se resuelve dentro de `atender_llamada`, después de
            # arrancar el núcleo del transporte; ver su docstring.
            await atender_llamada(
                sock,
                metadatos,
                settings,
                runtime,
                servicios_llamada,
                misiones=misiones,
                telefonia=telefonia,
            )

        audio_llamadas = ClienteAudioSCO(
            settings.telefonia_socket.with_name("telefonia-audio.sock"),
            _al_llegar_audio,
        )
        audio_llamadas.arrancar()

    programador = asyncio.create_task(
        ProgramadorTareas(settings, sala, telefonia, misiones).correr()
    )

    try:
        while True:
            tarjetas = await esperar_dispositivos(settings)
            if not await _conversar_en_sala(settings, runtime, telefonia, tarjetas, sala):
                break
    finally:
        programador.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await programador
        if audio_llamadas is not None:
            await audio_llamadas.parar()
