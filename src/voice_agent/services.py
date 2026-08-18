"""Construcción de los servicios de voz (STT, LLM, TTS) y del transporte.

Todo el cableado con Pipecat vive aquí, aislado de la lógica del agente. La
ventaja de concentrarlo en una factoría es que cambiar de proveedor —por
ejemplo, pasar la transcripción de local a la nube— es cambiar una variable de
entorno, no reescribir el pipeline.
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.services.llm_service import LLMService
from pipecat.services.piper.tts import PiperTTSService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.turns.user_mute.always_user_mute_strategy import AlwaysUserMuteStrategy
from pipecat.turns.user_mute.base_user_mute_strategy import BaseUserMuteStrategy
from pipecat.turns.user_start.transcription_user_turn_start_strategy import (
    TranscriptionUserTurnStartStrategy,
)
from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from voice_agent.audio_devices import resolve_device_indices
from voice_agent.audio_gate import MicrophoneGate
from voice_agent.logging import suppressed_stderr
from voice_agent_core.config import LLMBackend, Settings, STTBackend


class TunedWhisperSTTService(WhisperSTTService):
    """`WhisperSTTService` con la inferencia ajustada a una CPU ARM sin GPU.

    El servicio que trae Pipecat construye el modelo con
    ``WhisperModel(name, device=..., compute_type=...)`` y llama a
    ``transcribe(audio, language=...)`` sin más parámetros. En un x86 con GPU
    eso está bien; en el RK3399 deja dos cosas importantes sin controlar:

    * **El número de hilos.** Sin fijarlo, CTranslate2 reparte el trabajo entre
      los seis núcleos. Como cuatro de ellos son Cortex-A53 lentos, el conjunto
      avanza al ritmo del más lento y el resultado es *peor* que usar solo los
      dos Cortex-A72.
    * **Los parámetros de decodificación.** faster-whisper usa por defecto
      ``beam_size=5`` y ``condition_on_previous_text=True``. Lo primero
      multiplica el coste de decodificar; lo segundo, en frases sueltas de
      conversación, favorece que el modelo entre en bucles de repetición.
      Además Pipecat ya ha segmentado el audio con Silero, así que el VAD
      interno de Whisper sería trabajo duplicado.

    Esta subclase reimplementa la carga del modelo para poder fijar todo eso.
    """

    def __init__(self, *, cpu_threads: int, beam_size: int, **kwargs: Any) -> None:
        """Inicializa el servicio.

        Args:
            cpu_threads: Hilos de inferencia para CTranslate2.
            beam_size: Anchura del haz de búsqueda al decodificar.
            **kwargs: Argumentos que se pasan tal cual a `WhisperSTTService`.
        """
        self._cpu_threads = cpu_threads
        self._beam_size = beam_size
        super().__init__(**kwargs)

    def _load(self) -> None:
        """Carga el modelo Whisper con los ajustes propios de esta placa."""
        from faster_whisper import WhisperModel

        model_name = self._settings.model  # type: ignore[attr-defined]
        logger.info(
            f"Cargando Whisper '{model_name}' "
            f"({self._compute_type}, {self._cpu_threads} hilos, beam={self._beam_size})..."
        )
        model = WhisperModel(
            str(model_name),
            device=self._device,
            compute_type=self._compute_type,
            cpu_threads=self._cpu_threads,
            num_workers=1,
        )

        # `run_stt` de la clase base llama a `transcribe(audio, language=...)`.
        # En vez de reescribir ese método —lo que nos ataría a sus detalles
        # internos y se rompería al actualizar Pipecat— envolvemos `transcribe`
        # para inyectar nuestros valores por defecto. Se usa `setdefault` para
        # que cualquier parámetro que Pipecat pase explícitamente siga ganando.
        original_transcribe = model.transcribe

        def transcribe(audio: Any, **kwargs: Any) -> Any:
            kwargs.setdefault("beam_size", self._beam_size)
            kwargs.setdefault("condition_on_previous_text", False)
            kwargs.setdefault("vad_filter", False)
            return original_transcribe(audio, **kwargs)

        model.transcribe = transcribe
        self._model = model
        logger.info("Whisper cargado.")


def build_stt(settings: Settings, *, sample_rate: int | None = None) -> STTService:
    """Construye el servicio de transcripción según la configuración.

    Args:
        settings: Configuración del agente.
        sample_rate: Frecuencia del audio que recibirá, si no es la de la
            sala. El pipeline de la llamada corre a 8 kHz (CVSD) y Deepgram
            transcribe el flujo crudo: declararle la frecuencia equivocada
            hace que la voz le llegue a media velocidad y no entienda nada —
            el VAD detectaba turnos y no salía ni una transcripción.

    Returns:
        El servicio STT listo para insertar en el pipeline.

    Raises:
        ValueError: Si se pide Deepgram sin haber configurado su clave.
    """
    if settings.stt_backend is STTBackend.DEEPGRAM:
        if settings.deepgram_api_key is None:
            raise ValueError(
                "STT_BACKEND=deepgram requiere definir DEEPGRAM_API_KEY en .env.\n"
                "Consíguela en https://console.deepgram.com"
            )
        # Import diferido: el paquete de Deepgram solo hace falta en esta rama,
        # y así el arranque con Whisper no paga por importarlo.
        from pipecat.services.deepgram.stt import DeepgramSTTService

        logger.info(f"STT: Deepgram / {settings.deepgram_model} (streaming en la nube)")
        return DeepgramSTTService(
            api_key=settings.deepgram_api_key.get_secret_value(),
            # Deepgram transcribe sobre el flujo de audio directamente, así que
            # necesita saber a qué frecuencia se lo mandamos.
            sample_rate=sample_rate or settings.audio_sample_rate,
            # Pipecat trae 0.35 s, calibrado suponiendo VAD stop_secs=0.2. Se
            # declara el valor medido en esta placa para que la red de
            # seguridad de la estrategia de fin de turno no se colapse a cero
            # cuando se usa el perfil de altavoz, que necesita un stop_secs
            # mayor.
            ttfs_p99_latency=settings.deepgram_ttfs_p99,
            settings=DeepgramSTTService.Settings(
                model=settings.deepgram_model,
                language=Language.ES,
                # Puntuación y formato inteligente: sin esto llega todo en
                # minúsculas y sin signos, lo que empeora la comprensión del
                # LLM y, sobre todo, hace que las preguntas no se distingan de
                # las afirmaciones.
                punctuate=True,
                smart_format=True,
                # Resultados intermedios: no los usamos para nada visible, pero
                # permiten que la estrategia de fin de turno reaccione antes.
                interim_results=True,
            ),
        )

    logger.info(f"STT: Whisper local ({settings.whisper_model})")
    return TunedWhisperSTTService(
        cpu_threads=settings.whisper_cpu_threads,
        beam_size=settings.whisper_beam_size,
        device="cpu",
        compute_type=settings.whisper_compute_type,
        # Latencia P99 desde que se deja de hablar hasta que llega la
        # transcripción final. La usa `SpeechTimeoutUserTurnStopStrategy` como
        # red de seguridad para no cerrar el turno antes de que Whisper haya
        # entregado el texto. El valor por defecto de Pipecat es 1 s, pensado
        # para servicios en la nube; aquí la mediana medida es de unos 3 s
        # (ver docs/rendimiento.md), así que dejarlo en 1 s haría que la
        # estrategia registrara un aviso en cada intervención.
        ttfs_p99_latency=settings.whisper_ttfs_p99,
        settings=WhisperSTTService.Settings(
            model=settings.whisper_model,
            language=Language.ES,
            # Descarta segmentos que Whisper considera probablemente silencio.
            # Es la defensa principal contra las alucinaciones típicas del
            # modelo ("Subtítulos por la comunidad de Amara.org") cuando le
            # llega un fragmento sin habla.
            no_speech_prob=0.5,
        ),
    )


def build_llm(settings: Settings) -> LLMService[Any]:
    """Construye el servicio de LLM según el backend configurado.

    Solo hay dos cableados: Gemini Flash (nivel gratuito de AI Studio) o
    Llama vía Groq (nivel gratuito).

    Gemini va por su SDK nativo (`GoogleLLMService`), no por el endpoint
    OpenAI-compatible de AI Studio. No es una preferencia estética: el shim
    de compatibilidad emite por streaming tool-calls fantasma con el nombre
    vacío; Pipecat los guarda en el historial y Gemini rechaza después su
    propio historial con un 400 ("function_response.name: Name cannot be
    empty") en TODA petición posterior — el agente queda mudo a mitad de
    llamada. Se depuró en una conversación real; ver docs/arquitectura.md.

    Args:
        settings: Configuración del agente.

    Returns:
        El servicio LLM listo para insertar en el pipeline.
    """
    if settings.llm_backend is LLMBackend.GROQ:
        # Import diferido, como Deepgram: solo se paga en esta rama.
        from pipecat.services.groq.llm import GroqLLMService

        logger.info(f"LLM: Groq / {settings.groq_model}")
        return GroqLLMService(
            api_key=settings.require_llm_api_key(),
            settings=GroqLLMService.Settings(
                model=settings.groq_model,
                temperature=settings.llm_temperature,
                max_completion_tokens=settings.llm_max_tokens,
            ),
        )

    from pipecat.services.google.llm import GoogleLLMService

    logger.info(f"LLM: Google AI Studio / {settings.gemini_model}")
    return GoogleLLMService(
        api_key=settings.require_llm_api_key(),
        settings=GoogleLLMService.Settings(
            model=settings.gemini_model,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            # Gemini 2.5 "piensa" por defecto, y ese razonamiento se cobra en
            # tokens Y en segundos antes del primer token — medido: con un
            # presupuesto corto la respuesta llegaba vacía porque se fue
            # entera en pensar. En un agente de voz la latencia del primer
            # token es la conversación, así que se apaga.
            thinking=GoogleLLMService.ThinkingConfig(thinking_budget=0),
        ),
    )


def build_tts(settings: Settings, *, sample_rate: int | None = None) -> TTSService:
    """Construye el servicio de síntesis de voz.

    Piper se ejecuta dentro del propio proceso mediante ONNX Runtime, sin
    servidor aparte ni llamadas de red. Si la voz no está descargada, el
    constructor la baja; `python -m voice_agent.models` la deja preparada de
    antemano para que el arranque sea inmediato.

    Args:
        settings: Configuración del agente.
        sample_rate: Frecuencia de salida, si no es la de la sala; el
            pipeline de la llamada pide la del SCO (8 kHz).

    Returns:
        El servicio TTS listo para insertar en el pipeline.
    """
    logger.info(f"TTS: Piper / {settings.tts_voice}")
    settings.ensure_directories()
    return PiperTTSService(
        download_dir=settings.piper_dir,
        settings=PiperTTSService.Settings(voice=settings.tts_voice),
        sample_rate=sample_rate or settings.audio_sample_rate,
    )


def build_vad(settings: Settings) -> SileroVADAnalyzer:
    """Construye el analizador de actividad de voz.

    .. warning::
        En Pipecat 1.x el VAD se le pasa al **agregador de contexto**
        (``LLMUserAggregatorParams(vad_analyzer=...)``), no al transporte.

        Es un cambio respecto a las versiones 0.0.x, donde iba en
        ``TransportParams``, y es una trampa desagradable: ``TransportParams``
        es un modelo de Pydantic que **ignora los campos desconocidos**, así
        que pasarle ``vad_analyzer=...`` no da ningún error. Simplemente se
        descarta en silencio, el agente arranca con normalidad y nunca detecta
        que alguien está hablando. Lo detectó `mypy`, no una prueba.

    Args:
        settings: Configuración del agente.

    Returns:
        El analizador, con los umbrales derivados del perfil de audio.
    """
    return SileroVADAnalyzer(
        sample_rate=settings.audio_sample_rate,
        params=VADParams(
            confidence=settings.effective_vad_confidence,
            start_secs=settings.effective_vad_start_secs,
            stop_secs=settings.effective_vad_stop_secs,
            min_volume=settings.effective_vad_min_volume,
        ),
    )


def build_microphone_gate(settings: Settings) -> MicrophoneGate | None:
    """Crea la compuerta que silencia el micrófono mientras el agente habla.

    Solo tiene sentido en el perfil `speaker`, donde el micrófono capta la voz
    del propio agente. Con auriculares no hay nada que filtrar y filtrarlo
    impediría interrumpir, que es justo lo que ese perfil quiere permitir.

    Args:
        settings: Configuración del agente.

    Returns:
        La compuerta, o None si el perfil permite interrupciones.
    """
    if settings.allow_interruptions:
        return None
    return MicrophoneGate(hangover_secs=settings.mic_gate_hangover_secs)


def build_transport(settings: Settings, gate: MicrophoneGate | None = None) -> LocalAudioTransport:
    """Construye el transporte de audio local.

    Args:
        settings: Configuración del agente.
        gate: Compuerta de micrófono opcional. Se instala como filtro de
            entrada, de modo que actúa **antes** del VAD.

    Returns:
        El transporte configurado.
    """
    input_index, output_index = resolve_device_indices(settings)

    params = LocalAudioTransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=settings.audio_sample_rate,
        audio_out_sample_rate=settings.audio_sample_rate,
        audio_in_channels=1,
        audio_out_channels=1,
        input_device_index=input_index,
        output_device_index=output_index,
        audio_in_filter=gate,
    )

    # PortAudio sondea sus back-ends al construirse; ver logging.suppressed_stderr.
    with suppressed_stderr():
        return LocalAudioTransport(params)


def build_turn_strategies(settings: Settings) -> UserTurnStrategies:
    """Define cuándo empieza y cuándo termina el turno del usuario.

    Hay que declararlas explícitamente. La estrategia de parada que Pipecat 1.6
    usa por defecto es ``TurnAnalyzerUserTurnStopStrategy(LocalSmartTurnAnalyzerV3)``,
    un modelo semántico de fin de turno que depende de PyTorch y torchaudio:
    en aarch64 son cerca de un gigabyte de dependencias y un consumo de memoria
    que esta placa no puede permitirse. En su lugar usamos
    ``SpeechTimeoutUserTurnStopStrategy``, que cierra el turno por silencio
    medido con el VAD. Es menos "inteligente" —no distingue una pausa para
    pensar del final de la frase— pero cuesta cero.

    Args:
        settings: Configuración del agente.

    Returns:
        Las estrategias de inicio y parada de turno.
    """
    return UserTurnStrategies(
        start=[
            # El VAD abre el turno en cuanto detecta voz...
            VADUserTurnStartStrategy(),
            # ...y la llegada de una transcripción también, por si el VAD se
            # perdió el arranque de la frase.
            TranscriptionUserTurnStartStrategy(),
        ],
        stop=[
            SpeechTimeoutUserTurnStopStrategy(
                user_speech_timeout=settings.user_speech_timeout,
            )
        ],
    )


def build_mute_strategies(settings: Settings) -> list[BaseUserMuteStrategy]:
    """Decide si se ignora la entrada del usuario mientras el agente habla.

    Este es el mecanismo que implementa el perfil de audio:

    * ``headset``: lista vacía. La entrada nunca se silencia, así que hablar
      encima del agente lo interrumpe (*barge-in*), que es la interacción
      natural en una conversación.
    * ``speaker``: ``AlwaysUserMuteStrategy``. Con altavoz y micrófono abiertos
      en la misma sala, el micrófono capta la voz sintetizada del propio
      agente; sin silenciar la entrada, el VAD la tomaría por habla del usuario
      y el agente se interrumpiría a sí mismo en bucle. Silenciar mientras
      habla convierte la conversación en semidúplex: no se le puede
      interrumpir, pero funciona.

    Pipecat no incluye cancelación de eco acústico libre —solo integraciones
    comerciales (Krisp, Koala) y `rnnoise`, que suprime ruido pero no eco—, así
    que el semidúplex es la solución correcta sin hardware adicional.

    Args:
        settings: Configuración del agente.

    Returns:
        Las estrategias de silenciado aplicables.
    """
    if settings.allow_interruptions:
        return []
    return [AlwaysUserMuteStrategy()]  # type: ignore[no-untyped-call]
