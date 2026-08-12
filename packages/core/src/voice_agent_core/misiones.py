"""Misiones puntuales: las llamadas que el agente se agenda él mismo.

Una tarea de `tareas.py` es un prompt con **horario recurrente**, y la escribe
un humano en el panel. Una misión puntual es un prompt con **un momento
concreto**, y la escribe el agente hablando: cuando el paciente dice "ahora no
puedo, llámame mañana a las cinco", o cuando una llamada de misión no cuaja y
el planificador quiere volver a intentarlo.

## Quién escribe qué

`tareas.json` lo reescribe el panel entero en cada guardado, así que el agente
no puede apuntar nada ahí: lo perdería en el siguiente guardado. Por eso las
misiones puntuales viven en su propio fichero, del que el agente es el único
escritor, y el panel solo lee (ver `rutas.ruta_misiones_agente`). El camino de
vuelta —cancelar desde el panel— es un segundo fichero que va al revés
(`rutas.ruta_misiones_canceladas`).

## Por qué un Protocol y no una clase base

`EncargoLlamada` existe para que el planificador marque, converse y anote
igual con una tarea del panel que con una misión puntual: lo único que las
distingue es *cuándo* suenan. Se hizo con `typing.Protocol` y no con una
`BaseModel` común porque heredar cambiaría el orden de las claves de
`tareas.json` —pydantic serializa primero lo heredado— y porque el validador
de "una llamada sin número no vale" no es compartible: una tarea puede ser de
sala, una misión puntual nunca.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Collection
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger
from pydantic import BaseModel, Field, field_validator

from voice_agent_core.rutas import ruta_misiones_agente, ruta_misiones_canceladas
from voice_agent_core.tareas import PATRON_ID

#: Prefijo reservado para lo que se inventa el agente. Sirve para dos cosas:
#: que el panel las distinga de sus tareas de un vistazo, y que una misión no
#: pueda pisar la carpeta de resultados de una tarea del panel — el id acaba
#: siendo carpeta. `TareaForm` cierra el trato por el otro lado rechazando los
#: nombres que empiecen así.
PREFIJO_AGENDA = "agenda"

#: Formatos que se le aceptan al modelo. El docstring de `programar_llamada`
#: le pide uno solo, pero escribe lo que le sale: rechazar un `2026-08-13T17:00`
#: por la T sería una herramienta que falla por nada.
_FORMATOS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")


class EstadoMision(StrEnum):
    """En qué punto está una misión puntual.

    Attributes:
        PENDIENTE: Aún no ha sonado; el planificador la tiene en el calendario.
        EJECUTADA: Se marcó el número. Dice que se intentó, no que saliera
            bien: el desenlace lo cuenta la bitácora.
        CANCELADA: Alguien la retiró, por voz o desde el panel.
        CADUCADA: Su momento pasó sin que el planificador llegara a verlo,
            normalmente porque el agente estaba apagado.
    """

    PENDIENTE = "pendiente"
    EJECUTADA = "ejecutada"
    CANCELADA = "cancelada"
    CADUCADA = "caducada"


#: Los estados en los que una misión ya no va a sonar. Se purgan por antigüedad.
ESTADOS_TERMINALES = frozenset(
    {EstadoMision.EJECUTADA, EstadoMision.CANCELADA, EstadoMision.CADUCADA}
)


class EncargoLlamada(Protocol):
    """Lo mínimo para marcar un número y conversar con un encargo.

    Lo cumplen `tareas.TareaProgramada` (el cron del panel) y `MisionPuntual`
    (la que se inventa el agente). Existe para que `_ejecutar_llamada`,
    `MisionPendiente`, `instruccion_mision_llamada` y la bitácora sirvan a las
    dos sin duplicarse.

    Todos los miembros son propiedades de solo lectura a propósito: con mypy
    estricto, declarar aquí un atributo mutable obliga al implementador a
    tener también un atributo mutable, y `id_resultados` es calculada. Al
    revés sí funciona — un campo de pydantic satisface una propiedad de solo
    lectura— así que esta forma es la única que admite a los dos.
    """

    @property
    def id(self) -> str:
        """Identificador de este encargo concreto."""
        ...

    @property
    def id_resultados(self) -> str:
        """Bajo qué id se guardan las respuestas y se anota la bitácora.

        Para una tarea del panel es su propio id. Para el reintento de una
        tarea del panel es el de la tarea **original**: si no, las respuestas
        del segundo intento caerían en una carpeta que la página de Resultados
        de esa tarea no mira.
        """
        ...

    @property
    def intento(self) -> int:
        """Cuántas veces se ha marcado ya por este encargo, empezando en 0."""
        ...

    @property
    def mision(self) -> str:
        """El encargo, redactado para el modelo."""
        ...

    @property
    def contacto_nombre(self) -> str:
        """A quién se llama, para el prompt."""
        ...

    @property
    def contacto_numero(self) -> str:
        """El número que se marca."""
        ...

    @property
    def guardar_respuestas(self) -> bool:
        """Si es un cuestionario y hay que guardar lo respondido."""
        ...


class MisionPuntual(BaseModel):
    """Un encargo de llamada para un momento concreto, sin repetición."""

    id: str = Field(description="Identificador estable; puede acabar siendo carpeta.")
    cuando: datetime = Field(description="Momento del disparo, naive y en hora de la placa.")
    mision: str = Field(description="El encargo, redactado para el modelo.")
    contacto_numero: str = Field(description="El número que se marca.")
    contacto_nombre: str = Field(default="", description="A quién se llama, para el prompt.")
    guardar_respuestas: bool = False
    estado: EstadoMision = EstadoMision.PENDIENTE
    origen: str = Field(
        default="voz",
        description="Quién la creó: 'voz' si la pidió alguien hablando, 'reintento' si nació sola.",
    )
    intento: int = Field(default=0, ge=0, description="Cuántas veces se marcó antes por lo mismo.")
    id_tarea_origen: str = Field(
        default="",
        description=(
            "La tarea del panel de la que esto es un reintento, si lo es. Manda "
            "sobre dónde se guardan las respuestas: ver `id_resultados`."
        ),
    )
    creada_en: datetime | None = None

    @field_validator("id")
    @classmethod
    def _id_valido(cls, v: str) -> str:
        if not PATRON_ID.fullmatch(v):
            raise ValueError(
                f"El id '{v}' no vale: solo minúsculas, dígitos y guiones (puede ser una ruta)."
            )
        return v

    @field_validator("cuando", "creada_en")
    @classmethod
    def _naive_en_hora_local(cls, v: datetime | None) -> datetime | None:
        """Deja el momento naive y en hora de la placa, como el cron.

        `ExpresionCron` y el planificador trabajan en naive local (ver el
        docstring de `cron.py`: Colombia no tiene cambio de hora). Un `cuando`
        con zona horaria —que es lo que devuelve pydantic si alguien escribe un
        ISO con offset— haría reventar la comparación del tick con un
        `TypeError: can't compare offset-naive and offset-aware datetimes`, y
        `ProgramadorTareas.correr` se lo traga con `logger.exception`: las
        tareas dejarían de sonar sin nada roto a la vista.
        """
        if v is None or v.tzinfo is None:
            return v
        return v.astimezone().replace(tzinfo=None)

    @property
    def id_resultados(self) -> str:
        """El id de la tarea que la originó, o el suyo propio."""
        return self.id_tarea_origen or self.id

    @property
    def pendiente(self) -> bool:
        """Sigue en el calendario del planificador."""
        return self.estado is EstadoMision.PENDIENTE


class MisionesAgente(BaseModel):
    """El fichero `misiones_agente.json` completo."""

    version: int = 1
    generado_en: datetime | None = None
    misiones: list[MisionPuntual] = Field(default_factory=list)

    @property
    def pendientes(self) -> list[MisionPuntual]:
        """Las que el planificador debe tener en el calendario, por orden."""
        return sorted((m for m in self.misiones if m.pendiente), key=lambda m: m.cuando)


class CancelacionesMisiones(BaseModel):
    """El fichero `misiones_canceladas.json` completo.

    Es una lista de ids y nada más: el panel no sabe —ni tiene por qué— si el
    agente llegó a aplicarlas. Los ids que ya no correspondan a una misión
    pendiente los poda el propio panel la próxima vez que escriba.
    """

    version: int = 1
    generado_en: datetime | None = None
    ids: list[str] = Field(default_factory=list)


def cargar_misiones(data_dir: Path) -> MisionesAgente:
    """Lee las misiones puntuales del agente, o devuelve una lista vacía.

    Nunca lanza, por la misma razón que `cargar_tareas`: un JSON corrupto
    degrada a "sin misiones" con un aviso en el log. Y el fichero puede no
    existir durante mucho tiempo —el panel arranca antes que el agente, y hasta
    que alguien no agende nada por voz nadie lo crea—, así que su ausencia es
    el caso normal, no un error.

    Args:
        data_dir: La raíz de datos, normalmente `Settings.data_dir`.

    Returns:
        Las misiones leídas, o unas vacías.
    """
    ruta = ruta_misiones_agente(data_dir)
    if not ruta.is_file():
        return MisionesAgente()
    try:
        return MisionesAgente.model_validate_json(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as e:
        logger.error(f"No se pudieron leer las misiones de {ruta}: {e}. Se sigue sin misiones.")
        return MisionesAgente()


def cargar_cancelaciones(data_dir: Path) -> CancelacionesMisiones:
    """Lee las cancelaciones que dejó el panel, o devuelve una lista vacía.

    Nunca lanza, igual que `cargar_misiones`.

    Args:
        data_dir: La raíz de datos, normalmente `Settings.data_dir`.

    Returns:
        Los ids a cancelar, o ninguno.
    """
    ruta = ruta_misiones_canceladas(data_dir)
    if not ruta.is_file():
        return CancelacionesMisiones()
    try:
        return CancelacionesMisiones.model_validate_json(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as e:
        logger.error(f"No se pudieron leer las cancelaciones de {ruta}: {e}. Se sigue sin ellas.")
        return CancelacionesMisiones()


def nuevo_id_mision(cuando: datetime, ocupados: Collection[str]) -> str:
    """Inventa un id de misión puntual que valga como nombre de carpeta.

    Lleva la marca del momento para que ordene y se lea de un vistazo, y
    cuatro caracteres aleatorios porque dos misiones para la misma hora son
    perfectamente normales ("llámame mañana a las cinco" dicho por dos
    pacientes distintos). `token_hex` da solo `[0-9a-f]`, así que `PATRON_ID`
    se cumple por construcción.

    Args:
        cuando: El momento de la misión, para la marca del nombre.
        ocupados: Ids que no se pueden repetir — los de las misiones vivas y
            los de las tareas del panel, que comparten carpeta de resultados.

    Returns:
        Un id libre, con el prefijo `agenda-`.

    Raises:
        RuntimeError: Si diez intentos seguidos chocan, que es imposible salvo
            que `ocupados` esté mal construido.
    """
    for _ in range(10):
        candidato = f"{PREFIJO_AGENDA}-{cuando:%Y%m%d-%H%M}-{secrets.token_hex(2)}"
        if candidato not in ocupados:
            return candidato
    raise RuntimeError("No se pudo inventar un id de misión libre.")


def id_de_reintento(origen: str, intento: int, cuando: datetime) -> str:
    """Deriva el id del reintento del de su origen.

    Se recorta la raíz porque el id acaba siendo nombre de fichero y el origen
    puede ser ya un reintento de un reintento. Lleva marca de tiempo para que
    dos fracasos del mismo cron el mismo día no choquen.

    Args:
        origen: El id del encargo que no cuajó.
        intento: El número del intento nuevo, empezando en 1.
        cuando: Cuándo se volverá a marcar.

    Returns:
        El id del reintento.
    """
    raiz = origen[:40].rstrip("-")
    return f"{raiz}-r{intento}-{cuando:%m%d%H%M}"


def interpretar_cuando(texto: str, zona: str) -> datetime:
    """Convierte lo que escriba el modelo en un momento naive de la placa.

    El modelo razona en la zona que le dijo `obtener_fecha_hora`, que sale de
    `Settings.timezone`; el planificador compara contra `datetime.now()`, que
    es naive en la hora del sistema. Aquí se cierra ese salto: lo que llega sin
    zona se entiende como un reloj de pared en `zona`, y lo que llega con ella
    se respeta; en ambos casos se devuelve naive local, que es lo único con lo
    que el planificador sabe comparar. Cuando la placa ya corre en `zona` —el
    caso normal— todo esto no hace nada.

    Args:
        texto: Lo que devolvió el modelo, p. ej. `2026-08-13 17:00`.
        zona: `Settings.timezone`, la zona en la que el modelo hizo la cuenta.

    Returns:
        El momento, naive y en hora local de la placa.

    Raises:
        ValueError: Si no hay forma de entender el texto.
    """
    limpio = texto.strip().replace("/", "-")
    momento: datetime | None = None
    for formato in _FORMATOS:
        try:
            momento = datetime.strptime(limpio, formato)
            break
        except ValueError:
            continue
    if momento is None:
        try:
            # Recoge lo que no casa exacto, en particular los ISO con offset.
            momento = datetime.fromisoformat(limpio)
        except ValueError as e:
            raise ValueError(f"No entiendo la fecha '{texto}'.") from e

    if momento.tzinfo is None:
        try:
            momento = momento.replace(tzinfo=ZoneInfo(zona))
        except (ZoneInfoNotFoundError, ValueError):
            # Una zona mal escrita en los ajustes no puede tumbar una llamada
            # a la herramienta: se cae a "ya viene en hora de la placa", que es
            # lo que era antes de que existiera este ajuste.
            logger.warning(f"Zona horaria desconocida '{zona}'; se toma la fecha como local.")
            return momento
    return momento.astimezone().replace(tzinfo=None)


__all__ = [
    "ESTADOS_TERMINALES",
    "PREFIJO_AGENDA",
    "CancelacionesMisiones",
    "EncargoLlamada",
    "EstadoMision",
    "MisionPuntual",
    "MisionesAgente",
    "cargar_cancelaciones",
    "cargar_misiones",
    "id_de_reintento",
    "interpretar_cuando",
    "nuevo_id_mision",
]
