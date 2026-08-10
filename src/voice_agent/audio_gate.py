"""Compuerta de micrófono: silencia la entrada mientras el agente habla.

## El problema

Cuando el altavoz y el micrófono comparten sala —o incluso con unos auriculares
que filtren un poco— el micrófono capta la voz sintetizada del propio agente. El
detector de actividad de voz la toma por habla del usuario, abre un turno,
interrumpe la reproducción a media frase y, si el reconocedor llega a
transcribir algo, el agente acaba respondiéndose a sí mismo.

## Por qué no bastan los ajustes

Se probaron, en este orden, y se midió cada uno:

1. **Bajar la ganancia del micrófono.** No sirve por sí solo: Silero decide por
   la *forma* del habla, no por su intensidad. Bajando la ganancia de 12/16 a
   3/16 el pico cayó a la cuarta parte y la confianza del VAD se mantuvo en 0.92.
2. **Subir `min_volume`.** Sí influye —la condición real de Pipecat es
   ``confianza >= umbral Y volumen >= min_volume``— pero es un equilibrio
   inestable: el volumen que llega depende de a qué distancia esté el micrófono,
   de cuánto suba el usuario el altavoz y de cuánto dure la frase, porque el
   volumen se suaviza exponencialmente y sube cuanto más habla el agente. Con
   ganancia 8/16 y `min_volume=0.7`, medido en frío daba cero falsos positivos y
   en una sesión real seguían apareciendo cuatro en cuarenta segundos.
3. **`AlwaysUserMuteStrategy` de Pipecat.** Ayuda, pero actúa en el agregador de
   contexto, que está *después* del reconocedor y después de que el VAD haya
   emitido sus eventos de turno. Para entonces la interrupción ya se ha
   propagado.

## La solución

Cortar el audio en el origen. Un `BaseAudioFilter` se ejecuta en el transporte
de entrada **antes del VAD y antes de propagar nada aguas abajo**, así que si
devuelve silencio mientras el agente habla, el VAD sencillamente nunca ve la voz
del agente y no hay nada que interrumpir ni que transcribir.

Esto convierte la conversación en semidúplex —mientras el agente habla, no
escucha— que es exactamente lo que el perfil `speaker` promete. Con auriculares
de verdad no hace falta, y por eso solo se instala en ese perfil.

La **cola de guarda** tras dejar de hablar no es un adorno: cuando Pipecat emite
`BotStoppedSpeakingFrame` el audio aún está sonando, porque quedan hasta 85 ms
en el búfer de ALSA, más la reverberación de la sala. Sin ella, el agente se oye
a sí mismo justo al terminar.
"""

from __future__ import annotations

import time
from typing import Any

from loguru import logger
from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    FilterControlFrame,
    Frame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class MicrophoneGate(BaseAudioFilter):
    """Filtro que sustituye la entrada por silencio mientras el agente habla."""

    def __init__(self, hangover_secs: float = 0.5) -> None:
        """Inicializa la compuerta.

        Args:
            hangover_secs: Cuánto sigue cerrada después de que el agente calle,
                para cubrir el audio que queda en el búfer de la tarjeta y la
                reverberación de la sala.
        """
        self._hangover_secs = hangover_secs
        self._bot_speaking = False
        self._retenida = False
        self._abrir_en = 0.0
        self._bloques_silenciados = 0

    @property
    def cerrada(self) -> bool:
        """Indica si la compuerta está descartando audio en este instante."""
        return self._retenida or self._bot_speaking or time.monotonic() < self._abrir_en

    @property
    def retenida(self) -> bool:
        """Indica si hay una llamada en curso reteniendo la compuerta.

        Lo consulta el planificador de tareas: una misión de sala no debe
        arrancar mientras la persona está hablando por teléfono.
        """
        return self._retenida

    def retener(self) -> None:
        """Cierra la compuerta hasta `soltar`, gane quien gane el turno.

        Es el retén de las llamadas: mientras hay una en curso, la persona
        está hablando por teléfono y no con el agente — lo que capte el
        micrófono de la sala es media conversación ajena que el VAD tomaría
        por preguntas. Independiente del ciclo habla/calla del bot: ambos
        cierres se superponen sin pisarse.
        """
        if not self._retenida:
            logger.info("Compuerta de micrófono retenida: hay una llamada en curso")
        self._retenida = True

    def soltar(self) -> None:
        """Levanta el retén de llamada; el ciclo normal sigue mandando."""
        if self._retenida:
            logger.info("Compuerta de micrófono suelta: la llamada terminó")
        self._retenida = False

    def cerrar(self) -> None:
        """Cierra la compuerta: el agente ha empezado a hablar."""
        self._bot_speaking = True
        self._bloques_silenciados = 0

    def abrir_tras_cola(self) -> None:
        """Programa la apertura: el agente ha dejado de hablar."""
        self._bot_speaking = False
        self._abrir_en = time.monotonic() + self._hangover_secs
        if self._bloques_silenciados:
            logger.debug(
                f"Compuerta de micrófono: {self._bloques_silenciados} bloques descartados "
                f"mientras el agente hablaba; reabre en {self._hangover_secs}s"
            )

    async def start(self, sample_rate: int) -> None:
        """Prepara el filtro al arrancar el transporte de entrada."""
        logger.info(
            f"Compuerta de micrófono activa (cola de guarda {self._hangover_secs}s): "
            f"la entrada se ignora mientras el agente habla"
        )

    async def stop(self) -> None:
        """Libera el filtro al parar el transporte."""

    async def process_frame(self, frame: FilterControlFrame) -> None:
        """Atiende los frames de control del filtro. No se usa ninguno aquí."""

    async def filter(self, audio: bytes) -> bytes:
        """Devuelve el audio, o silencio del mismo tamaño si la compuerta está cerrada.

        Args:
            audio: Bloque de audio capturado del micrófono.

        Returns:
            El mismo bloque, o un bloque de ceros de idéntica longitud.

        Se devuelve silencio en lugar de una cadena vacía a propósito: el
        transporte y el VAD esperan un flujo continuo, y devolver menos bytes de
        los recibidos les descuadraría la cuenta del tiempo.
        """
        if self.cerrada:
            self._bloques_silenciados += 1
            return b"\x00" * len(audio)
        return audio


class BotSpeechGateController(FrameProcessor):
    """Abre y cierra una `MicrophoneGate` según hable o calle el agente.

    El filtro de audio no puede enterarse por su cuenta: su `process_frame` solo
    recibe frames de control del propio filtro, no los de habla del bot. Este
    procesador se coloca en el pipeline justo detrás de la salida de audio, que
    es quien emite esos frames, y se limita a accionar la compuerta y dejar
    pasar todo lo demás intacto.
    """

    def __init__(self, gate: MicrophoneGate, **kwargs: Any) -> None:
        """Inicializa el controlador.

        Args:
            gate: La compuerta que hay que accionar.
            **kwargs: Argumentos que se pasan a `FrameProcessor`.
        """
        super().__init__(**kwargs)
        self._gate = gate

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Acciona la compuerta y reenvía el frame sin modificarlo.

        Args:
            frame: El frame que atraviesa el procesador.
            direction: Sentido en el que circula.
        """
        await super().process_frame(frame, direction)

        if isinstance(frame, BotStartedSpeakingFrame):
            self._gate.cerrar()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._gate.abrir_tras_cola()

        await self.push_frame(frame, direction)
