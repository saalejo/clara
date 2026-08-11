"""El historial de pacientes: la memoria entre llamadas, por número de teléfono.

Las evaluaciones (`evaluaciones.py`) dejan alertas y resúmenes como ficheros
JSON indexados por `id_llamada`, y eso responde a la rúbrica del reto pero no
a la pregunta que importa al descolgar: **¿este número ya llamó antes, y qué
pasó?**. Este módulo la responde con una base SQLite en el volumen de datos:
el agente registra cada llamada identificada al montarla y le va anotando el
triaje y el resumen según las herramientas los persisten; al empezar la
siguiente llamada —entrante o misión saliente— consulta la ficha y se la da
al modelo para que dé continuidad.

Vive en `core` por el mismo motivo que `evaluaciones.py`: el agente escribe y
el panel lee, y el panel no puede importar `voice_agent`. Solo usa `sqlite3`
de la stdlib: el peso de `voice_agent_core` sigue siendo el que vigila
`tests/test_core_liviano.py`.

La doctrina de errores es la de la bitácora de tareas: **nada de aquí puede
tumbar una llamada**. Todo fallo de disco o de SQLite se anota en el log y se
degrada a "sin historial"; la indicación clínica nunca depende del registro.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field

from voice_agent_core.telefonia import NUMERO_OCULTO, RELLENOS_SIN_IDENTIFICAR

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS pacientes (
    numero TEXT PRIMARY KEY,
    nombre TEXT NOT NULL DEFAULT '',
    actualizado_en TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llamadas (
    id_llamada TEXT PRIMARY KEY,
    numero TEXT NOT NULL,
    momento TEXT NOT NULL,
    direccion TEXT NOT NULL,
    nivel TEXT NOT NULL DEFAULT '',
    paciente_y_procedimiento TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL DEFAULT '',
    proximos_pasos TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_llamadas_numero ON llamadas (numero, momento);
"""


class LlamadaRegistrada(BaseModel):
    """Una llamada del historial, con lo que se le fue anotando."""

    id_llamada: str
    numero: str
    momento: str
    direccion: str = Field(description="'entrante' o 'mision'.")
    nivel: str = ""
    paciente_y_procedimiento: str = ""
    decision: str = ""
    proximos_pasos: str = ""


class FichaPaciente(BaseModel):
    """Lo que el historial sabe de un número cuando vuelve a sonar.

    Attributes:
        numero: El número de teléfono, tal y como lo entrega el puente.
        nombre: El último nombre conocido (normalmente el de la agenda del
            móvil), o vacío.
        total_llamadas: Cuántas llamadas registradas tiene el número.
        ultima: La llamada más reciente, con su triaje y su resumen anotados
            si los hubo.
    """

    numero: str
    nombre: str = ""
    total_llamadas: int
    ultima: LlamadaRegistrada


def numero_identificable(numero: str) -> bool:
    """Decide si un número puede indexar una ficha.

    Los rellenos de las llamadas de app (`RELLENOS_SIN_IDENTIFICAR`) y el
    número oculto no identifican a nadie: guardarlos mezclaría pacientes
    distintos bajo una misma ficha, que es peor que no recordar.
    """
    limpio = numero.strip()
    return bool(limpio) and limpio != NUMERO_OCULTO and limpio not in RELLENOS_SIN_IDENTIFICAR


class HistorialPacientes:
    """La base SQLite del historial, con su doctrina de nunca lanzar.

    Cada operación abre y cierra su propia conexión: son escrituras de una
    fila y lecturas de pocas, y así el objeto se puede compartir entre el
    camino telefónico y las herramientas sin arrastrar una conexión viva
    entre tareas de asyncio. El panel, en otro proceso, lee el mismo fichero;
    el modo WAL deja convivir a ambos.
    """

    def __init__(self, ruta: Path) -> None:
        """Apunta al fichero de la base; no toca el disco hasta la primera operación."""
        self._ruta = ruta

    @contextmanager
    def _conexion(self) -> Iterator[sqlite3.Connection]:
        self._ruta.parent.mkdir(parents=True, exist_ok=True)
        conexion = sqlite3.connect(self._ruta, timeout=5)
        try:
            conexion.execute("PRAGMA journal_mode=WAL")
            conexion.executescript(_ESQUEMA)
            yield conexion
            conexion.commit()
        finally:
            conexion.close()

    # --- Escritura (el agente) -----------------------------------------------

    def registrar_llamada(
        self,
        id_llamada: str,
        numero: str,
        direccion: str,
        *,
        nombre: str = "",
        momento: datetime | None = None,
    ) -> None:
        """Deja constancia de una llamada identificada, al montarla.

        Se llama al principio y no al colgar a propósito: una llamada que se
        cae a medias también cuenta como "este número ya llamó". El triaje y
        el resumen llegan después con `anotar_alerta` y `anotar_resumen`.

        Args:
            id_llamada: El mismo id que llevan la traza, la alerta y el resumen.
            numero: El número del otro extremo; si no es identificable
                (relleno de app, oculto, vacío) no se registra nada.
            direccion: "entrante" o "mision".
            nombre: El nombre de la agenda del móvil, si lo hubo.
            momento: El instante de la llamada; por defecto, ahora.
        """
        if not numero_identificable(numero):
            return
        numero = numero.strip()
        cuando = (momento or datetime.now()).isoformat(timespec="seconds")
        try:
            with self._conexion() as conexion:
                conexion.execute(
                    "INSERT INTO pacientes (numero, nombre, actualizado_en) VALUES (?, ?, ?) "
                    "ON CONFLICT (numero) DO UPDATE SET "
                    "nombre = CASE WHEN excluded.nombre != '' THEN excluded.nombre ELSE nombre END, "
                    "actualizado_en = excluded.actualizado_en",
                    (numero, nombre.strip(), cuando),
                )
                conexion.execute(
                    "INSERT OR IGNORE INTO llamadas (id_llamada, numero, momento, direccion) "
                    "VALUES (?, ?, ?, ?)",
                    (id_llamada, numero, cuando, direccion),
                )
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[historial] no pude registrar la llamada {id_llamada}: {e}")

    def anotar_alerta(self, id_llamada: str, nivel: str) -> None:
        """Anota el nivel de triaje sobre la llamada, si está registrada.

        Sobre una llamada sin ficha (navegador, número oculto) no hace nada:
        el JSON de la alerta ya quedó escrito por su lado.
        """
        try:
            with self._conexion() as conexion:
                conexion.execute(
                    "UPDATE llamadas SET nivel = ? WHERE id_llamada = ?", (nivel, id_llamada)
                )
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[historial] no pude anotar la alerta de {id_llamada}: {e}")

    def anotar_resumen(
        self,
        id_llamada: str,
        *,
        paciente_y_procedimiento: str,
        decision: str,
        proximos_pasos: str,
        nivel: str = "",
    ) -> None:
        """Anota el resumen de la llamada, si está registrada.

        El `nivel` solo pisa al de `anotar_alerta` cuando trae algo: un
        resumen sin triaje no puede borrar el color ya decidido.
        """
        try:
            with self._conexion() as conexion:
                conexion.execute(
                    "UPDATE llamadas SET paciente_y_procedimiento = ?, decision = ?, "
                    "proximos_pasos = ?, nivel = CASE WHEN ? != '' THEN ? ELSE nivel END "
                    "WHERE id_llamada = ?",
                    (paciente_y_procedimiento, decision, proximos_pasos, nivel, nivel, id_llamada),
                )
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[historial] no pude anotar el resumen de {id_llamada}: {e}")

    # --- Lectura (el agente al descolgar, la herramienta y el panel) ----------

    def ficha(self, numero: str) -> FichaPaciente | None:
        """La memoria de un número: cuántas veces llamó y qué pasó la última.

        Returns:
            La ficha, o `None` si el número no es identificable, nunca llamó,
            o la base no se pudo leer (se degrada a "sin historial").
        """
        if not numero_identificable(numero):
            return None
        numero = numero.strip()
        try:
            with self._conexion() as conexion:
                paciente = conexion.execute(
                    "SELECT nombre FROM pacientes WHERE numero = ?", (numero,)
                ).fetchone()
                filas = self._filas_llamadas(conexion, "WHERE numero = ?", (numero,), limite=1)
                total = conexion.execute(
                    "SELECT COUNT(*) FROM llamadas WHERE numero = ?", (numero,)
                ).fetchone()
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[historial] no pude leer la ficha de {numero}: {e}")
            return None
        if not filas:
            return None
        return FichaPaciente(
            numero=numero,
            nombre=paciente[0] if paciente else "",
            total_llamadas=int(total[0]) if total else len(filas),
            ultima=filas[0],
        )

    def llamadas(self, numero: str | None = None, limite: int = 50) -> list[LlamadaRegistrada]:
        """Las llamadas registradas, de la más reciente a la más antigua.

        Args:
            numero: Filtra por número; `None` devuelve las de todos.
            limite: Cuántas como máximo.
        """
        try:
            with self._conexion() as conexion:
                if numero is None:
                    return self._filas_llamadas(conexion, "", (), limite=limite)
                return self._filas_llamadas(conexion, "WHERE numero = ?", (numero,), limite=limite)
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[historial] no pude listar las llamadas: {e}")
            return []

    def pacientes(self) -> list[FichaPaciente]:
        """Todas las fichas, de la más recientemente vista a la más antigua."""
        try:
            with self._conexion() as conexion:
                numeros = [
                    fila[0]
                    for fila in conexion.execute(
                        "SELECT numero FROM pacientes ORDER BY actualizado_en DESC"
                    )
                ]
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[historial] no pude listar los pacientes: {e}")
            return []
        return [ficha for numero in numeros if (ficha := self.ficha(numero)) is not None]

    @staticmethod
    def _filas_llamadas(
        conexion: sqlite3.Connection,
        condicion: str,
        parametros: tuple[str, ...],
        *,
        limite: int,
    ) -> list[LlamadaRegistrada]:
        filas = conexion.execute(
            "SELECT id_llamada, numero, momento, direccion, nivel, paciente_y_procedimiento, "
            f"decision, proximos_pasos FROM llamadas {condicion} ORDER BY momento DESC LIMIT ?",
            (*parametros, limite),
        ).fetchall()
        return [
            LlamadaRegistrada(
                id_llamada=fila[0],
                numero=fila[1],
                momento=fila[2],
                direccion=fila[3],
                nivel=fila[4],
                paciente_y_procedimiento=fila[5],
                decision=fila[6],
                proximos_pasos=fila[7],
            )
            for fila in filas
        ]


__all__ = [
    "FichaPaciente",
    "HistorialPacientes",
    "LlamadaRegistrada",
    "numero_identificable",
]
