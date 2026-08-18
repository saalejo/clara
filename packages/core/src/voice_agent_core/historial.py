"""El historial de pacientes: la memoria entre llamadas, por número de teléfono.

Las evaluaciones (`evaluaciones.py`) dejan alertas y resúmenes como ficheros
JSON indexados por `id_llamada`, y eso responde a la trazabilidad pero no
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
    proximos_pasos TEXT NOT NULL DEFAULT '',
    procedimiento TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_llamadas_numero ON llamadas (numero, momento);
"""

#: Columnas añadidas después de que la base existiera, con su definición.
#:
#: Hacen falta porque `_ESQUEMA` es todo `CREATE TABLE IF NOT EXISTS`: sobre una
#: base ya creada **no se ejecuta nada**, así que añadir una columna ahí arriba
#: no la añade en la placa, donde el fichero lleva meses con llamadas dentro. El
#: síntoma sería un `no such column` en cada anotación, tragado por el
#: `except` de la casa: el historial dejaría de anotar en silencio.
_COLUMNAS_AÑADIDAS: tuple[tuple[str, str], ...] = (("procedimiento", "TEXT NOT NULL DEFAULT ''"),)


def _migrar_columnas(conexion: sqlite3.Connection) -> None:
    """Añade a `llamadas` las columnas que le falten, una a una.

    Se consulta `PRAGMA table_info` en vez de intentar el ALTER y capturar el
    error: así la operación normal —la base ya está al día— no genera ninguna
    excepción, que es lo que conviene cuando esto corre en cada llamada.
    """
    existentes = {fila[1] for fila in conexion.execute("PRAGMA table_info(llamadas)")}
    for nombre, definicion in _COLUMNAS_AÑADIDAS:
        if nombre not in existentes:
            conexion.execute(f"ALTER TABLE llamadas ADD COLUMN {nombre} {definicion}")
            logger.info(f"[historial] columna '{nombre}' añadida a una base anterior.")


class LlamadaRegistrada(BaseModel):
    """Una llamada del historial, con lo que se le fue anotando."""

    id_llamada: str
    numero: str
    momento: str
    direccion: str = Field(description="'entrante' o 'mision'.")
    nivel: str = ""
    paciente_y_procedimiento: str = ""
    #: La cirugía sola, como la resolvió la puerta de cobertura. Es la que
    #: rearma la puerta cuando este número vuelve a llamar, así que no puede
    #: salir de la prosa del modelo: `paciente_y_procedimiento` mezcla nombre y
    #: operación en texto libre y no se puede cruzar con los temas del corpus.
    procedimiento: str = ""
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
            _migrar_columnas(conexion)
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
        procedimiento: str = "",
    ) -> None:
        """Anota el resumen de la llamada, si está registrada.

        El `nivel` solo pisa al de `anotar_alerta` cuando trae algo: un
        resumen sin triaje no puede borrar el color ya decidido. El
        `procedimiento` sigue la misma regla y por el mismo motivo: una llamada
        que se cortó sin llegar a saber la cirugía no puede borrar la que ya
        constaba de la vez anterior.
        """
        try:
            with self._conexion() as conexion:
                conexion.execute(
                    "UPDATE llamadas SET paciente_y_procedimiento = ?, decision = ?, "
                    "proximos_pasos = ?, nivel = CASE WHEN ? != '' THEN ? ELSE nivel END, "
                    "procedimiento = CASE WHEN ? != '' THEN ? ELSE procedimiento END "
                    "WHERE id_llamada = ?",
                    (
                        paciente_y_procedimiento,
                        decision,
                        proximos_pasos,
                        nivel,
                        nivel,
                        procedimiento,
                        procedimiento,
                        id_llamada,
                    ),
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

    def llamada(self, id_llamada: str) -> LlamadaRegistrada | None:
        """La fila de una llamada concreta, o `None` si no consta.

        La pide el panel para el detalle de un expediente: es la mitad del
        cruce que aporta el número, la dirección y el nombre, que en los JSON
        de la evaluación no viajan.
        """
        try:
            with self._conexion() as conexion:
                filas = self._filas_llamadas(
                    conexion, "WHERE id_llamada = ?", (id_llamada,), limite=1
                )
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[historial] no pude leer la llamada {id_llamada}: {e}")
            return None
        return filas[0] if filas else None

    def buscar_llamadas(
        self,
        *,
        numero: str = "",
        direccion: str = "",
        desde: str = "",
        hasta_exclusivo: str = "",
        limite: int = 200,
    ) -> list[LlamadaRegistrada]:
        """Las llamadas que cumplen los filtros, de la más reciente a la más antigua.

        Filtra por los tres campos que **solo** viven aquí —número, dirección y
        momento—, y eso es justo lo que hace seguro empujarlos a SQL. El triaje
        y el procedimiento también constan en los JSON de la evaluación, así que
        filtrarlos aquí escondería una llamada cuya anotación se hubiera perdido
        por el camino: esos se filtran después, sobre el expediente ya fusionado.

        Args:
            numero: Número exacto; vacío no filtra.
            direccion: "entrante" o "mision"; vacío no filtra.
            desde: Momento ISO mínimo, inclusive. Basta con el día
                (`AAAA-MM-DD`): los momentos son ISO naive de ancho fijo, así
                que compararlos como cadenas es compararlos como fechas.
            hasta_exclusivo: Momento ISO máximo, **exclusivo**. Para incluir un
                día entero se le pasa el día siguiente.
            limite: Cuántas como máximo.
        """
        condiciones: list[str] = []
        parametros: list[str] = []
        for campo, valor, comparador in (
            ("numero", numero, "="),
            ("direccion", direccion, "="),
            ("momento", desde, ">="),
            ("momento", hasta_exclusivo, "<"),
        ):
            if valor:
                condiciones.append(f"{campo} {comparador} ?")
                parametros.append(valor)
        condicion = f"WHERE {' AND '.join(condiciones)}" if condiciones else ""
        try:
            with self._conexion() as conexion:
                return self._filas_llamadas(conexion, condicion, tuple(parametros), limite=limite)
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[historial] no pude buscar llamadas: {e}")
            return []

    def procedimientos(self) -> list[str]:
        """Los procedimientos distintos que constan, para el filtro del panel."""
        try:
            with self._conexion() as conexion:
                return [
                    fila[0]
                    for fila in conexion.execute(
                        "SELECT DISTINCT procedimiento FROM llamadas "
                        "WHERE procedimiento != '' ORDER BY procedimiento"
                    )
                ]
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[historial] no pude listar los procedimientos: {e}")
            return []

    def nombres(self) -> dict[str, str]:
        """El nombre conocido de cada número, en una sola consulta.

        Es lo que evita una consulta por llamada al pintar una lista larga en
        el panel.
        """
        try:
            with self._conexion() as conexion:
                return {
                    fila[0]: fila[1]
                    for fila in conexion.execute("SELECT numero, nombre FROM pacientes")
                }
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[historial] no pude leer los nombres: {e}")
            return {}

    def pacientes(self) -> list[FichaPaciente]:
        """Todas las fichas, de la más recientemente vista a la más antigua.

        En una sola consulta y una sola conexión, no una por paciente: el panel
        la pide dos veces por página —el padrón y el desplegable del filtro de
        Evaluaciones— y abrir el WAL cuarenta veces en la placa se nota.

        El `JOIN` (y no `LEFT JOIN`) conserva la regla de `ficha()`: un número
        registrado que no tenga ninguna llamada no produce ficha.
        """
        consulta = """
            SELECT p.numero, p.nombre, c.total,
                   u.id_llamada, u.momento, u.direccion, u.nivel,
                   u.paciente_y_procedimiento, u.decision, u.proximos_pasos,
                   u.procedimiento
            FROM pacientes p
            JOIN (SELECT numero, COUNT(*) AS total FROM llamadas GROUP BY numero) c
              ON c.numero = p.numero
            JOIN (SELECT *, ROW_NUMBER() OVER (
                      PARTITION BY numero ORDER BY momento DESC, id_llamada DESC
                  ) AS orden FROM llamadas) u
              ON u.numero = p.numero AND u.orden = 1
            ORDER BY p.actualizado_en DESC
        """
        try:
            with self._conexion() as conexion:
                filas = conexion.execute(consulta).fetchall()
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[historial] no pude listar los pacientes: {e}")
            return []
        return [
            FichaPaciente(
                numero=fila[0],
                nombre=fila[1],
                total_llamadas=int(fila[2]),
                ultima=LlamadaRegistrada(
                    id_llamada=fila[3],
                    numero=fila[0],
                    momento=fila[4],
                    direccion=fila[5],
                    nivel=fila[6],
                    paciente_y_procedimiento=fila[7],
                    decision=fila[8],
                    proximos_pasos=fila[9],
                    procedimiento=fila[10],
                ),
            )
            for fila in filas
            if numero_identificable(fila[0])
        ]

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
            f"decision, proximos_pasos, procedimiento FROM llamadas {condicion} "
            "ORDER BY momento DESC LIMIT ?",
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
                procedimiento=fila[8],
            )
            for fila in filas
        ]


__all__ = [
    "FichaPaciente",
    "HistorialPacientes",
    "LlamadaRegistrada",
    "numero_identificable",
]
