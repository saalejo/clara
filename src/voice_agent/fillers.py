"""Muletillas: respuestas cortas pregrabadas que tapan los silencios de espera.

## Para qué

Entre que dejas de hablar y el agente empieza a contestar pasan un par de
segundos largos, y si además tiene que consultar la base de conocimiento son
cerca de tres. Ese silencio es el peor momento de la conversación: no sabes si
te ha oído, si está pensando o si se ha colgado, y la reacción natural es
repetir la pregunta, que además lo interrumpe.

Un «dame un segundo, que lo consulto» dicho al instante arregla eso sin acelerar
nada: la latencia real no baja, pero deja de percibirse como un fallo.

## Por qué pregrabadas

Sintetizar la muletilla en el momento no serviría: Piper tarda entre tres y ocho
décimas en soltar el primer fragmento, que es justo el hueco que se quiere
tapar. Aquí se sintetizan **una sola vez** y se guardan en disco como PCM en
crudo, ya a la frecuencia del pipeline. A partir de ahí reproducirlas es copiar
bytes a la salida: cero espera.

La caché vive en `data/fillers` y se invalida sola si cambias de voz o de
frecuencia, porque el nombre del fichero incluye ambas cosas.

## Cuándo suenan

Dos disparadores, con criterios distintos a propósito:

* **Llamada a herramienta.** Suena de inmediato, sin esperar: que el modelo haya
  decidido consultar el RAG garantiza varios segundos de espera, así que no hay
  riesgo de pisar una respuesta rápida.
* **El modelo tarda en arrancar.** Aquí sí se espera un umbral (`FILLER_DELAY_SECS`),
  porque muchas respuestas empiezan antes de eso y una muletilla innecesaria
  molesta más de lo que ayuda.

Entre dos muletillas se respeta un intervalo mínimo, para que una pregunta que
dispare las dos condiciones no suene atropellada.

## Cuándo NO suenan

* **En el saludo inicial.** El temporizador solo se arma cuando el usuario deja
  de hablar, y al arrancar eso no ha ocurrido. Además el saludo es texto fijo y
  sale al instante, así que no hay espera que tapar.
* **Cuando la respuesta ya viene en camino.** Se cancela con el primer token del
  modelo (`LLMTextFrame`), no al empezar la síntesis: entre ambos median la
  agregación de la frase y el arranque de Piper, y cancelar tarde deja una
  carrera que se oye.

  Ojo con la tentación de cancelar con `LLMFullResponseStartFrame`: ese frame
  se emite al LANZAR la petición HTTP, no con el primer token, y el agregador
  dispara la petición en el mismo instante en que cierra el turno del usuario.
  Cancelar con él mata el temporizador a los pocos milisegundos de armarse,
  siempre — así murió esta funcionalidad entera durante meses sin que nadie
  lo notara.

## Dónde va en el pipeline

Entre el LLM y el TTS, y no después del TTS, por dos razones que se descubren
por las malas: el TTS **consume** todos los `TextFrame` (el primer token no
sobrevive al cruce, así que detrás del TTS no hay con qué cancelar), y el
audio pregrabado que se inyecta atraviesa el servicio TTS sin resintetizarse
—los frames que no son suyos los reenvía por su cola de serialización, en
orden—.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    FunctionCallInProgressFrame,
    InterruptionFrame,
    LLMTextFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice_agent_core.config import Settings


class FillerBank:
    """Sintetiza y cachea las muletillas, y las sirve en crudo."""

    def __init__(self, settings: Settings, *, sample_rate: int | None = None) -> None:
        """Prepara el banco.

        Args:
            settings: Configuración del agente.
            sample_rate: Frecuencia del pipeline destino; si no se da, la del
                micrófono de la sala (`audio_sample_rate`).
        """
        self._settings = settings
        self._sample_rate = sample_rate if sample_rate is not None else settings.audio_sample_rate
        self._dir = settings.data_dir / "fillers"
        self._audio: dict[str, list[bytes]] = {}
        self._ultimo: dict[str, int] = {}

    def _ruta(self, texto: str) -> Path:
        """Ruta de caché de una frase.

        El nombre incluye la voz y la frecuencia, de modo que cambiar cualquiera
        de las dos genera un fichero distinto y la caché vieja deja de usarse
        sin necesidad de borrarla a mano.
        """
        clave = f"{self._settings.tts_voice}|{self._sample_rate}|{texto}"
        return self._dir / f"{hashlib.sha256(clave.encode()).hexdigest()[:20]}.raw"

    def preparar(self, frases: dict[str, list[str]]) -> None:
        """Sintetiza lo que falte y carga todas las muletillas en memoria.

        Args:
            frases: Diccionario de categoría a lista de frases.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        pendientes = [
            (cat, t) for cat, lista in frases.items() for t in lista if not self._ruta(t).exists()
        ]

        voz = None
        if pendientes:
            # El modelo solo se carga si hay algo que sintetizar. En arranques
            # posteriores la caché ya está y nos ahorramos varios segundos y
            # unas decenas de megabytes.
            from piper import PiperVoice
            from piper.download_voices import download_voice

            ruta_voz = self._settings.piper_dir / f"{self._settings.tts_voice}.onnx"
            if not ruta_voz.exists():
                # No debería hacer falta, porque el servicio de síntesis se
                # construye antes y ya la descarga. Se hace igualmente para que
                # el banco no dependa de ese orden: si alguien lo reordena, el
                # síntoma sería un arranque roto con un error de fichero
                # inexistente, nada evidente.
                logger.info(
                    f"Descargando la voz '{self._settings.tts_voice}' para las muletillas..."
                )
                download_voice(self._settings.tts_voice, self._settings.piper_dir)

            logger.info(
                f"Sintetizando {len(pendientes)} muletilla(s) con la voz "
                f"'{self._settings.tts_voice}'..."
            )
            voz = PiperVoice.load(ruta_voz)

        for categoria, lista in frases.items():
            self._audio[categoria] = []
            for texto in lista:
                ruta = self._ruta(texto)
                if not ruta.exists() and voz is not None:
                    ruta.write_bytes(self._sintetizar(voz, texto))
                self._audio[categoria].append(ruta.read_bytes())

        total = sum(len(v) for v in self._audio.values())
        logger.info(f"Muletillas listas: {total} frase(s) en {self._dir}")

    def _sintetizar(self, voz: Any, texto: str) -> bytes:
        """Sintetiza una frase y la remuestrea a la frecuencia del pipeline."""
        import soxr

        pcm = b"".join(c.audio_int16_bytes for c in voz.synthesize(texto))
        muestras = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        origen = voz.config.sample_rate
        destino = self._sample_rate
        if origen != destino:
            muestras = np.asarray(soxr.resample(muestras, origen, destino, quality="VHQ"))
        pcm16: bytes = (np.clip(muestras, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        return pcm16

    def siguiente(self, categoria: str) -> bytes | None:
        """Devuelve una muletilla de la categoría, evitando repetir la anterior.

        Args:
            categoria: Categoría de la que elegir.

        Returns:
            El audio en crudo, o None si la categoría está vacía.
        """
        opciones = self._audio.get(categoria) or []
        if not opciones:
            return None
        if len(opciones) == 1:
            return opciones[0]
        # Repetir la misma frase dos veces seguidas delata que está enlatada.
        indices = [i for i in range(len(opciones)) if i != self._ultimo.get(categoria)]
        elegido = random.choice(indices)
        self._ultimo[categoria] = elegido
        return opciones[elegido]


class FillerProcessor(FrameProcessor):
    """Inyecta muletillas en los huecos de espera.

    Se coloca **entre el LLM y el TTS**: es el único sitio desde el que se ve
    el primer token del modelo (el TTS los consume), y el audio pregrabado que
    inyecta cruza el TTS sin resintetizarse, camino de la salida. Ver el
    docstring del módulo.
    """

    def __init__(
        self,
        bank: FillerBank,
        settings: Settings,
        *,
        sample_rate: int | None = None,
        **kwargs: Any,
    ) -> None:
        """Inicializa el procesador.

        Args:
            bank: Banco de muletillas ya preparado.
            settings: Configuración del agente.
            sample_rate: Frecuencia del pipeline destino; si no se da, la del
                micrófono de la sala (`audio_sample_rate`).
            **kwargs: Argumentos que se pasan a `FrameProcessor`.
        """
        super().__init__(**kwargs)
        self._bank = bank
        self._settings = settings
        self._sample_rate = sample_rate if sample_rate is not None else settings.audio_sample_rate
        self._bot_hablando = False
        self._ultima_muletilla = 0.0
        self._contador = 0
        self._temporizador: asyncio.Task[None] | None = None

    def _puede_sonar(self) -> bool:
        """Indica si toca muletilla, respetando el intervalo mínimo."""
        return (
            not self._bot_hablando
            and time.monotonic() - self._ultima_muletilla >= self._settings.filler_min_gap_secs
        )

    def _cancelar_espera(self) -> None:
        """Anula el temporizador de la muletilla de espera, si lo hay."""
        if self._temporizador is not None:
            self._temporizador.cancel()
            self._temporizador = None

    def _armar_espera(self) -> None:
        """Programa una muletilla si el agente tarda en arrancar.

        Hace falta un temporizador de verdad, y no comprobar el reloj cada vez
        que pasa un frame: mientras el modelo piensa no circula ningún frame por
        el pipeline, que es precisamente el momento que se quiere tapar.
        """
        self._cancelar_espera()

        async def esperar() -> None:
            try:
                await asyncio.sleep(self._settings.filler_delay_secs)
            except asyncio.CancelledError:
                return
            if self._puede_sonar():
                await self._reproducir("pensando")

        self._temporizador = self.create_task(esperar())

    async def _reproducir(self, categoria: str) -> None:
        """Empuja una muletilla hacia la salida de audio.

        Se envuelve entre `TTSStartedFrame` y `TTSStoppedFrame` para que el
        transporte la trate igual que cualquier otra voz del agente: emite
        `BotStartedSpeaking`, lo que de paso cierra la compuerta del micrófono y
        evita que el agente se oiga decir la muletilla.
        """
        audio = self._bank.siguiente(categoria)
        if audio is None:
            return

        self._ultima_muletilla = time.monotonic()
        self._cancelar_espera()
        self._contador += 1
        contexto = f"muletilla-{self._contador}"
        logger.debug(f"Muletilla ({categoria}): {len(audio) / 2 / self._sample_rate:.1f}s")

        await self.push_frame(TTSStartedFrame(context_id=contexto), FrameDirection.DOWNSTREAM)
        await self.push_frame(
            TTSAudioRawFrame(
                audio=audio,
                sample_rate=self._sample_rate,
                num_channels=1,
                context_id=contexto,
            ),
            FrameDirection.DOWNSTREAM,
        )
        await self.push_frame(TTSStoppedFrame(context_id=contexto), FrameDirection.DOWNSTREAM)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Observa el pipeline y decide cuándo meter una muletilla.

        Args:
            frame: El frame que atraviesa el procesador.
            direction: Sentido en el que circula.
        """
        await super().process_frame(frame, direction)

        if isinstance(frame, UserStoppedSpeakingFrame):
            self._armar_espera()
        elif isinstance(frame, LLMTextFrame | TTSStartedFrame):
            # Ya hay respuesta en camino; no hay hueco que tapar.
            #
            # Se cancela con el PRIMER TOKEN del modelo (`LLMTextFrame`), no
            # con `LLMFullResponseStartFrame`: ese otro se emite al lanzar la
            # petición —milisegundos después de cerrarse el turno— y cancelar
            # con él es no sonar nunca. Ver el docstring del módulo.
            self._cancelar_espera()
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_hablando = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_hablando = False
        elif isinstance(frame, InterruptionFrame):
            self._cancelar_espera()
            self._bot_hablando = False

        await self.push_frame(frame, direction)

        # La inyección va DESPUÉS de reenviar el frame original, para no colar
        # audio en medio de una secuencia que el transporte espera completa.
        if isinstance(frame, FunctionCallInProgressFrame) and self._puede_sonar():
            # Una consulta a la base de conocimiento son segundos garantizados,
            # así que no hay riesgo de pisar una respuesta rápida: suena ya, sin
            # esperar al umbral.
            await self._reproducir("consulta")
