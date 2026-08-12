"""Configuración del agente, tipada y validada a partir de variables de entorno.

Toda la configuración vive aquí y se carga con `pydantic-settings` desde el
fichero `.env` (o desde el entorno, que tiene prioridad). Eso da tres ventajas
sobre leer `os.environ` disperso por el código:

1. **Validación temprana**: un valor mal escrito falla al arrancar, con un
   mensaje claro, en vez de reventar a mitad de una conversación.
2. **Un único sitio que documentar**: `.env.example` y esta clase son la
   referencia completa de qué se puede ajustar.
3. **Testabilidad**: los tests construyen `Settings(...)` con valores
   explícitos, sin tocar el entorno del proceso.

Las rutas derivadas (`chroma_dir`, `models_dir`, ...) se exponen como
propiedades para que el resto del código nunca componga rutas a mano.

## Orden de prioridad, y por qué la instantánea gana al entorno

Desde que existe el panel hay una fuente más: el fichero que este escribe en
`<DATA_DIR>/config/settings.json`. Su sitio en la cadena no es un detalle:

    kwargs explícitos  >  instantánea del panel  >  entorno  >  .env  >  secrets

La instantánea va **por encima del entorno** porque la unidad de systemd inyecta
el `.env` del proyecto como variables de entorno reales dentro del contenedor.
Colocarla por debajo dejaría el panel sin efecto sobre las dos docenas de
ajustes que ese fichero define: se guardaría, se exportaría, se reiniciaría, y
no cambiaría nada.

Los kwargs siguen ganando para que los tests puedan construir un `Settings`
explícito sin que nada del sistema de ficheros se cuele.
"""

from __future__ import annotations

import json
import os
import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    JsonConfigSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from voice_agent_core.rutas import ruta_snapshot_desde_entorno

#: Campos que el panel **no** puede tocar, por dos motivos distintos.
#:
#: `data_dir` y `corpus_dir` son rutas *dentro del contenedor* y no coinciden con
#: las del anfitrión. Dejar que se configuraran desde el panel resucitaría la
#: trampa que ya costó una sesión: un `DATA_DIR` relativo que no existe dentro de
#: la imagen y un agente que busca su índice donde no está. Además, la propia
#: instantánea vive bajo `data_dir`, así que permitir cambiarlo desde ella sería
#: circular.
#:
#: Las dos claves de API se quedan fuera para que el panel no pueda leerlas ni
#: escribirlas. Viven en el `.env`, que el contenedor del panel ni siquiera monta.
#:
#: `telefonia_socket_path` se queda fuera por el mismo motivo que `data_dir`:
#: es una ruta que vale distinto dentro y fuera del contenedor, y fijarla desde
#: el panel dejaría al agente buscando el puente donde no está.
CAMPOS_PROTEGIDOS: frozenset[str] = frozenset(
    {
        "data_dir",
        "corpus_dir",
        "gemini_api_key",
        "groq_api_key",
        "deepgram_api_key",
        "turn_credential",
        "web_codigo_acceso",
        "telefonia_socket_path",
    }
)


class PanelSettingsSource(JsonConfigSettingsSource):
    """Lee la instantánea del panel, filtrando lo que no le corresponde.

    Hace dos cosas que la fuente JSON de serie no hace:

    1. **Descarta `CAMPOS_PROTEGIDOS` y claves desconocidas.** `Settings` usa
       `extra="ignore"`, de modo que una clave mal escrita se tiraría en
       silencio; filtrarla aquí, con el modelo delante, permite además avisar.
    2. **No deja que un JSON roto tumbe el arranque.** Devuelve un diccionario
       vacío y lo registra: el agente arranca con la configuración de siempre.
    """

    def _read_file(self, file_path: Path) -> dict[str, Any]:
        """Lee la instantánea sin que un fichero corrupto llegue a lanzar.

        La protección va aquí y no en `__call__` porque la clase base lee el
        fichero **en su constructor**: para cuando alguien llama a la fuente, la
        excepción ya se habría propagado desde `Settings()`. Costó un test rojo
        descubrirlo, así que no muevas esto sin volver a comprobarlo.
        """
        try:
            return super()._read_file(file_path)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            logger.error(
                f"La instantánea de configuración del panel ({file_path}) no se pudo leer: {e}. "
                "Se ignora y se arranca con el entorno y el .env."
            )
            return {}

    def __call__(self) -> dict[str, Any]:
        """Devuelve los ajustes de la instantánea que sí se pueden aplicar."""
        datos = super().__call__()
        conocidos = self.settings_cls.model_fields
        aceptados: dict[str, Any] = {}
        for clave, valor in datos.items():
            if clave in CAMPOS_PROTEGIDOS:
                logger.warning(
                    f"La instantánea del panel trae '{clave}', que está protegido. Se ignora."
                )
            elif clave not in conocidos:
                logger.warning(
                    f"La instantánea del panel trae '{clave}', que no existe. Se ignora."
                )
            else:
                aceptados[clave] = valor
        return aceptados


class AudioProfile(StrEnum):
    """Perfil acústico del puesto de trabajo.

    Determina si el agente puede ser interrumpido mientras habla.

    Attributes:
        HEADSET: Auriculares o headset. El micrófono no oye al altavoz, así que
            se permite *barge-in*: hablar encima del agente lo interrumpe.
        SPEAKER: Altavoz y micrófono abiertos en la misma sala. El micrófono
            oye al altavoz, de modo que permitir interrupciones haría que el
            agente se interrumpiese a sí mismo. Se trabaja en semidúplex.
    """

    HEADSET = "headset"
    SPEAKER = "speaker"


class ModoTelefonia(StrEnum):
    """Cuándo se le ofrecen al modelo las herramientas de teléfono.

    Attributes:
        AUTO: Se sondea el puente al arrancar; si contesta, se activan. Es el
            valor por defecto y hace que la telefonía se encienda sola cuando
            el puente está en marcha, sin tocar configuración.
        ON: Se activan siempre. Sirve para arrancar el puente después del
            agente: las herramientas ya están y devuelven un error claro
            mientras tanto, en vez de no existir.
        OFF: No se activan nunca. El agente queda exactamente como antes de que
            la telefonía existiera.
    """

    AUTO = "auto"
    ON = "on"
    OFF = "off"


class LLMBackend(StrEnum):
    """Proveedor del modelo de lenguaje.

    El reto Clara 2026 restringe el modelo a familias concretas en su
    nivel gratuito (compuerta G3 de la rúbrica: usar otro descalifica). El
    resto del stack es libre.

    Attributes:
        GEMINI: Google Gemini Flash por el endpoint OpenAI-compatible de
            AI Studio, en su nivel gratuito. Es el valor por defecto: mismo
            modelo con el que se calibraron los prompts de este agente.
        GROQ: Meta Llama servido por Groq, también en nivel gratuito. Es el
            plan B para la demo: su límite de peticiones por minuto es más
            holgado que el de Gemini.
    """

    GEMINI = "gemini"
    GROQ = "groq"


class STTBackend(StrEnum):
    """Motor de transcripción de voz a texto.

    Attributes:
        WHISPER: faster-whisper ejecutándose localmente en la CPU de la placa.
            Sin coste ni dependencias externas, a cambio de latencia.
        DEEPGRAM: Transcripción en streaming en la nube. Mucho más rápida y
            precisa, requiere `DEEPGRAM_API_KEY` y tiene coste por minuto.
    """

    WHISPER = "whisper"
    DEEPGRAM = "deepgram"


class Settings(BaseSettings):
    """Configuración completa del agente de voz."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Inserta la instantánea del panel entre los kwargs y el entorno.

        El orden lo explica el docstring del módulo: la instantánea tiene que
        ganar al entorno o el panel no sirve de nada, y los kwargs tienen que
        ganar a la instantánea o los tests dejan de ser herméticos.
        """
        return (
            init_settings,
            PanelSettingsSource(settings_cls, json_file=ruta_snapshot_desde_entorno()),
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )

    # --- LLM ---------------------------------------------------------------
    llm_backend: LLMBackend = Field(
        default=LLMBackend.GEMINI,
        description=(
            "Proveedor del modelo de lenguaje, restringido a los permitidos "
            "por el reto (compuerta G3). `gemini` por defecto; `groq` como "
            "plan B si el nivel gratuito de Gemini se queda corto de "
            "peticiones por minuto durante la demo."
        ),
    )
    gemini_api_key: SecretStr | None = Field(
        default=None,
        description=(
            "Clave gratuita de Google AI Studio (https://aistudio.google.com/apikey). "
            "Es opcional en el modelo para que las utilidades que no hablan "
            "con el LLM —`make ingest`, `make ask`— funcionen sin ella. Su "
            "ausencia se comprueba al construir el servicio LLM."
        ),
    )
    gemini_model: str = Field(
        default="gemini-2.5-flash",
        description=(
            "Modelo de la gama Gemini Flash. Debe soportar function calling y "
            "responder rápido: en un agente de voz la latencia del primer "
            "token es lo que se percibe como 'lentitud'."
        ),
    )
    groq_api_key: SecretStr | None = Field(
        default=None,
        description="Clave gratuita de Groq (https://console.groq.com/keys). Solo si LLM_BACKEND=groq.",
    )
    groq_model: str = Field(
        default="llama-3.3-70b-versatile",
        description="Modelo de la familia Llama servido por Groq, en su nivel gratuito.",
    )
    llm_temperature: float = Field(default=0.6, ge=0.0, le=2.0)
    llm_max_tokens: int = Field(
        default=400,
        gt=0,
        description=(
            "Tope de tokens de respuesta. Se mantiene bajo a propósito: una "
            "respuesta hablada larga es una mala experiencia y además tarda "
            "mucho en sintetizarse."
        ),
    )

    # --- STT ---------------------------------------------------------------
    stt_backend: STTBackend = STTBackend.WHISPER
    whisper_model: str = Field(
        default="tiny",
        description=(
            "Tamaño del modelo Whisper: tiny | base | small | medium. Debe ser "
            "multilingüe (los modelos '.en' no sirven para español). En esta "
            "placa 'tiny' tarda 2.9 s por intervención, 'base' 5.1 s y 'small' "
            "16.6 s, sin que los mayores acierten más: lo que degrada la "
            "transcripción es la brevedad de la frase, no el tamaño del modelo. "
            "Si las frases cortas te fallan, la salida no es subir de modelo "
            "sino STT_BACKEND=deepgram. Ver docs/rendimiento.md."
        ),
    )
    whisper_compute_type: str = Field(
        default="int8",
        description="Cuantización de CTranslate2. int8 es la única opción sensata en ARM sin GPU.",
    )
    whisper_cpu_threads: int = Field(
        default=4,
        gt=0,
        description=(
            "Hilos para la inferencia de CTranslate2. Medido en esta placa: "
            "1 hilo 5.2 s, 2 hilos 3.2 s, 4 hilos 3.0 s, 6 hilos 2.7 s. Se "
            "eligen 4 y no 6 para dejar dos núcleos libres al hilo de audio en "
            "tiempo real, al VAD y al bucle de eventos; los 0.3 s que se "
            "ganarían no compensan arriesgar cortes en la reproducción."
        ),
    )
    whisper_beam_size: int = Field(
        default=1,
        gt=0,
        description=(
            "Anchura del haz de búsqueda al decodificar. faster-whisper usa 5 "
            "por defecto, lo que en esta CPU multiplica el tiempo de "
            "transcripción sin una mejora perceptible en frases cortas de "
            "conversación. 1 (búsqueda voraz) es la opción correcta aquí."
        ),
    )
    whisper_ttfs_p99: float = Field(
        default=3.5,
        gt=0,
        description=(
            "Latencia P99, en segundos, desde el fin del habla hasta la "
            "transcripción final. La usa la estrategia de fin de turno como red "
            "de seguridad. El valor por defecto de Pipecat es 1 s, válido para "
            "servicios en la nube pero no para Whisper en esta CPU, donde la "
            "mediana medida es de unos 3 s. Si cambias WHISPER_MODEL, ajústalo."
        ),
    )
    deepgram_api_key: SecretStr | None = Field(
        default=None,
        description="Clave de Deepgram (https://console.deepgram.com). Solo si STT_BACKEND=deepgram.",
    )
    deepgram_ttfs_p99: float = Field(
        default=0.9,
        gt=0,
        description=(
            "Latencia P99 de Deepgram desde el fin del habla hasta el "
            "transcript final. Pipecat trae 0.35 s, pero ese valor está "
            "calibrado suponiendo VAD_STOP_SECS=0.2; con un stop_secs mayor, "
            "la red de seguridad de la estrategia de fin de turno se colapsa a "
            "cero y Pipecat avisa en cada intervención. Medido desde esta "
            "placa, la ida y vuelta a Deepgram promedia 0.61 s, así que 0.9 s "
            "es un P99 razonable que además deja margen al perfil de altavoz."
        ),
    )
    deepgram_model: str = Field(
        default="nova-3",
        description=(
            "Modelo de Deepgram. 'nova-3' es el más reciente y el que mejor "
            "rinde en español; 'nova-2' es la generación anterior. A "
            "diferencia de Whisper, aquí no hay que elegir entre velocidad y "
            "precisión: la transcripción llega en streaming mientras hablas."
        ),
    )

    # --- Llamada web (WebRTC) ----------------------------------------------
    ice_servers: str = Field(
        default="stun:stun.l.google.com:19302",
        description=(
            "Servidores ICE para la llamada por navegador, separados por "
            "comas. Con el jurado en otra red hace falta añadir un servidor "
            "TURN (el túnel de Cloudflare solo transporta la señalización "
            "HTTP, no la media UDP); sus credenciales van en TURN_USERNAME y "
            "TURN_CREDENTIAL y se aplican a las URLs turn:/turns:."
        ),
    )
    turn_username: str | None = Field(
        default=None,
        description="Usuario del servidor TURN, si `ice_servers` incluye uno.",
    )
    turn_credential: SecretStr | None = Field(
        default=None,
        description="Credencial del servidor TURN, si `ice_servers` incluye uno.",
    )

    # --- Puerta y límites de la llamada web --------------------------------
    # La interfaz de llamada está publicada en internet. Sin nada de esto,
    # cualquiera que dé con la URL consume la cuota gratuita del modelo y de
    # Deepgram, ocupa la única sesión que la placa admite y le cuelga la
    # llamada a quien esté hablando. Ver docs/seguridad.md.
    web_codigo_acceso: SecretStr | None = Field(
        default=None,
        description=(
            "Código que abre la interfaz de llamada. Quien la vaya a usar "
            "recibe el enlace `https://clara.voz-digital.com/?c=CODIGO`: la "
            "aplicación lo canjea por una galleta firmada y no vuelve a pedir "
            "nada en WEB_ACCESO_HORAS. Vacío deja la interfaz ABIERTA, que es "
            "lo cómodo en local y lo inaceptable en la placa. Cambiarlo "
            "invalida todas las galletas emitidas —la clave de firma se deriva "
            "de él—, así que rotarlo echa a quien ya hubiera entrado: no lo "
            "rotes con una llamada en curso."
        ),
    )
    web_acceso_horas: int = Field(
        default=12,
        gt=0,
        le=168,
        description=(
            "Cuánto vale la galleta antes de volver a pedir el código. Doce "
            "horas cubren una jornada de demostración sin obligar a volver al "
            "correo, y caducan antes de que un portátil prestado se convierta "
            "en un acceso permanente."
        ),
    )
    web_acceso_max_intentos: int = Field(
        default=5,
        gt=0,
        description=(
            "Códigos erróneos que se toleran desde una misma IP antes de "
            "bloquearla WEB_ACCESO_BLOQUEO_SECS. Comprobar un código cuesta un "
            "HMAC, pero atender la petición cuesta CPU de la placa, que es la "
            "misma que sintetiza la voz."
        ),
    )
    web_acceso_bloqueo_secs: int = Field(
        default=900,
        ge=0,
        description=(
            "Castigo, en segundos, tras agotar los intentos. Durante el "
            "bloqueo no entra ni el código correcto: si no, bastaría con "
            "seguir probando."
        ),
    )
    web_llamada_max_secs: int = Field(
        default=900,
        ge=0,
        description=(
            "Duración máxima de una llamada por navegador. A falta de "
            "WEB_AVISO_PREVIO_SECS el agente avisa en voz alta, se despide y "
            "cuelga; el resumen y la traza se guardan igual. 0 la deja sin "
            "límite. Existe porque una llamada eterna consume cuota gratuita y "
            "ocupa la única sesión que cabe en la placa."
        ),
    )
    web_aviso_previo_secs: int = Field(
        default=60,
        ge=0,
        description=(
            "Cuánto antes del corte avisa el agente de que se acaba el tiempo. "
            "Cortar sin avisar en una llamada clínica parece una avería."
        ),
    )
    web_aviso_cierre: str = Field(
        default=("Nos queda un minuto de llamada. Si te queda algo por contarme, es buen momento."),
        description=(
            "Lo que se dice al faltar WEB_AVISO_PREVIO_SECS. Es texto fijo y "
            "no pasa por el modelo, a propósito: tiene que sonar sí o sí."
        ),
    )
    web_despedida_cierre: str = Field(
        default=(
            "Se nos acabó el tiempo de esta llamada. Si algo empeora, "
            "comunícate con tu equipo médico. Cuídate."
        ),
        description="Lo último que se dice antes de colgar por tiempo agotado.",
    )
    web_cierre_gracia_secs: float = Field(
        default=8.0,
        gt=0,
        description=(
            "Margen que se le da a la despedida para sonar entera antes de cortar la conexión."
        ),
    )
    web_inactividad_secs: int = Field(
        default=300,
        ge=0,
        description=(
            "Silencio absoluto tras el que se cuelga. Antes no había límite "
            "('el jurado puede quedarse callado pensando'), pero una pestaña "
            "abierta y olvidada mantiene viva la conexión de streaming con "
            "Deepgram, que se factura por tiempo conectado. Cinco minutos "
            "sobran para pensar una respuesta. 0 lo desactiva."
        ),
    )
    web_whatsapp: str = Field(
        default="",
        description=(
            "Número de WhatsApp al que se ofrece escribir a quien llega sin "
            "código, en formato internacional (por ejemplo 573001234567). Se "
            "aceptan el '+', los espacios y los guiones: se limpian solos. "
            "Vacío —lo normal— no enseña ningún botón; sin él, quien llegue "
            "sin código se queda en una pantalla sin salida."
        ),
    )
    web_whatsapp_mensaje: str = Field(
        default=(
            "Hola, estoy en la página de Clara y me gustaría probar la "
            "demostración. ¿Me compartes el código de acceso?"
        ),
        description=(
            "Texto que aparece ya escrito en WhatsApp al pulsar el botón. Se "
            "deja redactado para que quien escribe no tenga que explicarse y "
            "para reconocer de un vistazo de dónde sale el mensaje."
        ),
    )
    web_espera_sesion_secs: float = Field(
        default=8.0,
        ge=0,
        description=(
            "Cuánto se espera a que la sesión anterior demuestre que ya no "
            "está antes de rechazar una llamada nueva con un 409. Existe por "
            "la pestaña recargada: su conexión tarda unos segundos en "
            "delatarse, y sin esta espera quien recarga se comería un error. "
            "0 rechaza en seco."
        ),
    )
    web_llamadas_max_por_hora: int = Field(
        default=12,
        ge=0,
        description=(
            "Llamadas que se le aceptan a una misma IP por hora. Es la red que "
            "queda si el código de acceso se filtra: el enlace compartido no "
            "se convierte en barra libre de minutos de transcripción. 0 lo "
            "desactiva."
        ),
    )

    # --- TTS ---------------------------------------------------------------
    tts_voice: str = Field(
        default="es_ES-davefx-medium",
        description=(
            "Voz de Piper. Alternativas en español: es_MX-claude-high, "
            "es_AR-daniela-high, es_ES-sharvard-medium. Las voces 'high' suenan "
            "mejor pero consumen bastante más CPU en esta placa."
        ),
    )

    # --- Audio -------------------------------------------------------------
    audio_profile: AudioProfile = AudioProfile.HEADSET
    audio_input_device: str = Field(
        default="default",
        description=(
            "Subcadena del nombre del dispositivo de entrada de PortAudio. Se "
            "busca por nombre y no por índice porque los índices no son "
            "estables entre reinicios. 'default' usa la capa ALSA plug definida "
            "en deploy/asound.conf, imprescindible en esta placa porque la "
            "tarjeta USB no acepta 16 kHz de forma nativa."
        ),
    )
    audio_output_device: str = Field(default="default")
    audio_sample_rate: int = Field(
        default=16000,
        description=(
            "Frecuencia de trabajo del pipeline. 16 kHz es obligatorio: Silero "
            "VAD solo admite 8 kHz o 16 kHz, y Whisper trabaja a 16 kHz."
        ),
    )

    vad_confidence: float | None = Field(
        default=None,
        description="Umbral de confianza del VAD. Si es None se deriva del perfil de audio.",
    )
    vad_start_secs: float | None = Field(default=None)
    vad_stop_secs: float | None = Field(default=None)
    vad_min_volume: float | None = Field(
        default=None,
        description=(
            "Volumen mínimo para que el VAD considere que hay voz. Silero decide "
            "por la *forma* del habla, no por su intensidad, así que este umbral "
            "es lo único que distingue la voz del usuario del sonido del propio "
            "agente colándose por el micrófono. Si es None se deriva del perfil."
        ),
    )
    user_speech_timeout: float = Field(
        default=0.6,
        gt=0,
        description=(
            "Silencio, en segundos, tras el que se da por terminado el turno "
            "del usuario. Subirlo da margen para pensar en voz alta; bajarlo "
            "hace al agente más ágil pero más propenso a cortar."
        ),
    )

    filler_enabled: bool = Field(
        default=True,
        description=(
            "Reproduce muletillas pregrabadas ('déjame consultarlo') en los "
            "huecos de espera. No acelera nada, pero evita el silencio que hace "
            "dudar de si el agente te ha oído."
        ),
    )
    filler_delay_secs: float = Field(
        default=1.2,
        gt=0,
        description=(
            "Silencio, tras dejar de hablar, que se tolera antes de soltar una "
            "muletilla genérica de espera. No afecta a las de consulta al RAG, "
            "que suenan de inmediato.\n\n"
            "El valor sale de medir la conversación real en esta placa: el "
            "modelo suelta su primer token en torno a un segundo, y cuando "
            "decide consultar el RAG la llamada aparece hacia los 1.2 s. Con un "
            "umbral más bajo, la muletilla genérica ('a ver...') se adelantaba a "
            "la de consulta ('déjame consultarlo'), que es bastante más "
            "informativa, y encima la bloqueaba por el intervalo mínimo. A 1.2 s "
            "las respuestas rápidas no llevan muletilla —no les hace falta— y "
            "las consultas llevan la suya."
        ),
    )
    filler_min_gap_secs: float = Field(
        default=4.0,
        ge=0,
        description=(
            "Intervalo mínimo entre dos muletillas, para que una pregunta que "
            "dispare las dos condiciones no suene atropellada."
        ),
    )
    mic_gate_hangover_secs: float = Field(
        default=0.5,
        ge=0.0,
        description=(
            "Segundos que la compuerta de micrófono sigue cerrada después de que "
            "el agente calle. Cubre el audio que queda en el búfer de ALSA (hasta "
            "85 ms) más la reverberación de la sala. Solo aplica al perfil "
            "'speaker'. Súbelo si el agente aún se oye a sí mismo al terminar de "
            "hablar; bájalo si tarda demasiado en volver a escucharte."
        ),
    )

    # --- RAG ---------------------------------------------------------------
    data_dir: Path = Field(
        default=Path("data"),
        description="Raíz de los datos generados: índice de Chroma y modelos descargados.",
    )
    corpus_dir: Path = Field(
        default=Path("corpus"),
        description="Carpeta con los documentos fuente (.md, .txt, .pdf) que alimentan el RAG.",
    )
    chroma_collection: str = Field(
        default="conocimiento",
        description=(
            "Prefijo de las colecciones de ChromaDB. Cada tema del corpus —cada "
            "subcarpeta de corpus/— tiene la suya: los documentos sueltos de la "
            "raíz van a 'conocimiento' y los del tema 'la-placa' a "
            "'conocimiento__la-placa'. Repartirlo así es lo que permite borrar un "
            "tema o un documento sin reconstruir el índice entero. Cambiarlo "
            "**obliga a reindexar**: las colecciones viejas quedan huérfanas."
        ),
    )
    embedding_model: str = Field(
        default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        description=(
            "Modelo de embeddings de fastembed. Debe ser multilingüe para que "
            "funcione en español. Alternativa de más calidad y más coste: "
            "jinaai/jina-embeddings-v2-base-es."
        ),
    )
    rag_top_k: int = Field(default=4, gt=0, description="Número de fragmentos a recuperar.")
    rag_max_distance: float = Field(
        default=0.68,
        ge=0.0,
        le=2.0,
        description=(
            "Distancia coseno máxima para aceptar un fragmento. Filtrar por "
            "distancia evita que el modelo reciba contexto irrelevante cuando "
            "la pregunta no está cubierta por el corpus, que es la causa más "
            "común de alucinación en un RAG. El valor 0.68 sale de medir el "
            "corpus de ejemplo con preguntas cubiertas y no cubiertas; "
            "docs/rag.md explica cómo recalibrarlo para otro corpus."
        ),
    )
    chunk_size: int = Field(
        default=700, gt=0, description="Tamaño objetivo de fragmento, en caracteres."
    )
    chunk_overlap: int = Field(default=120, ge=0, description="Solapamiento entre fragmentos.")

    # --- Telefonía (Bluetooth manos libres) --------------------------------
    # Todo este bloque es ADITIVO: con el puente parado, el agente arranca y se
    # comporta exactamente igual que antes de que existiera. Ver docs/telefonia.md.
    telefonia_modo: ModoTelefonia = Field(
        default=ModoTelefonia.AUTO,
        description=(
            "Cuándo ofrecerle al modelo las herramientas de teléfono. `auto` "
            "sondea el puente al arrancar y las activa solo si contesta, así "
            "que no hay nada que encender a mano. `on` las ofrece aunque el "
            "puente no esté (útil si lo arrancas después). `off` no las ofrece "
            "nunca, y entonces el agente es idéntico a como era antes."
        ),
    )
    telefonia_socket_path: Path | None = Field(
        default=None,
        description=(
            "Ruta del socket del puente de telefonía. Déjalo vacío: por defecto "
            "se deriva de DATA_DIR, que es lo correcto tanto en nativo como "
            "dentro del contenedor. Fijarlo a mano reproduce la trampa de rutas "
            "que ya costó una sesión con DATA_DIR."
        ),
    )
    telefonia_timeout_secs: float = Field(
        default=4.0,
        gt=0,
        description=(
            "Tope de espera de cada petición al puente. Mientras una "
            "herramienta corre, el agente calla; más de unos segundos y la "
            "conversación se rompe."
        ),
    )
    telefonia_bluetooth_address: str = Field(
        default="",
        description=(
            "MAC del móvil emparejado, en formato AA:BB:CC:DD:EE:FF. Vacío "
            "significa 'el primer teléfono emparejado que ofrezca manos "
            "libres', que es lo normal cuando solo hay uno."
        ),
    )
    telefonia_contactos_ttl_horas: int = Field(
        default=12,
        gt=0,
        description=(
            "Cada cuánto se vuelve a descargar la agenda del móvil por PBAP. "
            "Descargarla ocupa el canal Bluetooth unos segundos, así que no "
            "conviene hacerlo a menudo; los contactos nuevos aparecen tras el "
            "siguiente ciclo o forzando la sincronización desde el panel."
        ),
    )
    telefonia_max_candidatos: int = Field(
        default=5,
        gt=0,
        description=(
            "Cuántos contactos parecidos se le enseñan al modelo cuando un "
            "nombre es ambiguo. Más de cinco convierte una pregunta hablada en "
            "una lista imposible de escuchar."
        ),
    )
    telefonia_anuncio: str = Field(
        default="Te llama {quien}. ¿Respondo?",
        description=(
            "Lo que dice el agente cuando entra una llamada. Es texto fijo y no "
            "pasa por el modelo, a propósito: un móvil suena unos 25 segundos y "
            "no hay margen para una ida y vuelta al LLM. Marcador: {quien}."
        ),
    )

    telefonia_saludo_llamada: str = Field(
        default="Hola, soy el asistente. ¿En qué puedo ayudarte?",
        description=(
            "Lo que dice el agente en cuanto una llamada queda contestada. Vacío lo "
            "desactiva. Texto fijo por el mismo motivo que el anuncio: quien está al "
            "otro lado acaba de descolgar y espera oír algo ya. Marcador: {quien}. "
            "OJO: quien llama solo te oye si el móvil está en ALTAVOZ y cerca de la "
            "placa; el audio de la llamada no pasa por la tarjeta de sonido."
        ),
    )
    telefonia_saludo_espera_secs: float = Field(
        default=1.5,
        ge=0,
        description=(
            "Pausa entre que el audio de la llamada llega a la placa y el saludo. "
            "El canal SCO se abre antes de que el operador termine de enrutar el "
            "audio hasta quien llama, y un saludo inmediato le llega decapitado: "
            "solo oye el final de la frase y no entiende quién contesta. Medido en "
            "esta placa: con el saludo a dos segundos del descuelgue se oía entero; "
            "a uno, no."
        ),
    )

    # --- Varios ------------------------------------------------------------
    log_level: str = Field(default="INFO")
    timezone: str = Field(default="America/Bogota")

    @field_validator("whisper_model")
    @classmethod
    def _reject_english_only_models(cls, v: str) -> str:
        """Rechaza modelos Whisper monolingües de inglés.

        Es un error fácil de cometer (el valor por defecto de Pipecat es
        `distil-medium.en`) y su síntoma —transcripciones en inglés macarrónico
        de audio en español— es confuso de diagnosticar.
        """
        if v.endswith(".en") or ".en-" in v or "medium.en" in v:
            raise ValueError(
                f"'{v}' es un modelo Whisper solo-inglés y este agente habla español. "
                "Usa tiny, base, small o medium."
            )
        return v

    @field_validator("web_whatsapp")
    @classmethod
    def _solo_digitos_en_el_whatsapp(cls, v: str) -> str:
        """Deja el número como lo quiere `wa.me`: solo dígitos.

        Se limpia en vez de rechazar porque lo natural es copiar el número tal
        y como se lee («+57 304 641 1802»), y un `wa.me` con espacios abre una
        pantalla de error de WhatsApp que no dice qué está mal.
        """
        limpio = re.sub(r"[^0-9]", "", v)
        if limpio and len(limpio) < 8:
            raise ValueError(
                f"'{v}' no parece un número de WhatsApp internacional: quedan "
                f"{len(limpio)} dígitos. Se espera algo como 573001234567 "
                "(indicativo de país incluido, sin el 00 ni el +)."
            )
        return limpio

    @field_validator("audio_sample_rate")
    @classmethod
    def _validate_sample_rate(cls, v: int) -> int:
        """Silero VAD solo soporta 8 kHz y 16 kHz."""
        if v not in (8000, 16000):
            raise ValueError(
                f"audio_sample_rate debe ser 8000 o 16000, no {v} (limitación de Silero VAD)."
            )
        return v

    # --- Propiedades derivadas ---------------------------------------------

    @property
    def models_dir(self) -> Path:
        """Carpeta donde se cachean todos los modelos descargados."""
        return self.data_dir / "models"

    @property
    def piper_dir(self) -> Path:
        """Carpeta de las voces de Piper (ficheros .onnx + .onnx.json)."""
        return self.models_dir / "piper"

    @property
    def telefonia_socket(self) -> Path:
        """Socket del puente de telefonía, derivado de `data_dir`.

        Derivarlo es deliberado: dentro del contenedor `data_dir` es `/data` y
        en nativo es `<proyecto>/data`, así que la ruta correcta es distinta en
        cada caso. Un valor por defecto fijo sería la trampa de DATA_DIR otra
        vez, con el mismo síntoma desconcertante: servicio en marcha, logs
        limpios, y el agente incapaz de hablar con el puente.
        """
        return self.telefonia_socket_path or self.data_dir / "run" / "telefonia.sock"

    @property
    def chroma_dir(self) -> Path:
        """Carpeta del índice persistente de ChromaDB."""
        return self.data_dir / "chroma"

    @property
    def allow_interruptions(self) -> bool:
        """Indica si el usuario puede interrumpir al agente mientras habla.

        Solo es seguro con auriculares. Con altavoz abierto, el micrófono
        capta la propia voz del agente y este se interrumpiría a sí mismo en
        bucle.
        """
        return self.audio_profile is AudioProfile.HEADSET

    @property
    def effective_vad_confidence(self) -> float:
        """Umbral de confianza del VAD, derivado del perfil si no se fijó a mano.

        Con altavoz abierto se sube el umbral para que el ruido de la propia
        reproducción y el eco de la sala no disparen el detector.
        """
        if self.vad_confidence is not None:
            return self.vad_confidence
        return 0.7 if self.allow_interruptions else 0.85

    @property
    def effective_vad_start_secs(self) -> float:
        """Tiempo de voz sostenida antes de declarar 'el usuario está hablando'."""
        if self.vad_start_secs is not None:
            return self.vad_start_secs
        return 0.2 if self.allow_interruptions else 0.35

    @property
    def effective_vad_min_volume(self) -> float:
        """Volumen mínimo del VAD, derivado del perfil si no se fijó a mano.

        La condición real de Pipecat es ``confianza >= umbral Y volumen >=
        min_volume``. Como Silero reconoce el habla independientemente de lo
        alta que suene —bajar la ganancia del micrófono no reduce su
        confianza—, este umbral de volumen es el **único** mecanismo que evita
        que el agente se interrumpa a sí mismo al oírse por el altavoz.
        """
        if self.vad_min_volume is not None:
            return self.vad_min_volume
        # Ambos perfiles usan el mismo valor, el que trae Pipecat por defecto.
        # Durante un tiempo el perfil de altavoz usó 0.7, intentando que el
        # agente no se oyese a sí mismo. Fue un error: medido después, el ruido
        # de sala no dispara el VAD a *ninguna* ganancia, porque lo bloquea la
        # confianza, no el volumen; lo único que conseguía ese 0.7 era descartar
        # también al usuario cuando hablaba a distancia normal. La
        # auto-interrupción la resuelve la compuerta de audio_gate.py, que corta
        # la entrada antes del VAD, y ese es su sitio.
        return 0.6

    @property
    def effective_vad_stop_secs(self) -> float:
        """Tiempo de silencio antes de declarar 'el usuario dejó de hablar'."""
        if self.vad_stop_secs is not None:
            return self.vad_stop_secs
        # 0.2 s es el punto en el que Pipecat calibra sus latencias P99 de STT,
        # y además es el mínimo razonable para no cortar a media frase. Con
        # altavoz abierto se sube, porque ahí el margen contra el eco pesa más
        # que la latencia.
        return 0.2 if self.allow_interruptions else 0.6

    @property
    def llm_model_efectivo(self) -> str:
        """El identificador del modelo de lenguaje según el backend elegido."""
        if self.llm_backend is LLMBackend.GROQ:
            return self.groq_model
        return self.gemini_model

    @property
    def ice_servers_urls(self) -> list[str]:
        """Las URLs de `ice_servers`, ya separadas y sin espacios."""
        return [url.strip() for url in self.ice_servers.split(",") if url.strip()]

    def require_llm_api_key(self) -> str:
        """Devuelve la clave del backend de LLM o falla con un mensaje accionable.

        Returns:
            La clave en claro, lista para pasar al servicio LLM.

        Raises:
            ValueError: Si no está configurada la clave del backend elegido.
        """
        if self.llm_backend is LLMBackend.GROQ:
            if self.groq_api_key is None or not self.groq_api_key.get_secret_value():
                raise ValueError(
                    "LLM_BACKEND=groq requiere GROQ_API_KEY. Créala gratis en "
                    "https://console.groq.com/keys y añádela al .env del proyecto:\n"
                    "    GROQ_API_KEY=gsk_..."
                )
            return self.groq_api_key.get_secret_value()
        if self.gemini_api_key is None or not self.gemini_api_key.get_secret_value():
            raise ValueError(
                "Falta GEMINI_API_KEY. Créala gratis en "
                "https://aistudio.google.com/apikey y añádela al .env del proyecto:\n"
                "    GEMINI_API_KEY=AIza..."
            )
        return self.gemini_api_key.get_secret_value()

    def ensure_directories(self) -> None:
        """Crea las carpetas de datos si no existen.

        Se llama una vez al arrancar. `corpus_dir` no se crea aquí a propósito:
        si no existe es un error de configuración que conviene que se note.
        """
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.piper_dir.mkdir(parents=True, exist_ok=True)
        self.chroma_dir.mkdir(parents=True, exist_ok=True)

    def apply_model_cache_env(self) -> None:
        """Redirige las cachés de las librerías de modelos a `models_dir`.

        Hugging Face (Whisper) y fastembed deciden dónde descargar leyendo
        variables de entorno en el momento del import. Centralizarlo todo bajo
        `data/models` es lo que permite montar un único volumen en el
        contenedor y que un reinicio no vuelva a descargar cientos de MB.
        """
        self.ensure_directories()
        os.environ.setdefault("HF_HOME", str(self.models_dir / "huggingface"))
        os.environ.setdefault("FASTEMBED_CACHE_PATH", str(self.models_dir / "fastembed"))
        # CTranslate2 y ONNX Runtime respetan OMP_NUM_THREADS. Sin fijarlo,
        # OpenMP intenta usar los 6 núcleos, incluidos los A53 lentos, y el
        # reparto acaba siendo más lento que usar solo los dos A72.
        os.environ.setdefault("OMP_NUM_THREADS", str(self.whisper_cpu_threads))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Devuelve la configuración del proceso, cacheada.

    Se cachea para que leer la configuración desde cualquier punto del código
    sea barato y siempre devuelva la misma instancia.
    """
    return Settings()  # los valores vienen del entorno y del fichero .env
