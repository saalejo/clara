"""Planificador de tareas programadas: misiones que el agente ejecuta solo.

Es el segundo sitio del proyecto —tras `telefonia_anuncios`— donde algo entra
en la conversación desde fuera, y el primero que **dispara un turno del
modelo**: una misión de sala encola un mensaje de sistema con `run_llm=True`,
y el modelo habla por iniciativa propia, con sus herramientas montadas. Una
misión de llamada ni siquiera pasa por la sala: marca por el puente y deja
registrada la misión para que `atender_llamada` la recoja cuando llegue el
audio SCO.

## La recarga en caliente no viola la doctrina de `bot.py`

La configuración del panel se lee una vez porque recargar el prompt o las
muletillas invalidaría el historial y el banco de audio a mitad de turno. Las
tareas no tocan ni lo uno ni lo otro: son eventos futuros. Por eso este módulo
vigila el mtime de `tareas.json` en cada vuelta —el mismo argumento por el que
el índice RAG ya se recoge en caliente— y el panel puede guardar una tarea sin
reiniciar a nadie.

## Disparos perdidos: se pierden

Si el agente estaba apagado a la hora de una tarea, esa ejecución no se
recupera al arrancar: `siguiente()` es estrictamente futuro y aquí no hay
estado persistente de "última ejecución". Es deliberado (decisión del usuario):
una misión vieja sonando a deshoras es peor que una misión perdida. La única
huella es la bitácora, que registra lo que sí corrió.

## Una misión cada vez, y la sala manda

El planificador ejecuta las misiones en serie. Una misión de sala espera a que
haya sala (tarjeta puesta) y a que no haya llamada en curso (compuerta
retenida); si en `ESPERA_OCUPADO_MAX_SECS` no se despeja, se anota como
`sin_sala` y se pasa al siguiente disparo. Mejor perder una ejecución que
interrumpir una llamada o hablarle a una habitación sin altavoz.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from pipecat.frames.frames import LLMMessagesAppendFrame

from voice_agent.telefonia import ClienteTelefonia, ErrorTelefonia
from voice_agent_core.cron import ErrorDeCron
from voice_agent_core.rutas import ruta_bitacora_tareas, ruta_tareas
from voice_agent_core.tareas import TareaProgramada, TareasConfig, TipoTarea, cargar_tareas
from voice_agent_core.telefonia import EstadoLlamada

if TYPE_CHECKING:
    from pipecat.pipeline.worker import PipelineWorker

    from voice_agent.audio_gate import MicrophoneGate
    from voice_agent_core.config import Settings

#: Cada cuánto mira el reloj y el mtime de `tareas.json`. Medio minuto da una
#: puntualidad de sobra para tareas con resolución de minuto sin gastar CPU.
INTERVALO_TICK_SECS = 30.0

#: Cuánto se aplaza una misión de sala si no hay sala o hay llamada en curso.
ESPERA_OCUPADO_MAX_SECS = 600.0

#: Si nadie contesta la llamada saliente en este plazo, se cuelga y se anota.
TIMEOUT_SALIENTE_SECS = 60.0

#: Cada cuánto se sondea el estado de una llamada saliente en curso.
SONDEO_LLAMADA_SECS = 5.0

#: Una misión de llamada registrada que no recibe audio SCO caduca: si el SCO
#: llega más tarde de esto, será de otra llamada.
CADUCIDAD_MISION_SECS = 120.0

#: El SCO y el estado `EN_CURSO` de oFono son dos subsistemas distintos —audio
#: HFP contra propiedad D-Bus— y no cambian en el mismo instante. Medido en
#: una llamada real: el audio llegó un segundo después de marcar, con la
#: llamada todavía en `SONANDO`, y una comprobación única perdió la misión sin
#: remedio para el resto de la llamada. Estos reintentos cubren ese margen.
REINTENTOS_CORRELACION = 5
ESPERA_ENTRE_REINTENTOS_SECS = 0.3


@dataclass
class SalaActual:
    """El puente entre `ejecutar()` y la sala que esté montada en cada momento.

    El planificador vive a nivel de `ejecutar` —las misiones de llamada no
    necesitan tarjeta de sonido— pero las misiones de sala necesitan el worker
    del pipeline en marcha. `_conversar_en_sala` deja aquí el suyo al montarlo
    y lo retira al desmontarlo; entre medias, `worker` es `None` y las misiones
    de sala se aplazan.
    """

    worker: PipelineWorker | None = None
    gate: MicrophoneGate | None = None

    @property
    def ocupada(self) -> bool:
        """Hay una llamada en curso: la sala no está para misiones."""
        return self.gate is not None and self.gate.retenida


@dataclass
class MisionPendiente:
    """Una llamada saliente marcada por una tarea, esperando su audio SCO."""

    tarea: TareaProgramada
    id_llamada: str
    creada_en: float = field(default_factory=time.monotonic)
    consumida: bool = False

    @property
    def caducada(self) -> bool:
        """El SCO tardó demasiado: lo que llegue ya no es de esta llamada."""
        return time.monotonic() - self.creada_en > CADUCIDAD_MISION_SECS


class MisionesLlamada:
    """La misión de llamada pendiente, si la hay, y quién puede consumirla.

    El handoff SCO del puente no lleva ni id ni dirección —es un contrato que
    no conviene tocar: el puente es un servicio nativo y el agente una imagen
    de veinte minutos—, así que la correlación se hace aquí: el callback del
    SCO pregunta si hay una misión cuya llamada esté `EN_CURSO` ahora mismo.
    Una entrante que se cruce no se la lleva: su id no casa con el registrado.
    """

    def __init__(self) -> None:
        """Arranca sin misión pendiente."""
        self._pendiente: MisionPendiente | None = None

    def registrar(self, tarea: TareaProgramada, id_llamada: str) -> MisionPendiente:
        """Deja constancia de que la próxima llamada `id_llamada` es una misión."""
        self._pendiente = MisionPendiente(tarea=tarea, id_llamada=id_llamada)
        return self._pendiente

    def descartar(self) -> None:
        """Olvida la misión pendiente; la llamada terminó o no llegó a nada."""
        self._pendiente = None

    async def tomar_si_en_curso(self, cliente: ClienteTelefonia | None) -> MisionPendiente | None:
        """Devuelve la misión si su llamada está `EN_CURSO`; si no, `None`.

        La consulta al puente es lo que evita el falso positivo: acaba de
        llegar audio SCO, pero ¿es de NUESTRA llamada saliente? Solo si el id
        registrado sigue vivo y en curso. No marca la misión dos veces:
        consumida una vez, las siguientes llamadas devuelven `None`.

        Reintenta unas cuantas veces antes de rendirse (ver
        `REINTENTOS_CORRELACION`): el estado puede seguir en `SONANDO` un
        instante después de que el SCO ya esté entregado. Un error del
        puente, en cambio, no se reintenta —si está caído, reintentar de
        inmediato no lo arregla— y se cae a "no es una misión" sin más.
        """
        pendiente = self._pendiente
        if pendiente is None or pendiente.consumida or cliente is None:
            return None
        if pendiente.caducada:
            logger.info(f"[tareas] la misión '{pendiente.tarea.id}' caducó sin audio SCO")
            self._pendiente = None
            return None
        for intento in range(REINTENTOS_CORRELACION):
            try:
                estado = await cliente.estado()
            except ErrorTelefonia as e:
                logger.warning(f"[tareas] no pude confirmar la llamada de la misión: {e}")
                return None
            llamada = next((c for c in estado.llamadas if c.id == pendiente.id_llamada), None)
            if llamada is not None and llamada.estado is EstadoLlamada.EN_CURSO:
                pendiente.consumida = True
                logger.info(f"[tareas] la llamada {llamada.id} es la misión '{pendiente.tarea.id}'")
                return pendiente
            if intento < REINTENTOS_CORRELACION - 1:
                await asyncio.sleep(ESPERA_ENTRE_REINTENTOS_SECS)
        logger.warning(
            f"[tareas] llegó audio de llamada pero '{pendiente.tarea.id}' nunca confirmó "
            "EN_CURSO; se atiende como si no fuera una misión"
        )
        return None


def instruccion_mision_sala(tarea: TareaProgramada) -> str:
    """Redacta el mensaje de sistema que arranca una misión en la sala."""
    texto = (
        f"MISIÓN PROGRAMADA, ahora mismo (id de tarea: {tarea.id}). Tu encargo: "
        f"{tarea.mision}\n"
        "Habla tú primero, en voz alta y con naturalidad; no esperes a que te "
        "hablen. Si había una conversación en marcha, discúlpate un momento y "
        "trae el tema. Cumple el encargo y no lo alargues."
    )
    if tarea.guardar_respuestas:
        texto += (
            "\nEs un cuestionario: cuando tengas las respuestas, guárdalas con la "
            f"herramienta guardar_respuestas usando id_tarea='{tarea.id}' y "
            "confirma en voz alta que quedaron guardadas."
        )
    return texto


def instruccion_mision_llamada(tarea: TareaProgramada) -> str:
    """Redacta el añadido al prompt del pipeline de una llamada de misión."""
    quien = tarea.contacto_nombre or tarea.contacto_numero
    texto = (
        f"\n\nEsta llamada la has hecho TÚ: acabas de llamar a {quien} en nombre "
        "de la casa. Preséntate como el asistente y explica enseguida por qué "
        f"llamas. Tu encargo (id de tarea: {tarea.id}): {tarea.mision}\n"
        "Sé breve, es una llamada. Cuando el encargo esté cumplido, despídete "
        "con claridad."
    )
    if tarea.guardar_respuestas:
        texto += (
            " Es un cuestionario: antes de despedirte, guarda las respuestas con "
            f"la herramienta guardar_respuestas usando id_tarea='{tarea.id}'."
        )
    return texto


class ProgramadorTareas:
    """El bucle que mira el reloj y ejecuta las misiones cuando tocan."""

    def __init__(
        self,
        settings: Settings,
        sala: SalaActual,
        telefonia: ClienteTelefonia | None,
        misiones: MisionesLlamada,
        *,
        ahora: Callable[[], datetime] = datetime.now,
    ) -> None:
        """Prepara el planificador; no lee nada hasta la primera vuelta.

        Args:
            settings: Configuración del agente (por `data_dir`).
            sala: El worker y la compuerta de la sala montada, si la hay.
            telefonia: Cliente del puente, o `None` si este agente no tiene.
            misiones: Registro compartido con el callback del SCO.
            ahora: El reloj, inyectable en los tests.
        """
        self._settings = settings
        self._sala = sala
        self._telefonia = telefonia
        self._misiones = misiones
        self._ahora = ahora
        self._config = TareasConfig()
        self._mtime: float | None = None
        self._proximas: dict[str, datetime] = {}

    async def correr(self) -> None:
        """Vigila el fichero y el reloj para siempre. Nunca deja escapar nada.

        El mismo contrato que `escuchar_telefonia`: un fallo aquí no puede
        tumbar el agente, así que todo lo que no sea la cancelación se anota y
        se sigue en la siguiente vuelta.
        """
        while True:
            try:
                self._recargar_si_cambio()
                await self._disparar_vencidas()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"[tareas] vuelta del planificador fallida: {e}")
            await asyncio.sleep(INTERVALO_TICK_SECS)

    # --- Calendario ----------------------------------------------------------

    def _recargar_si_cambio(self) -> None:
        """Relee `tareas.json` si su mtime cambió y recalcula el calendario.

        Los próximos disparos se recalculan **estrictamente en el futuro**: una
        edición nunca dispara hacia atrás, y por eso mismo un reinicio no
        recupera nada del pasado.
        """
        ruta = ruta_tareas(self._settings.data_dir)
        try:
            mtime = ruta.stat().st_mtime
        except OSError:
            mtime = None  # sin fichero: sin tareas
        if mtime == self._mtime:
            return

        self._mtime = mtime
        self._config = cargar_tareas(self._settings.data_dir)
        ahora = self._ahora()
        self._proximas = {}
        for tarea in self._config.habilitadas:
            try:
                self._proximas[tarea.id] = tarea.expresion.siguiente(ahora)
            except ErrorDeCron as e:
                # No debería pasar: el panel valida con el mismo parser.
                logger.error(f"[tareas] '{tarea.id}' tiene un cron imposible: {e}")
        if self._proximas:
            detalle = ", ".join(f"{i} -> {p:%d/%m %H:%M}" for i, p in self._proximas.items())
            logger.info(f"[tareas] calendario: {detalle}")
        else:
            logger.info("[tareas] sin tareas habilitadas")

    async def _disparar_vencidas(self) -> None:
        """Ejecuta, de una en una, las misiones cuyo momento llegó."""
        for tarea in self._config.habilitadas:
            programada = self._proximas.get(tarea.id)
            if programada is None or self._ahora() < programada:
                continue
            if tarea.tipo is TipoTarea.SALA and not self._sala_disponible():
                if (self._ahora() - programada).total_seconds() <= ESPERA_OCUPADO_MAX_SECS:
                    continue  # se reintenta en la próxima vuelta
                self._anotar(tarea, programada, "sin_sala", "sin tarjeta o con llamada en curso")
            else:
                await self._ejecutar(tarea, programada)
            self._proximas[tarea.id] = tarea.expresion.siguiente(self._ahora())

    def _sala_disponible(self) -> bool:
        return self._sala.worker is not None and not self._sala.ocupada

    async def _ejecutar(self, tarea: TareaProgramada, programada: datetime) -> None:
        logger.info(f"[tareas] ejecutando '{tarea.id}' (programada a las {programada:%H:%M})")
        if tarea.tipo is TipoTarea.SALA:
            await self._ejecutar_en_sala(tarea, programada)
        else:
            await self._ejecutar_llamada(tarea, programada)

    # --- Misiones de sala ----------------------------------------------------

    async def _ejecutar_en_sala(self, tarea: TareaProgramada, programada: datetime) -> None:
        """Encola la misión en el pipeline de la sala y dispara un turno.

        Un solo frame de datos con `run_llm=True`: el mensaje de sistema entra
        en el historial y el modelo habla sin que nadie le haya hablado. Desde
        fuera del pipeline solo se encolan `DataFrame`s, y este lo es — la
        misma regla que `telefonia_anuncios`.
        """
        worker = self._sala.worker
        if worker is None:  # la tarjeta se fue entre la comprobación y ahora
            self._anotar(tarea, programada, "sin_sala", "la sala se desmontó en el último momento")
            return
        await worker.queue_frames(
            [
                LLMMessagesAppendFrame(
                    messages=[{"role": "system", "content": instruccion_mision_sala(tarea)}],
                    run_llm=True,
                )
            ]
        )
        self._anotar(tarea, programada, "hablado")

    # --- Misiones de llamada -------------------------------------------------

    async def _ejecutar_llamada(self, tarea: TareaProgramada, programada: datetime) -> None:
        """Marca el número congelado y vigila la llamada hasta su final.

        La conversación en sí no pasa por aquí: cuando el otro lado descuelga,
        oFono abre el SCO, el puente lo entrega, y `atender_llamada` recoge la
        misión vía `MisionesLlamada`. Este método solo marca, espera y anota.
        """
        if self._telefonia is None:
            self._anotar(tarea, programada, "error", "este agente no tiene puente de telefonía")
            return
        try:
            llamada = await self._telefonia.marcar(tarea.contacto_numero)
        except ErrorTelefonia as e:
            self._anotar(tarea, programada, "error", f"no se pudo marcar: {e}")
            return

        pendiente = self._misiones.registrar(tarea, llamada.id)
        inicio = time.monotonic()
        try:
            while True:
                await asyncio.sleep(SONDEO_LLAMADA_SECS)
                try:
                    estado = await self._telefonia.estado()
                except ErrorTelefonia as e:
                    self._anotar(tarea, programada, "error", f"puente caído en plena llamada: {e}")
                    return
                actual = next((c for c in estado.llamadas if c.id == llamada.id), None)
                if actual is None:
                    # La llamada ya no existe: o conversó y colgaron, o nadie
                    # llegó a descolgar.
                    if pendiente.consumida:
                        duracion = int(time.monotonic() - inicio)
                        self._anotar(
                            tarea, programada, "llamada_contestada", f"duró {duracion} segundos"
                        )
                    else:
                        self._anotar(tarea, programada, "sin_respuesta", "la llamada no cuajó")
                    return
                if not pendiente.consumida and time.monotonic() - inicio > TIMEOUT_SALIENTE_SECS:
                    with contextlib.suppress(ErrorTelefonia):
                        await self._telefonia.colgar(llamada.id)
                    self._anotar(
                        tarea,
                        programada,
                        "sin_respuesta",
                        f"nadie contestó en {TIMEOUT_SALIENTE_SECS:.0f} segundos",
                    )
                    return
        finally:
            self._misiones.descartar()

    # --- Bitácora -------------------------------------------------------------

    def _anotar(
        self, tarea: TareaProgramada, programada: datetime, resultado: str, detalle: str = ""
    ) -> None:
        """Deja una línea JSON en la bitácora; el panel la enseña tal cual.

        Nunca lanza: la bitácora es informativa y un disco lleno no puede
        parar el planificador.
        """
        logger.info(f"[tareas] '{tarea.id}': {resultado}{f' ({detalle})' if detalle else ''}")
        entrada = {
            "id_tarea": tarea.id,
            "programada": programada.isoformat(timespec="seconds"),
            "ejecutada": self._ahora().isoformat(timespec="seconds"),
            "resultado": resultado,
            "detalle": detalle,
        }
        ruta: Path = ruta_bitacora_tareas(self._settings.data_dir)
        try:
            ruta.parent.mkdir(parents=True, exist_ok=True)
            with ruta.open("a", encoding="utf-8") as fichero:
                fichero.write(json.dumps(entrada, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.error(f"[tareas] no pude escribir la bitácora: {e}")


__all__ = [
    "CADUCIDAD_MISION_SECS",
    "ESPERA_OCUPADO_MAX_SECS",
    "INTERVALO_TICK_SECS",
    "TIMEOUT_SALIENTE_SECS",
    "MisionPendiente",
    "MisionesLlamada",
    "ProgramadorTareas",
    "SalaActual",
    "instruccion_mision_llamada",
    "instruccion_mision_sala",
]
