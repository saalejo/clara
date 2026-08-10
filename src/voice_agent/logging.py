"""Configuración de logging y silenciado del ruido de ALSA.

Pipecat usa `loguru` internamente, así que basta con configurar su sink para
gobernar también las trazas del framework.

La segunda mitad de este módulo resuelve una molestia clásica de PortAudio en
Linux: al enumerar dispositivos, alsa-lib escribe directamente en el `stderr`
del proceso decenas de líneas del tipo::

    ALSA lib pcm.c:2664:(snd_pcm_open_noupdate) Unknown PCM cards.pcm.rear
    ALSA lib pcm_route.c:877:(find_matching_chmap) Found no matching channel map

No son errores del programa: alsa-lib prueba PCMs definidos en su configuración
por defecto (`surround51`, `rear`, `hdmi`, ...) que esta tarjeta USB no
implementa. Como se escriben desde C, no pasan por `logging` ni por `loguru` y
no se pueden filtrar desde Python de la forma habitual. La única manera limpia
de callarlas es registrar un manejador de error propio en la librería mediante
`ctypes`.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from voice_agent_core.rutas import ruta_snapshot_settings

# Firma de snd_lib_error_handler_t:
#     void (*)(const char *file, int line, const char *function, int err, const char *fmt, ...)
_ALSA_ERROR_HANDLER = ctypes.CFUNCTYPE(
    None,
    ctypes.c_char_p,  # file
    ctypes.c_int,  # line
    ctypes.c_char_p,  # function
    ctypes.c_int,  # err
    ctypes.c_char_p,  # fmt
)


def _swallow_alsa_error(
    _file: bytes | None,
    _line: int,
    _function: bytes | None,
    _err: int,
    _fmt: bytes | None,
) -> None:
    """Descarta un mensaje de error de alsa-lib."""


# Hay que conservar una referencia viva al callback: si el recolector de basura
# se lo lleva, alsa-lib acabaría saltando a memoria liberada.
_alsa_handler_ref = _ALSA_ERROR_HANDLER(_swallow_alsa_error)


def silence_alsa_warnings() -> None:
    """Redirige los mensajes de error de alsa-lib a un manejador vacío.

    Es una operación idempotente y sin efecto si la librería no está presente
    (por ejemplo, al ejecutar los tests en una máquina sin ALSA).
    """
    try:
        asound = ctypes.cdll.LoadLibrary("libasound.so.2")
    except OSError:
        logger.debug("libasound.so.2 no disponible; no hay avisos de ALSA que silenciar")
        return
    asound.snd_lib_error_set_handler(_alsa_handler_ref)


@contextmanager
def suppressed_stderr() -> Iterator[None]:
    """Silencia el descriptor 2 durante el bloque, incluso desde código C.

    El manejador de errores de alsa-lib solo cubre a alsa-lib. PortAudio
    compila además soporte de JACK y, al inicializarse, libjack escribe por su
    cuenta cosas como::

        Cannot connect to server socket err = No such file or directory
        jack server is not running or cannot be started

    en esta placa, donde no hay ni habrá un servidor JACK. Eso no pasa por
    `loguru` ni por el manejador de ALSA: se escribe directamente en el
    descriptor 2 del proceso.

    La única forma fiable de callarlo es redirigir el propio descriptor a
    /dev/null. Se usa **solo** alrededor de la construcción de `PyAudio`, que
    es la ventana en la que aparece ese ruido; fuera de ella el `stderr` sigue
    intacto, de modo que ningún error real del programa queda oculto.
    """
    sys.stderr.flush()
    saved_fd = os.dup(2)
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull_fd, 2)
        yield
    finally:
        sys.stderr.flush()
        os.dup2(saved_fd, 2)
        os.close(devnull_fd)
        os.close(saved_fd)


def setup_logging(level: str = "INFO", *, archivo: Path | None = None) -> None:
    """Configura los sinks de loguru para todo el proceso.

    Args:
        level: Nivel mínimo a emitir (DEBUG, INFO, WARNING, ERROR).
        archivo: Fichero al que duplicar el log, además de la consola. Lo usa el
            agente para que el **panel de control**, que corre en otro
            contenedor, pueda seguir la conversación en vivo. Sin esto el panel
            no tendría forma de leer los logs: el journal del sistema no es
            accesible desde dentro de un contenedor sin privilegios, y
            `podman logs` tampoco vale porque Quadlet añade `--rm` y destruye el
            contenedor —con sus logs— justo cuando querrías saber por qué se
            cayó.
    """
    logger.remove()
    logger.add(
        sys.stderr,
        level=level.upper(),
        colorize=True,
        backtrace=False,  # las trazas completas de Pipecat son enormes
        diagnose=False,  # evita volcar variables locales (pueden llevar secretos)
        format=(
            "<green>{time:HH:mm:ss.SSS}</green> "
            "<level>{level: <8}</level> "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan> - "
            "<level>{message}</level>"
        ),
    )

    if archivo is None:
        return

    archivo.parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        archivo,
        level=level.upper(),
        # La rotación y la retención no son opcionales: esto escribe en una
        # microSD, y un log sin techo se la come y se la desgasta.
        rotation="5 MB",
        retention=3,
        # El agente vive en un bucle asyncio con audio en tiempo real. `enqueue`
        # manda la escritura a un hilo aparte para que una espera de disco no
        # llegue a cortar la reproducción.
        enqueue=True,
        backtrace=False,
        diagnose=False,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} {level: <8} {name}:{function} - {message}",
    )


def log_startup_banner(settings: Any, runtime: Any = None) -> None:
    """Vuelca la configuración efectiva al arrancar.

    Tener esto en el log ahorra muchísimo tiempo cuando algo se comporta de
    forma inesperada: casi siempre la causa es que una variable de entorno no
    era la que uno creía.

    Desde que existe el panel hay una segunda pregunta igual de frecuente —"lo
    cambié y no pasó nada"— y por eso se registra también de dónde salió la
    configuración y qué se aplicó. Sin esa línea, distinguir "el panel no
    guardó", "no se exportó" y "el agente no se reinició" es adivinar.

    Args:
        settings: La instancia de `Settings` en uso.
        runtime: La `RuntimeConfig` aplicada, si la hay.
    """
    logger.info("=" * 68)
    logger.info("Agente de voz — configuración efectiva")
    logger.info("=" * 68)
    logger.info(f"  LLM ............ {settings.llm_model_efectivo} ({settings.llm_backend})")
    # Cada backend tiene ajustes distintos; mostrar los de Whisper cuando está
    # activo Deepgram sería justo el tipo de confusión que este banner pretende
    # evitar.
    if settings.stt_backend == "deepgram":
        detalle_stt = (
            f"deepgram / {settings.deepgram_model} (nube, p99={settings.deepgram_ttfs_p99}s)"
        )
    else:
        detalle_stt = (
            f"whisper / {settings.whisper_model} (local, {settings.whisper_compute_type}, "
            f"{settings.whisper_cpu_threads} hilos, p99={settings.whisper_ttfs_p99}s)"
        )
    logger.info(f"  STT ............ {detalle_stt}")
    logger.info(f"  TTS ............ piper / {settings.tts_voice}")
    logger.info(
        f"  Muletillas ..... {'sí' if settings.filler_enabled else 'no'}"
        + (
            f" (espera {settings.filler_delay_secs}s, intervalo mínimo {settings.filler_min_gap_secs}s)"
            if settings.filler_enabled
            else ""
        )
    )
    logger.info(
        f"  Perfil audio ... {settings.audio_profile} "
        f"(interrupciones: {'sí' if settings.allow_interruptions else 'no'})"
    )
    logger.info(
        f"  Dispositivos ... entrada={settings.audio_input_device} "
        f"salida={settings.audio_output_device} @ {settings.audio_sample_rate} Hz"
    )
    logger.info(
        f"  VAD ............ confianza={settings.effective_vad_confidence} "
        f"vol_min={settings.effective_vad_min_volume} "
        f"inicio={settings.effective_vad_start_secs}s parada={settings.effective_vad_stop_secs}s"
    )
    logger.info(
        f"  RAG ............ {settings.chroma_collection} @ {settings.chroma_dir} "
        f"(top_k={settings.rag_top_k}, dist_max={settings.rag_max_distance})"
    )

    instantanea = ruta_snapshot_settings(settings.data_dir)
    if instantanea.is_file():
        marca = datetime.fromtimestamp(instantanea.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"  Panel .......... instantánea aplicada del {marca}")
    else:
        logger.info("  Panel .......... sin instantánea; manda el .env y el entorno")

    if runtime is not None:
        logger.info(f"  Perfil ......... {runtime.perfil or '(sin perfil)'}")
        desactivadas = sorted(runtime.herramientas_desactivadas)
        logger.info(
            f"  Alma ........... {'sí' if runtime.prompt.alma.strip() else 'no'}"
            f"  |  herramientas desactivadas: {', '.join(desactivadas) or 'ninguna'}"
        )
        activos = [h.nombre for h in runtime.hooks if h.habilitado]
        mcp = [s.nombre for s in runtime.servidores_mcp_activos]
        logger.info(
            f"  Hooks .......... {', '.join(activos) or 'ninguno'}"
            f"  |  MCP: {', '.join(mcp) or 'ninguno'}"
        )
    logger.info("=" * 68)
