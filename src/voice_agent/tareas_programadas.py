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

## Dos calendarios, un solo ejecutor

Además de las tareas del panel —cron, recurrentes, `tareas.json`— el
planificador lleva las **misiones puntuales** que el agente se agenda a sí
mismo hablando, o que nacen de un reintento (`misiones_agente.py`). Suenan una
vez y se acabó. Todo lo que va de marcar en adelante es común a las dos: por
eso `_ejecutar_llamada` y compañía trabajan contra `EncargoLlamada` y no contra
`TareaProgramada`.

La asimetría que importa: `tareas.json` lo escribe el panel y aquí se recarga
por mtime; `misiones_agente.json` lo escribe el agente, y aquí **no se relee
nunca** — se le pregunta al `AlmacenMisiones` compartido, que es el mismo
objeto que usan las herramientas. Dos copias de la misma verdad en el mismo
proceso sería una carrera garantizada. El mtime solo gobierna lo que llega de
fuera, y de ahí también el fichero de cancelaciones, que va panel -> agente.

## Disparos perdidos: se pierden

Si el agente estaba apagado a la hora de una tarea, esa ejecución no se
recupera al arrancar: `siguiente()` es estrictamente futuro y aquí no hay
estado persistente de "última ejecución". Es deliberado (decisión del usuario):
una misión vieja sonando a deshoras es peor que una misión perdida. La única
huella es la bitácora, que registra lo que sí corrió.

Para las puntuales rige lo mismo y sin ventana de gracia, pero el mecanismo es
otro: de las que vencieron con el agente apagado se encarga
`AlmacenMisiones.caducar_vencidas()` al arrancar, y de las que vencen mientras
el planificador está ocupado, `_disparar_puntuales`. Ojo a esto último, que es
un límite real: **una llamada en curso bloquea el tick entero**, porque
`_ejecutar_llamada` sondea hasta que la llamada muere. Un "llámame en cinco
minutos" dicho en mitad de una llamada de ocho vence sin que nadie lo mire y se
anota `caducada`. La antelación mínima de `tools/misiones.py` evita el caso
trivial; el resto queda en la bitácora, que es donde se ve.

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
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from pipecat.frames.frames import LLMMessagesAppendFrame

from voice_agent.telefonia import ClienteTelefonia, ErrorTelefonia
from voice_agent_core.cron import ErrorDeCron
from voice_agent_core.misiones import EncargoLlamada, EstadoMision, cargar_cancelaciones
from voice_agent_core.rutas import (
    ruta_bitacora_tareas,
    ruta_misiones_canceladas,
    ruta_tareas,
)
from voice_agent_core.tareas import TareaProgramada, TareasConfig, TipoTarea, cargar_tareas
from voice_agent_core.telefonia import EstadoLlamada

if TYPE_CHECKING:
    from pipecat.pipeline.worker import PipelineWorker

    from voice_agent.audio_gate import MicrophoneGate
    from voice_agent.misiones_agente import AlmacenMisiones
    from voice_agent_core.config import Settings

#: Cada cuánto mira el reloj y el mtime de `tareas.json`. Medio minuto da una
#: puntualidad de sobra para tareas con resolución de minuto sin gastar CPU.
INTERVALO_TICK_SECS = 30.0

#: Cuánto se aplaza una misión de sala si no hay sala o hay llamada en curso.
ESPERA_OCUPADO_MAX_SECS = 600.0

#: Hasta cuánto retraso se le tolera a una misión puntual antes de darla por
#: perdida. **No es una ventana de gracia**: es la resolución del propio tick.
#: Cuando el planificador ve una misión vencida siempre lo hace con algo de
#: retraso —mira el reloj cada `INTERVALO_TICK_SECS` y la vuelta en sí tarda
#: algo—, así que sin este margen no sonaría ninguna jamás. Se deja en dos
#: ticks para absorber una vuelta lenta. Un retraso mayor significa que nadie
#: estuvo mirando —el agente apagado, o el tick bloqueado por una llamada
#: larga— y entonces la misión se anota `caducada` y no suena: la regla de que
#: los disparos perdidos se pierden vale igual para las puntuales.
MARGEN_TICK_SECS = INTERVALO_TICK_SECS * 2

#: Si nadie contesta la llamada saliente en este plazo, se cuelga y se anota.
TIMEOUT_SALIENTE_SECS = 60.0

#: Cada cuánto se sondea el estado de una llamada saliente en curso.
SONDEO_LLAMADA_SECS = 5.0

#: Una misión de llamada registrada que no recibe audio SCO caduca: si el SCO
#: llega más tarde de esto, será de otra llamada.
CADUCIDAD_MISION_SECS = 120.0

#: El SCO y el estado `EN_CURSO` de oFono son dos subsistemas distintos —audio
#: HFP contra propiedad D-Bus— y no cambian en el mismo instante. Peor aún,
#: medido en una llamada real de misión: **el móvil abre el SCO en el instante
#: de marcar** —por él viaja el tono de llamada— y la confirmación `EN_CURSO`
#: tarda lo que tarde el humano en descolgar, dieciséis segundos aquella vez.
#: Un puñado de reintentos fijos (5 de 0,3 s, la versión anterior) perdió la
#: misión: la llamada se atendió como entrante y el vigilante la colgó a los
#: sesenta segundos creyendo que nadie había contestado. Por eso la
#: correlación ahora ESPERA mientras la llamada registrada siga viva y
#: sonando, sondeando a este ritmo, y solo se rinde si la llamada desaparece,
#: si el audio resulta ser de OTRA llamada, o si la misión caduca.
SONDEO_CONFIRMACION_SECS = 0.5


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
    """Una llamada saliente ya marcada, esperando su audio SCO.

    El encargo puede ser una tarea del panel o una misión puntual del agente:
    a partir de aquí da igual, y por eso está tipado al Protocol.
    """

    encargo: EncargoLlamada
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

    **Hay un único hueco pendiente**, y hoy basta porque el planificador
    ejecuta las misiones en serie: mientras vigila una llamada no marca otra.
    Si algún día se paralelizan, esto es lo primero que rompe.
    """

    def __init__(self) -> None:
        """Arranca sin misión pendiente."""
        self._pendiente: MisionPendiente | None = None

    def registrar(self, encargo: EncargoLlamada, id_llamada: str) -> MisionPendiente:
        """Deja constancia de que la próxima llamada `id_llamada` es una misión."""
        self._pendiente = MisionPendiente(encargo=encargo, id_llamada=id_llamada)
        return self._pendiente

    def descartar(self) -> None:
        """Olvida la misión pendiente; la llamada terminó o no llegó a nada."""
        self._pendiente = None

    async def tomar_si_en_curso(self, cliente: ClienteTelefonia | None) -> MisionPendiente | None:
        """Devuelve la misión si su llamada llega a `EN_CURSO`; si no, `None`.

        La consulta al puente es lo que evita el falso positivo: acaba de
        llegar audio SCO, pero ¿es de NUESTRA llamada saliente? Solo si el id
        registrado sigue vivo y en curso. No marca la misión dos veces:
        consumida una vez, las siguientes llamadas devuelven `None`.

        Y espera todo lo que haga falta (ver `SONDEO_CONFIRMACION_SECS`): el
        móvil abre el SCO **al marcar** —por él viaja el tono— y `EN_CURSO`
        no llega hasta que el otro lado descuelga, segundos o decenas de
        segundos después. Mientras la llamada registrada exista y no haya
        otra en curso que reclame el audio, se sigue esperando; la espera es
        segura porque el núcleo del transporte ya está drenando el socket.
        Se rinde si la llamada desaparece (rechazada o colgada), si aparece
        OTRA llamada en curso (el SCO es suyo), o si la misión caduca. Un
        error del puente no se reintenta —si está caído, reintentar de
        inmediato no lo arregla— y se cae a "no es una misión" sin más.
        """
        pendiente = self._pendiente
        if pendiente is None or pendiente.consumida or cliente is None:
            return None
        while not pendiente.caducada:
            try:
                estado = await cliente.estado()
            except ErrorTelefonia as e:
                logger.warning(f"[tareas] no pude confirmar la llamada de la misión: {e}")
                return None
            llamada = next((c for c in estado.llamadas if c.id == pendiente.id_llamada), None)
            if llamada is None:
                logger.info(
                    f"[tareas] la llamada de '{pendiente.encargo.id}' ya no existe; "
                    "este audio no es de la misión"
                )
                return None
            if llamada.estado is EstadoLlamada.EN_CURSO:
                pendiente.consumida = True
                logger.info(
                    f"[tareas] la llamada {llamada.id} es la misión '{pendiente.encargo.id}'"
                )
                return pendiente
            if any(
                c.id != pendiente.id_llamada and c.estado is EstadoLlamada.EN_CURSO
                for c in estado.llamadas
            ):
                logger.info(
                    f"[tareas] hay otra llamada en curso; el audio no es de "
                    f"'{pendiente.encargo.id}', que sigue {llamada.estado}"
                )
                return None
            await asyncio.sleep(SONDEO_CONFIRMACION_SECS)
        logger.info(f"[tareas] la misión '{pendiente.encargo.id}' caducó sin confirmar EN_CURSO")
        self._pendiente = None
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


def instruccion_mision_llamada(encargo: EncargoLlamada) -> str:
    """Redacta el añadido al prompt del pipeline de una llamada de misión.

    El id que se le dicta al modelo es `id_resultados` y no `id`: si esto es el
    reintento de una tarea del panel, las respuestas tienen que caer en la
    carpeta de la tarea original, que es la que mira su página de Resultados.
    """
    quien = encargo.contacto_nombre or encargo.contacto_numero
    texto = (
        f"\n\nEsta llamada la has hecho TÚ: acabas de llamar a {quien} en nombre "
        "del equipo de seguimiento postoperatorio. Preséntate y explica "
        f"enseguida por qué llamas. Tu encargo (id de tarea: {encargo.id_resultados}): "
        f"{encargo.mision}\n"
        "Sé breve, es una llamada. Cuando el encargo esté cumplido, despídete "
        "con claridad."
    )
    if encargo.intento:
        texto += (
            f" Ya intentaste esta llamada {encargo.intento} vez/veces sin conseguir "
            "hablar con la persona, así que no des por hecho que sabe de qué va."
        )
    if encargo.guardar_respuestas:
        texto += (
            " Es un cuestionario: antes de despedirte, guarda las respuestas con "
            f"la herramienta guardar_respuestas usando id_tarea='{encargo.id_resultados}'."
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
        almacen: AlmacenMisiones | None = None,
    ) -> None:
        """Prepara el planificador; no lee nada hasta la primera vuelta.

        Args:
            settings: Configuración del agente (por `data_dir`).
            sala: El worker y la compuerta de la sala montada, si la hay.
            telefonia: Cliente del puente, o `None` si este agente no tiene.
            misiones: Registro compartido con el callback del SCO.
            ahora: El reloj, inyectable en los tests.
            almacen: La agenda de misiones puntuales, o `None` si este agente
                no la tiene. Es el MISMO objeto que reciben las herramientas
                por `AppResources`: sin eso, una llamada programada a mitad de
                una conversación no entraría en el calendario hasta reiniciar.
        """
        self._settings = settings
        self._sala = sala
        self._telefonia = telefonia
        self._misiones = misiones
        self._ahora = ahora
        self._almacen = almacen
        self._config = TareasConfig()
        self._mtime: float | None = None
        self._mtime_cancelaciones: float | None = None
        self._proximas: dict[str, datetime] = {}

    async def correr(self) -> None:
        """Vigila los ficheros y el reloj para siempre. Nunca deja escapar nada.

        El mismo contrato que `escuchar_telefonia`: un fallo aquí no puede
        tumbar el agente, así que todo lo que no sea la cancelación se anota y
        se sigue en la siguiente vuelta.
        """
        while True:
            try:
                self._recargar_si_cambio()
                await self._aplicar_cancelaciones_si_cambio()
                await self._disparar_vencidas()
                await self._disparar_puntuales()
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

    # --- Misiones puntuales ---------------------------------------------------

    async def _aplicar_cancelaciones_si_cambio(self) -> None:
        """Recoge lo que el panel dejó apuntado en `misiones_canceladas.json`.

        Por mtime, como `tareas.json`: este sí es un fichero que escribe el
        panel. El del agente no se relee nunca (ver el docstring del módulo).
        """
        if self._almacen is None:
            return
        ruta = ruta_misiones_canceladas(self._settings.data_dir)
        try:
            mtime = ruta.stat().st_mtime
        except OSError:
            mtime = None  # sin fichero: nada que cancelar
        if mtime == self._mtime_cancelaciones:
            return

        self._mtime_cancelaciones = mtime
        ids = cargar_cancelaciones(self._settings.data_dir).ids
        for mision in await self._almacen.aplicar_cancelaciones(ids):
            self._anotar(mision, mision.cuando, "cancelada", "cancelada desde el panel")

    async def _disparar_puntuales(self) -> None:
        """Ejecuta las misiones puntuales que vencieron, y caduca las perdidas.

        Se marcan **antes** de llamar: `_ejecutar_llamada` bloquea la vuelta
        hasta que la llamada muere, y dejarla pendiente mientras tanto la haría
        disparar dos veces si algo saliera mal por el camino.
        """
        if self._almacen is None:
            return
        for mision in self._almacen.pendientes():
            ahora = self._ahora()
            if ahora < mision.cuando:
                break  # vienen ordenadas: la primera futura corta el barrido
            retraso = (ahora - mision.cuando).total_seconds()
            if retraso > MARGEN_TICK_SECS:
                await self._almacen.marcar(mision.id, EstadoMision.CADUCADA)
                self._anotar(
                    mision,
                    mision.cuando,
                    "caducada",
                    f"su hora pasó hace {int(retraso // 60)} min sin que nadie la mirara",
                )
                continue
            if self._telefonia is None:
                await self._almacen.marcar(mision.id, EstadoMision.CADUCADA)
                self._anotar(mision, mision.cuando, "error", "este agente no tiene puente")
                continue
            await self._almacen.marcar(mision.id, EstadoMision.EJECUTADA)
            await self._ejecutar_llamada(mision, mision.cuando)

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

    async def _ejecutar_llamada(self, encargo: EncargoLlamada, programada: datetime) -> None:
        """Marca el número congelado y vigila la llamada hasta su final.

        La conversación en sí no pasa por aquí: cuando el otro lado descuelga,
        oFono abre el SCO, el puente lo entrega, y `atender_llamada` recoge la
        misión vía `MisionesLlamada`. Este método solo marca, espera y anota.

        Sirve igual a una tarea del panel que a una misión puntual: de ahí que
        reciba un `EncargoLlamada`. Y **bloquea la vuelta del planificador
        mientras dura la llamada**, que es lo que hace que las misiones se
        ejecuten en serie y que `MisionesLlamada` pueda tener un solo hueco.
        """
        if self._telefonia is None:
            await self._cerrar(encargo, programada, "error", "este agente no tiene puente")
            return
        try:
            llamada = await self._telefonia.marcar(encargo.contacto_numero)
        except ErrorTelefonia as e:
            await self._cerrar(encargo, programada, "error", f"no se pudo marcar: {e}")
            return

        pendiente = self._misiones.registrar(encargo, llamada.id)
        inicio = time.monotonic()
        try:
            while True:
                await asyncio.sleep(SONDEO_LLAMADA_SECS)
                try:
                    estado = await self._telefonia.estado()
                except ErrorTelefonia as e:
                    await self._cerrar(
                        encargo, programada, "error", f"puente caído en plena llamada: {e}"
                    )
                    return
                actual = next((c for c in estado.llamadas if c.id == llamada.id), None)
                if actual is None:
                    # La llamada ya no existe: o conversó y colgaron, o nadie
                    # llegó a descolgar.
                    if pendiente.consumida:
                        duracion = int(time.monotonic() - inicio)
                        await self._cerrar(
                            encargo, programada, "llamada_contestada", f"duró {duracion} segundos"
                        )
                    else:
                        await self._cerrar(
                            encargo, programada, "sin_respuesta", "la llamada no cuajó"
                        )
                    return
                if not pendiente.consumida and time.monotonic() - inicio > TIMEOUT_SALIENTE_SECS:
                    with contextlib.suppress(ErrorTelefonia):
                        await self._telefonia.colgar(llamada.id)
                    await self._cerrar(
                        encargo,
                        programada,
                        "sin_respuesta",
                        f"nadie contestó en {TIMEOUT_SALIENTE_SECS:.0f} segundos",
                    )
                    return
        finally:
            self._misiones.descartar()

    async def _cerrar(
        self, encargo: EncargoLlamada, programada: datetime, resultado: str, detalle: str = ""
    ) -> None:
        """Anota el desenlace de una llamada y, si no cuajó, reprograma.

        Punto único de salida de `_ejecutar_llamada`, que tiene cinco caminos
        de vuelta: sembrar el reintento en cada uno era pedir que se olvidara
        alguno.

        Solo reintentan `sin_respuesta` y `error`. Si contestaron y la
        conversación quedó a medias no se vuelve a llamar: eso es insistirle a
        alguien que ya cogió el teléfono. Y sin puente tampoco: reintentar
        contra un puente caído no lo levanta.
        """
        self._anotar(encargo, programada, resultado, detalle)
        if resultado not in ("sin_respuesta", "error"):
            return
        if self._almacen is None or self._telefonia is None:
            return
        if encargo.intento + 1 >= self._settings.tareas_reintentos_max:
            logger.info(
                f"[tareas] '{encargo.id}' agotó sus "
                f"{self._settings.tareas_reintentos_max} intentos; no se reprograma"
            )
            return
        cuando = self._ahora() + timedelta(minutes=self._settings.tareas_reintento_espera_min)
        try:
            await self._almacen.programar_reintento(encargo, cuando, ahora=self._ahora())
        except (OSError, RuntimeError, ValueError) as e:
            logger.error(f"[tareas] no se pudo programar el reintento de '{encargo.id}': {e}")

    # --- Bitácora -------------------------------------------------------------

    def _anotar(
        self, encargo: EncargoLlamada, programada: datetime, resultado: str, detalle: str = ""
    ) -> None:
        """Deja una línea JSON en la bitácora; el panel la enseña tal cual.

        Nunca lanza: la bitácora es informativa y un disco lleno no puede
        parar el planificador.

        Se anota bajo `id_resultados` y no bajo `id`: la página de Resultados
        del panel filtra la bitácora por el nombre de la tarea, así que un
        reintento apuntado con su id propio no se vería justo cuando más
        interesa. El id real va aparte, y solo cuando difiere.
        """
        logger.info(f"[tareas] '{encargo.id}': {resultado}{f' ({detalle})' if detalle else ''}")
        entrada: dict[str, object] = {
            "id_tarea": encargo.id_resultados,
            "programada": programada.isoformat(timespec="seconds"),
            "ejecutada": self._ahora().isoformat(timespec="seconds"),
            "resultado": resultado,
            "detalle": detalle,
        }
        if encargo.id != encargo.id_resultados:
            entrada["id_mision"] = encargo.id
        if encargo.intento:
            entrada["intento"] = encargo.intento
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
    "MARGEN_TICK_SECS",
    "TIMEOUT_SALIENTE_SECS",
    "MisionPendiente",
    "MisionesLlamada",
    "ProgramadorTareas",
    "SalaActual",
    "instruccion_mision_llamada",
    "instruccion_mision_sala",
]
