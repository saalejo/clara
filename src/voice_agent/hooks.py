"""Hooks: reglas que el panel engancha a puntos concretos de la conversación.

Un hook mira pasar los frames del pipeline y, según cómo esté configurado,
lanza un comando en la placa, reescribe un texto o lo descarta.

## Por qué hay dos procesadores y no uno

Los frames que interesan no viven todos en el mismo sitio del pipeline:

    transport.input() → stt → [ENTRADA] → agregador.user() → llm → [SALIDA] → tts → ...

* **ENTRADA**, entre el STT y el agregador, es el único punto donde reescribir o
  descartar la transcripción surte efecto: un frame más abajo ya ha entrado en
  el historial de la conversación y el modelo lo va a ver igual.
* **SALIDA**, entre el modelo y el sintetizador, es donde se puede tocar la
  respuesta antes de que se convierta en voz.

Si no hay hooks configurados no se inserta ninguno de los dos, así que la
instalación por defecto no paga absolutamente nada.

## La regla que no se puede romper

**Solo se transforman o descartan `TranscriptionFrame` y `LLMTextFrame`.** Ambos
son `DataFrame`: llevan datos y el pipeline sobrevive perfectamente a que uno
desaparezca. El resto —`StartFrame`, `EndFrame`, `InterruptionFrame`,
`UserStoppedSpeakingFrame`— son `SystemFrame` o `ControlFrame`, y tragarse uno
**cuelga el pipeline entero**: `_wait_for_pipeline_end` se queda esperando a que
llegue al sumidero un frame que ya no existe, y lo único que se ve es un
"timeout waiting for ... (being blocked somewhere?)" que no dice nada.

La garantía no es una convención: la rama que reescribe o veta está detrás de un
`isinstance` contra esas dos clases, y todo lo demás cae directo al `push_frame`.
`tests/test_hooks.py` lo comprueba explícitamente.

## Los comandos no bloquean el turno

Un hook que ejecuta algo se lanza por defecto **sin esperarlo**. La alternativa
—esperar— suma su duración a *cada* turno de la conversación, que en un agente
de voz se nota inmediatamente; por eso `bloqueante` es opt-in y su timeout está
topado en dos segundos. Además cada hook lleva un semáforo de uno: uno colgado
de `respuesta_texto`, que se dispara con cada fragmento del modelo, lanzaría si
no cientos de procesos por respuesta.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    LLMTextFrame,
    TranscriptionFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice_agent_core.runtime import AccionHook, EventoHook, HookConfig, RuntimeConfig

#: Variables del entorno del agente que se le pasan a un comando. Todo lo demás
#: se corta: el proceso del agente lleva las claves del LLM y de Deepgram en
#: su entorno, y un hook no tiene por qué heredarlas.
VARIABLES_HEREDADAS = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")

#: Tope de longitud del texto que se pasa por una expresión regular. Un patrón
#: con cuantificadores anidados sobre un texto largo puede tardar una eternidad,
#: y aquí eso significa congelar el bucle de eventos que mueve el audio.
MAX_CARACTERES_REGEX = 10_000


@dataclass
class _HookPreparado:
    """Un hook con su expresión regular ya compilada y su semáforo."""

    cfg: HookConfig
    regex: re.Pattern[str] | None
    semaforo: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))


def _preparar(hooks: Sequence[HookConfig]) -> list[_HookPreparado]:
    """Compila las expresiones regulares una sola vez, al construir el pipeline.

    Un patrón que no compila se descarta con un aviso en lugar de tumbar el
    arranque: el panel ya lo valida al guardar, así que llegar aquí con uno roto
    significa que alguien editó el JSON a mano.
    """
    preparados: list[_HookPreparado] = []
    for cfg in hooks:
        regex: re.Pattern[str] | None = None
        if cfg.accion is not AccionHook.EJECUTAR_COMANDO:
            try:
                regex = re.compile(cfg.patron)
            except re.error as e:
                logger.error(
                    f"El hook '{cfg.nombre}' tiene un patrón que no compila ({e}). Se ignora."
                )
                continue
        preparados.append(_HookPreparado(cfg=cfg, regex=regex))
    return preparados


class HookProcessor(FrameProcessor):
    """Aplica los hooks configurados a los frames que pasan por él."""

    def __init__(self, hooks: Sequence[HookConfig], *, nombre: str, **kwargs: Any) -> None:
        """Prepara los hooks agrupados por evento.

        Args:
            hooks: Los hooks que le tocan a este punto del pipeline.
            nombre: Nombre del procesador, para que se distingan en los logs.
            **kwargs: Se pasan tal cual a `FrameProcessor`.
        """
        super().__init__(name=nombre, **kwargs)
        self._por_evento: dict[EventoHook, list[_HookPreparado]] = {}
        for preparado in _preparar(hooks):
            self._por_evento.setdefault(preparado.cfg.evento, []).append(preparado)

    @staticmethod
    def _evento_de(frame: Frame) -> EventoHook | None:
        """Traduce un frame de Pipecat al evento que el panel enseña.

        `InterimTranscriptionFrame` queda deliberadamente fuera: es una
        transcripción provisional que Deepgram corrige sobre la marcha, y
        disparar hooks con ella significaría ejecutarlos varias veces por frase.
        """
        if isinstance(frame, TranscriptionFrame):
            return EventoHook.TRANSCRIPCION_LISTA
        if isinstance(frame, LLMTextFrame):
            return EventoHook.RESPUESTA_TEXTO
        if isinstance(frame, UserStoppedSpeakingFrame):
            return EventoHook.USUARIO_TERMINO
        if isinstance(frame, FunctionCallInProgressFrame):
            return EventoHook.LLAMADA_HERRAMIENTA
        if isinstance(frame, FunctionCallResultFrame):
            return EventoHook.RESULTADO_HERRAMIENTA
        if isinstance(frame, ErrorFrame):
            return EventoHook.ERROR
        return None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Dispara los hooks del evento y deja pasar el frame, si procede."""
        await super().process_frame(frame, direction)

        evento = self._evento_de(frame)
        preparados = self._por_evento.get(evento) if evento is not None else None
        if not preparados:
            await self.push_frame(frame, direction)
            return

        for preparado in preparados:
            if preparado.cfg.accion is AccionHook.EJECUTAR_COMANDO:
                await self._disparar_comando(preparado, frame, evento)

        # Aquí está la garantía estructural: solo estos dos tipos —ambos
        # DataFrame— pueden reescribirse o descartarse. Lee el docstring del
        # módulo antes de ampliar esta condición.
        if isinstance(frame, TranscriptionFrame | LLMTextFrame) and not self._aplicar_texto(
            preparados, frame
        ):
            return

        await self.push_frame(frame, direction)

    def _aplicar_texto(
        self, preparados: Sequence[_HookPreparado], frame: TranscriptionFrame | LLMTextFrame
    ) -> bool:
        """Reescribe o veta el texto del frame.

        Returns:
            `True` si el frame debe seguir su camino, `False` si se descarta.
        """
        texto = frame.text
        if len(texto) > MAX_CARACTERES_REGEX:
            logger.warning(
                f"Texto de {len(texto)} caracteres: se salta la aplicación de patrones "
                "para no bloquear el bucle de eventos."
            )
            return True

        for preparado in preparados:
            if preparado.regex is None:
                continue
            if preparado.cfg.accion is AccionHook.VETAR:
                if preparado.regex.search(texto):
                    logger.warning(f"El hook '{preparado.cfg.nombre}' ha descartado: {texto!r}")
                    return False
            elif preparado.cfg.accion is AccionHook.REESCRIBIR:
                texto = preparado.regex.sub(preparado.cfg.reemplazo, texto)

        if texto != frame.text:
            logger.debug(f"Hooks: {frame.text!r} -> {texto!r}")
            frame.text = texto
        return True

    async def _disparar_comando(
        self, preparado: _HookPreparado, frame: Frame, evento: EventoHook | None
    ) -> None:
        """Lanza el comando de un hook, esperándolo solo si es bloqueante."""
        carga = _carga_util(frame, evento)
        if preparado.cfg.bloqueante:
            await self._correr(preparado, carga)
        else:
            self.create_task(self._correr(preparado, carga))

    async def _correr(self, preparado: _HookPreparado, carga: dict[str, Any]) -> None:
        """Ejecuta el proceso hijo con timeout y sin pasar por una shell."""
        cfg = preparado.cfg
        if preparado.semaforo.locked():
            # Pasa de verdad con hooks colgados de 'respuesta_texto': el modelo
            # emite decenas de fragmentos por respuesta.
            logger.debug(f"El hook '{cfg.nombre}' sigue ocupado; se salta esta vez.")
            return

        async with preparado.semaforo:
            entorno = {k: v for k in VARIABLES_HEREDADAS if (v := os.environ.get(k)) is not None}
            entorno.update(cfg.entorno)
            try:
                proceso = await asyncio.create_subprocess_exec(
                    *cfg.comando,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=entorno,
                )
            except (OSError, ValueError) as e:
                logger.error(f"El hook '{cfg.nombre}' no se pudo lanzar: {e}")
                return

            datos = json.dumps(carga, ensure_ascii=False).encode()
            try:
                salida, error = await asyncio.wait_for(
                    proceso.communicate(datos), timeout=cfg.timeout_secs
                )
            except TimeoutError:
                logger.warning(f"El hook '{cfg.nombre}' pasó de {cfg.timeout_secs}s; se mata.")
                await _matar(proceso)
                return
            except asyncio.CancelledError:
                # El pipeline se está apagando y esta tarea era de las que no se
                # esperaban. Sin matar al hijo aquí, queda huérfano y el bucle de
                # eventos se queda esperándolo al cerrarse: un `make test` que
                # pasa y luego se cuelga para siempre, sin decir por qué.
                logger.debug(f"El hook '{cfg.nombre}' se cancela; se mata su proceso.")
                await _matar(proceso)
                raise

            if proceso.returncode != 0:
                logger.warning(
                    f"El hook '{cfg.nombre}' terminó con código {proceso.returncode}: "
                    f"{error.decode(errors='replace').strip()}"
                )
            elif salida.strip():
                logger.debug(
                    f"El hook '{cfg.nombre}' dijo: {salida.decode(errors='replace').strip()}"
                )


async def _matar(proceso: asyncio.subprocess.Process) -> None:
    """Mata un proceso hijo y lo recoge, sin propagar errores de carrera.

    Se recoge siempre (`wait`) aunque ya haya muerto: un hijo sin recoger deja
    un zombi, y asyncio se queda esperándolo al cerrar el bucle.
    """
    with contextlib.suppress(ProcessLookupError):
        proceso.kill()
    with contextlib.suppress(BaseException):
        await proceso.wait()


def _carga_util(frame: Frame, evento: EventoHook | None) -> dict[str, Any]:
    """Prepara el contexto que recibe un comando por su entrada estándar.

    Va por stdin y no por la línea de órdenes a propósito: así no hay que
    preocuparse de comillas ni de espacios, y nada de esto aparece en un `ps`.
    """
    carga: dict[str, Any] = {
        "evento": evento.value if evento else "",
        "frame": type(frame).__name__,
    }
    if isinstance(frame, TranscriptionFrame | LLMTextFrame):
        carga["texto"] = frame.text
    if isinstance(frame, FunctionCallInProgressFrame | FunctionCallResultFrame):
        carga["herramienta"] = frame.function_name
        carga["argumentos"] = frame.arguments
    if isinstance(frame, FunctionCallResultFrame):
        carga["resultado"] = str(frame.result)
    if isinstance(frame, ErrorFrame):
        carga["error"] = frame.error
    return carga


def construir_procesadores(
    runtime: RuntimeConfig,
) -> tuple[HookProcessor | None, HookProcessor | None]:
    """Crea los dos procesadores de hooks, o ninguno si no hay nada configurado.

    Args:
        runtime: La configuración del panel.

    Returns:
        El procesador de entrada (entre el STT y el agregador) y el de salida
        (entre el modelo y el sintetizador). Cada uno es `None` si no le
        corresponde ningún hook activo, para no meter en el pipeline
        procesadores que solo harían de pasarela.
    """
    eventos_entrada = (EventoHook.TRANSCRIPCION_LISTA, EventoHook.USUARIO_TERMINO)
    activos = [h for h in runtime.hooks if h.habilitado]

    de_entrada = sorted(
        (h for h in activos if h.evento in eventos_entrada), key=lambda h: (h.orden, h.nombre)
    )
    de_salida = sorted(
        (h for h in activos if h.evento not in eventos_entrada), key=lambda h: (h.orden, h.nombre)
    )

    return (
        HookProcessor(de_entrada, nombre="HooksEntrada") if de_entrada else None,
        HookProcessor(de_salida, nombre="HooksSalida") if de_salida else None,
    )
