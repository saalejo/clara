"""Descarga anticipada de todos los modelos que el agente necesita.

El agente usa tres modelos que se bajan de internet la primera vez:

* **Whisper** (transcripción), desde Hugging Face — entre 75 MB y 500 MB según
  el tamaño elegido.
* **La voz de Piper** (síntesis), desde el repositorio `rhasspy/piper-voices` —
  unos 60 MB.
* **El modelo de embeddings** (RAG), desde Hugging Face — unos 120 MB.

Si se dejan para el primer arranque, la primera conversación se queda colgada
varios minutos sin explicación. Ejecutar ``make models`` los deja cacheados en
``data/models``, que es justo la carpeta que se monta como volumen en el
contenedor: así una reconstrucción de la imagen o un reinicio del servicio no
vuelven a descargar nada.

El modelo de Silero para el VAD no aparece aquí porque viaja dentro del propio
paquete de Pipecat y no requiere descarga.
"""

from __future__ import annotations

import sys
import time

from loguru import logger

from voice_agent.logging import setup_logging
from voice_agent_core.config import Settings, get_settings


def download_whisper(settings: Settings) -> None:
    """Descarga y carga una vez el modelo de Whisper."""
    from faster_whisper import WhisperModel

    logger.info(f"Whisper: descargando/verificando '{settings.whisper_model}'...")
    start = time.perf_counter()
    WhisperModel(
        settings.whisper_model,
        device="cpu",
        compute_type=settings.whisper_compute_type,
        cpu_threads=settings.whisper_cpu_threads,
    )
    logger.info(f"Whisper listo ({time.perf_counter() - start:.1f} s)")


def download_piper_voice(settings: Settings) -> None:
    """Descarga la voz de Piper si no está ya en `data/models/piper`."""
    from piper.download_voices import download_voice

    destino = settings.piper_dir / f"{settings.tts_voice}.onnx"
    if destino.exists():
        logger.info(f"Piper: la voz '{settings.tts_voice}' ya está descargada")
        return

    logger.info(f"Piper: descargando la voz '{settings.tts_voice}'...")
    start = time.perf_counter()
    download_voice(settings.tts_voice, settings.piper_dir)
    logger.info(f"Voz lista ({time.perf_counter() - start:.1f} s)")


def download_embeddings(settings: Settings) -> None:
    """Descarga y carga una vez el modelo de embeddings del RAG."""
    from fastembed import TextEmbedding

    logger.info(f"Embeddings: descargando/verificando '{settings.embedding_model}'...")
    start = time.perf_counter()
    modelo = TextEmbedding(model_name=settings.embedding_model)
    # Una inferencia de prueba fuerza la carga real de la sesión de ONNX.
    list(modelo.embed(["prueba de arranque"]))
    logger.info(f"Embeddings listos ({time.perf_counter() - start:.1f} s)")


def main() -> int:
    """Descarga todos los modelos necesarios."""
    settings = get_settings()
    setup_logging(settings.log_level)
    settings.apply_model_cache_env()

    logger.info(f"Cacheando modelos en {settings.models_dir.resolve()}")
    total = time.perf_counter()

    download_whisper(settings)
    download_piper_voice(settings)
    download_embeddings(settings)

    logger.info(f"Todos los modelos preparados en {time.perf_counter() - total:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
