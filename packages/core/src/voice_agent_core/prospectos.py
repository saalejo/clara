"""El almacén de prospectos: la memoria comercial entre conversaciones.

Cuando Clara trabaja como asesora comercial, la persona del otro lado no es un
paciente con número de teléfono sino un visitante del navegador interesado en
un agente de voz para su negocio. Este módulo guarda lo que importa entre una
visita y la siguiente: quién es (nombre, empresa, contacto), qué conversaciones
tuvo, la transcripción de cada una y el brief de requisitos que la herramienta
`guardar_brief` estructura. Con eso, un prospecto que vuelve retoma donde iba
en vez de empezar de cero.

La identidad es un id opaco —la galleta del navegador— y no un número: dos
navegadores pueden ser la misma persona, así que `buscar_por_identidad` permite
adoptar la ficha vieja cuando el interlocutor se presenta con el mismo nombre y
empresa (ver `reasignar_conversacion`).

Vive en `core` por el mismo motivo que `historial.py`: el agente escribe y el
panel lee, y el panel no puede importar `voice_agent`. Solo usa `sqlite3` de la
stdlib: el peso de `voice_agent_core` sigue siendo el que vigila
`tests/test_core_liviano.py`.

La doctrina de errores es la de la casa: **nada de aquí puede tumbar una
conversación**. Todo fallo de disco o de SQLite se anota en el log y se degrada
a "sin memoria"; la conversación sigue, solo que sin recordar.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from loguru import logger
from pydantic import BaseModel

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS prospectos (
    id TEXT PRIMARY KEY,
    nombre TEXT NOT NULL DEFAULT '',
    empresa TEXT NOT NULL DEFAULT '',
    contacto TEXT NOT NULL DEFAULT '',
    creado_en TEXT NOT NULL,
    actualizado_en TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversaciones (
    id_conversacion TEXT PRIMARY KEY,
    id_prospecto TEXT NOT NULL,
    momento TEXT NOT NULL,
    canal TEXT NOT NULL DEFAULT 'web',
    resumen TEXT NOT NULL DEFAULT '',
    transcripcion TEXT NOT NULL DEFAULT '',
    aviso_texto TEXT NOT NULL DEFAULT '',
    aviso_momento TEXT NOT NULL DEFAULT '',
    consentimiento TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_conversaciones_prospecto
    ON conversaciones (id_prospecto, momento);
CREATE TABLE IF NOT EXISTS briefs (
    id_conversacion TEXT PRIMARY KEY,
    id_prospecto TEXT NOT NULL,
    momento TEXT NOT NULL,
    empresa_y_contacto TEXT NOT NULL DEFAULT '',
    necesidad TEXT NOT NULL DEFAULT '',
    caso_de_uso TEXT NOT NULL DEFAULT '',
    canales TEXT NOT NULL DEFAULT '',
    integraciones TEXT NOT NULL DEFAULT '',
    plazo_y_presupuesto TEXT NOT NULL DEFAULT '',
    proximos_pasos TEXT NOT NULL DEFAULT '',
    notas TEXT NOT NULL DEFAULT ''
);
"""

#: Columnas añadidas después de que la base existiera, por tabla.
#:
#: Nace vacío, pero existe desde el día uno a propósito: `_ESQUEMA` es todo
#: `CREATE TABLE IF NOT EXISTS`, así que sobre la base que ya viva en la placa
#: **no se ejecuta nada** — añadir una columna solo allí arriba no la añade
#: donde importa. El síntoma sería un `no such column` en cada anotación,
#: tragado por el `except` de la casa: el almacén dejaría de anotar en
#: silencio. Toda columna nueva se declara en las dos partes (ver
#: `historial._COLUMNAS_AÑADIDAS`, que ya mordió).
_COLUMNAS_AÑADIDAS: dict[str, tuple[tuple[str, str], ...]] = {
    "prospectos": (),
    "conversaciones": (
        # El registro del consentimiento (D. 1377 arts. 7-8): qué aviso se
        # pronunció, cuándo, y si el visitante siguió conversando después.
        ("aviso_texto", "TEXT NOT NULL DEFAULT ''"),
        ("aviso_momento", "TEXT NOT NULL DEFAULT ''"),
        ("consentimiento", "TEXT NOT NULL DEFAULT ''"),
    ),
    "briefs": (),
}


def _migrar_columnas(conexion: sqlite3.Connection) -> None:
    """Añade a cada tabla las columnas que le falten, una a una.

    Se consulta `PRAGMA table_info` en vez de intentar el ALTER y capturar el
    error: así la operación normal —la base ya está al día— no genera ninguna
    excepción, que es lo que conviene cuando esto corre en cada conversación.
    """
    for tabla, columnas in _COLUMNAS_AÑADIDAS.items():
        if not columnas:
            continue
        existentes = {fila[1] for fila in conexion.execute(f"PRAGMA table_info({tabla})")}
        for nombre, definicion in columnas:
            if nombre not in existentes:
                conexion.execute(f"ALTER TABLE {tabla} ADD COLUMN {nombre} {definicion}")
                logger.info(f"[prospectos] columna '{nombre}' añadida a '{tabla}'.")


class Prospecto(BaseModel):
    """La identidad de un prospecto, tal y como se fue presentando."""

    id: str
    nombre: str = ""
    empresa: str = ""
    contacto: str = ""
    creado_en: str = ""
    actualizado_en: str = ""


class ConversacionProspecto(BaseModel):
    """Una conversación del registro, con lo que se le fue anotando.

    Attributes:
        aviso_texto: El texto exacto del aviso de privacidad que se pronunció
            al empezar — el texto y no una versión, porque el saludo es
            editable desde el panel y una constante mentiría sobre lo que de
            verdad se dijo. Vacío: no hubo aviso (conversaciones anteriores).
        aviso_momento: Cuándo se pronunció, en ISO. Vacío si no hubo.
        consentimiento: `"conducta_inequivoca"` si el visitante siguió
            conversando tras el aviso (D. 1377 art. 7); vacío si no habló.
    """

    id_conversacion: str
    id_prospecto: str
    momento: str
    canal: str = "web"
    resumen: str = ""
    transcripcion: str = ""
    aviso_texto: str = ""
    aviso_momento: str = ""
    consentimiento: str = ""


class Brief(BaseModel):
    """El brief de requisitos que la herramienta estructura al final."""

    id_conversacion: str
    id_prospecto: str
    momento: str
    empresa_y_contacto: str = ""
    necesidad: str = ""
    caso_de_uso: str = ""
    canales: str = ""
    integraciones: str = ""
    plazo_y_presupuesto: str = ""
    proximos_pasos: str = ""
    notas: str = ""


class FichaProspecto(BaseModel):
    """Lo que el almacén sabe de un prospecto cuando vuelve.

    Attributes:
        prospecto: Su identidad, con lo último que dijo de sí mismo.
        total_conversaciones: Cuántas conversaciones registradas tiene.
        ultima: La conversación más reciente, con su resumen si lo hubo.
        ultimo_brief: El brief más reciente, o `None` si nunca se cerró uno.
    """

    prospecto: Prospecto
    total_conversaciones: int
    ultima: ConversacionProspecto
    ultimo_brief: Brief | None = None


def _normalizada(cadena: str) -> str:
    return cadena.strip().lower()


#: Los campos de un brief, en el orden que espera `_a_brief`.
_CAMPOS_BRIEF = (
    "id_conversacion, id_prospecto, momento, empresa_y_contacto, necesidad, "
    "caso_de_uso, canales, integraciones, plazo_y_presupuesto, proximos_pasos, notas"
)


class AlmacenProspectos:
    """La base SQLite de los prospectos, con su doctrina de nunca lanzar.

    Cada operación abre y cierra su propia conexión: son escrituras de una
    fila y lecturas de pocas, y así el objeto se puede compartir entre las
    herramientas sin arrastrar una conexión viva entre tareas de asyncio. El
    panel, en otro proceso, lee el mismo fichero; el modo WAL deja convivir a
    ambos. El agente es el único escritor habitual (doctrina de un fichero, un
    escritor), con una única excepción deliberada: `borrar`, que ejerce el
    panel — la supresión del art. 8 de la Ley 1581 es una escritura humana,
    rara y transaccional que el WAL serializa sin drama, y un tombstone no
    cumpliría (el dato tiene que desaparecer de verdad).
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

    def registrar_conversacion(
        self,
        id_conversacion: str,
        id_prospecto: str,
        *,
        canal: str = "web",
        momento: datetime | None = None,
    ) -> None:
        """Deja constancia de una conversación, al montarla.

        Se llama al principio y no al terminar a propósito: una conversación
        que se cae a medias también cuenta como "este prospecto ya habló con
        nosotros". La transcripción y el brief llegan después.

        Args:
            id_conversacion: El mismo id que lleva la traza de la llamada.
            id_prospecto: El id opaco del prospecto (la galleta del
                navegador); vacío no registra nada.
            canal: De dónde vino la conversación; hoy, "web".
            momento: El instante; por defecto, ahora.
        """
        if not id_prospecto.strip() or not id_conversacion.strip():
            return
        cuando = (momento or datetime.now()).isoformat(timespec="seconds")
        try:
            with self._conexion() as conexion:
                conexion.execute(
                    "INSERT INTO prospectos (id, creado_en, actualizado_en) VALUES (?, ?, ?) "
                    "ON CONFLICT (id) DO UPDATE SET actualizado_en = excluded.actualizado_en",
                    (id_prospecto, cuando, cuando),
                )
                conexion.execute(
                    "INSERT OR IGNORE INTO conversaciones "
                    "(id_conversacion, id_prospecto, momento, canal) VALUES (?, ?, ?, ?)",
                    (id_conversacion, id_prospecto, cuando, canal),
                )
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[prospectos] no pude registrar la conversación {id_conversacion}: {e}")

    def identificar(
        self,
        id_prospecto: str,
        *,
        nombre: str = "",
        empresa: str = "",
        contacto: str = "",
        momento: datetime | None = None,
    ) -> None:
        """Anota la identidad que el interlocutor dio de sí mismo.

        Un campo que llegue vacío no borra el que ya constaba: que en la
        segunda visita no repita su empresa no significa que haya dejado de
        tenerla (misma regla que el nombre de la agenda en el historial).
        """
        if not id_prospecto.strip():
            return
        cuando = (momento or datetime.now()).isoformat(timespec="seconds")
        try:
            with self._conexion() as conexion:
                conexion.execute(
                    "INSERT INTO prospectos (id, nombre, empresa, contacto, creado_en, "
                    "actualizado_en) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "nombre = CASE WHEN excluded.nombre != '' THEN excluded.nombre ELSE nombre END, "
                    "empresa = CASE WHEN excluded.empresa != '' "
                    "THEN excluded.empresa ELSE empresa END, "
                    "contacto = CASE WHEN excluded.contacto != '' "
                    "THEN excluded.contacto ELSE contacto END, "
                    "actualizado_en = excluded.actualizado_en",
                    (
                        id_prospecto,
                        nombre.strip(),
                        empresa.strip(),
                        contacto.strip(),
                        cuando,
                        cuando,
                    ),
                )
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[prospectos] no pude identificar al prospecto {id_prospecto}: {e}")

    def reasignar_conversacion(self, id_conversacion: str, id_prospecto: str) -> None:
        """Cuelga la conversación (y su brief, si ya lo hubiera) de otra ficha.

        Es la adopción de identidad: el visitante habla desde un navegador
        nuevo —id nuevo— pero se presenta como alguien que ya tiene ficha. La
        conversación en curso se muda a la ficha vieja para que la memoria
        siga siendo una sola.
        """
        if not id_prospecto.strip() or not id_conversacion.strip():
            return
        try:
            with self._conexion() as conexion:
                conexion.execute(
                    "UPDATE conversaciones SET id_prospecto = ? WHERE id_conversacion = ?",
                    (id_prospecto, id_conversacion),
                )
                conexion.execute(
                    "UPDATE briefs SET id_prospecto = ? WHERE id_conversacion = ?",
                    (id_prospecto, id_conversacion),
                )
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[prospectos] no pude reasignar la conversación {id_conversacion}: {e}")

    def guardar_brief(
        self,
        id_conversacion: str,
        id_prospecto: str,
        *,
        empresa_y_contacto: str = "",
        necesidad: str = "",
        caso_de_uso: str = "",
        canales: str = "",
        integraciones: str = "",
        plazo_y_presupuesto: str = "",
        proximos_pasos: str = "",
        notas: str = "",
        momento: datetime | None = None,
    ) -> bool:
        """Guarda (o reescribe) el brief de la conversación.

        Reescribir es deliberado: si el modelo llama dos veces porque afinó un
        dato al final, la versión buena es la última.

        Returns:
            `True` si quedó en disco; `False` si la base falló (la herramienta
            se lo dice al modelo para que avise en vez de prometer en falso).
        """
        cuando = (momento or datetime.now()).isoformat(timespec="seconds")
        try:
            with self._conexion() as conexion:
                conexion.execute(
                    "INSERT INTO briefs (id_conversacion, id_prospecto, momento, "
                    "empresa_y_contacto, necesidad, caso_de_uso, canales, integraciones, "
                    "plazo_y_presupuesto, proximos_pasos, notas) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (id_conversacion) DO UPDATE SET "
                    "id_prospecto = excluded.id_prospecto, momento = excluded.momento, "
                    "empresa_y_contacto = excluded.empresa_y_contacto, "
                    "necesidad = excluded.necesidad, caso_de_uso = excluded.caso_de_uso, "
                    "canales = excluded.canales, integraciones = excluded.integraciones, "
                    "plazo_y_presupuesto = excluded.plazo_y_presupuesto, "
                    "proximos_pasos = excluded.proximos_pasos, notas = excluded.notas",
                    (
                        id_conversacion,
                        id_prospecto,
                        cuando,
                        empresa_y_contacto,
                        necesidad,
                        caso_de_uso,
                        canales,
                        integraciones,
                        plazo_y_presupuesto,
                        proximos_pasos,
                        notas,
                    ),
                )
            return True
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[prospectos] no pude guardar el brief de {id_conversacion}: {e}")
            return False

    def anotar_transcripcion(self, id_conversacion: str, transcripcion: str) -> None:
        """Anota la transcripción sobre la conversación, si está registrada."""
        try:
            with self._conexion() as conexion:
                conexion.execute(
                    "UPDATE conversaciones SET transcripcion = ? WHERE id_conversacion = ?",
                    (transcripcion, id_conversacion),
                )
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[prospectos] no pude anotar la transcripción de {id_conversacion}: {e}")

    def anotar_resumen(self, id_conversacion: str, resumen: str) -> None:
        """Anota el resumen sobre la conversación, si está registrada."""
        try:
            with self._conexion() as conexion:
                conexion.execute(
                    "UPDATE conversaciones SET resumen = ? WHERE id_conversacion = ?",
                    (resumen, id_conversacion),
                )
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[prospectos] no pude anotar el resumen de {id_conversacion}: {e}")

    def anotar_aviso(
        self, id_conversacion: str, texto: str, *, momento: datetime | None = None
    ) -> None:
        """Deja constancia del aviso de privacidad, al pronunciarlo.

        Se anota en el momento de hablar y no al colgar: el hecho que hay que
        poder probar (D. 1377 art. 8) es que el aviso sonó antes de recolectar
        nada, y una llamada que se corta a medias no debe perderlo.
        """
        if not id_conversacion.strip() or not texto.strip():
            return
        cuando = (momento or datetime.now()).isoformat(timespec="seconds")
        try:
            with self._conexion() as conexion:
                conexion.execute(
                    "UPDATE conversaciones SET aviso_texto = ?, aviso_momento = ? "
                    "WHERE id_conversacion = ?",
                    (texto, cuando, id_conversacion),
                )
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[prospectos] no pude anotar el aviso de {id_conversacion}: {e}")

    def anotar_consentimiento(self, id_conversacion: str) -> None:
        """Anota que el visitante siguió conversando tras el aviso.

        Es la conducta inequívoca del D. 1377 art. 7. Solo se anota sobre una
        conversación con aviso registrado: sin aviso previo no hay
        consentimiento que reclamar, y dejarlo escrito sería mentir.
        """
        try:
            with self._conexion() as conexion:
                conexion.execute(
                    "UPDATE conversaciones SET consentimiento = 'conducta_inequivoca' "
                    "WHERE id_conversacion = ? AND aviso_momento != ''",
                    (id_conversacion,),
                )
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[prospectos] no pude anotar el consentimiento de {id_conversacion}: {e}")

    # --- Supresión (el panel, a petición del titular) -------------------------

    def borrar(self, id_prospecto: str) -> bool:
        """Suprime la ficha entera: prospecto, conversaciones y briefs.

        Es el derecho de supresión del art. 8 de la Ley 1581, y la única
        escritura que hace el panel en esta base (ver el docstring de la
        clase). La cascada es manual porque el esquema no declara claves
        foráneas — como en el resto del fichero.

        Returns:
            `True` si la ficha existía y quedó borrada; `False` si no constaba
            o la base falló (se anota en el log, nunca lanza).
        """
        if not id_prospecto.strip():
            return False
        try:
            with self._conexion() as conexion:
                conexion.execute("DELETE FROM briefs WHERE id_prospecto = ?", (id_prospecto,))
                conexion.execute(
                    "DELETE FROM conversaciones WHERE id_prospecto = ?", (id_prospecto,)
                )
                cursor = conexion.execute("DELETE FROM prospectos WHERE id = ?", (id_prospecto,))
                borrado = cursor.rowcount > 0
            if borrado:
                logger.info(f"[prospectos] ficha {id_prospecto} suprimida a petición del titular")
            return borrado
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[prospectos] no pude suprimir la ficha {id_prospecto}: {e}")
            return False

    # --- Lectura (el agente al montar, las herramientas y el panel) -----------

    def ficha(self, id_prospecto: str) -> FichaProspecto | None:
        """La memoria de un prospecto: quién es y qué pasó la última vez.

        Returns:
            La ficha, o `None` si el id viene vacío, nunca conversó, o la base
            no se pudo leer (se degrada a "sin memoria").
        """
        if not id_prospecto.strip():
            return None
        try:
            with self._conexion() as conexion:
                prospecto = self._prospecto(conexion, id_prospecto)
                filas = self._filas_conversaciones(
                    conexion, "WHERE id_prospecto = ?", (id_prospecto,), limite=1
                )
                total = conexion.execute(
                    "SELECT COUNT(*) FROM conversaciones WHERE id_prospecto = ?",
                    (id_prospecto,),
                ).fetchone()
                brief = self._ultimo_brief(conexion, id_prospecto)
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[prospectos] no pude leer la ficha de {id_prospecto}: {e}")
            return None
        if prospecto is None or not filas:
            return None
        return FichaProspecto(
            prospecto=prospecto,
            total_conversaciones=int(total[0]) if total else len(filas),
            ultima=filas[0],
            ultimo_brief=brief,
        )

    def buscar_por_identidad(self, nombre: str, empresa: str = "") -> Prospecto | None:
        """Busca la ficha de quien se presenta por nombre y empresa.

        La empresa manda: es más discriminante que un nombre de pila, y el STT
        oye "Marta" como "Martha" con facilidad — si la empresa cuadra, el
        nombre solo desempata. La comparación es sin mayúsculas ni espacios
        sobrantes, **en Python y no en SQL**: el `LOWER()` de SQLite solo baja
        ASCII, así que «Óptica» no casaría con «óptica». Las filas están todas
        en memoria de todos modos: son los prospectos de una placa, no un CRM.
        Si varios cuadran, gana el visto más recientemente — es una heurística
        de cortesía, no una autenticación, y el prompt ya obliga a confirmar
        con la persona.
        """
        nombre_n, empresa_n = _normalizada(nombre), _normalizada(empresa)
        if not nombre_n and not empresa_n:
            return None
        try:
            with self._conexion() as conexion:
                filas = conexion.execute(
                    "SELECT id, nombre, empresa, contacto, creado_en, actualizado_en "
                    "FROM prospectos ORDER BY actualizado_en DESC"
                ).fetchall()
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[prospectos] no pude buscar por identidad: {e}")
            return None
        if empresa_n:
            por_empresa = [f for f in filas if _normalizada(f[2]) == empresa_n]
            if por_empresa:
                exactos = [f for f in por_empresa if nombre_n and _normalizada(f[1]) == nombre_n]
                return self._a_prospecto((exactos or por_empresa)[0])
            return None
        por_nombre = [f for f in filas if _normalizada(f[1]) == nombre_n]
        return self._a_prospecto(por_nombre[0]) if por_nombre else None

    def conversaciones(self, id_prospecto: str, limite: int = 20) -> list[ConversacionProspecto]:
        """Las conversaciones del prospecto, de la más reciente a la más antigua."""
        try:
            with self._conexion() as conexion:
                return self._filas_conversaciones(
                    conexion, "WHERE id_prospecto = ?", (id_prospecto,), limite=limite
                )
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[prospectos] no pude listar las conversaciones: {e}")
            return []

    def conversacion(self, id_conversacion: str) -> ConversacionProspecto | None:
        """La fila de una conversación concreta, o `None` si no consta."""
        try:
            with self._conexion() as conexion:
                filas = self._filas_conversaciones(
                    conexion, "WHERE id_conversacion = ?", (id_conversacion,), limite=1
                )
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[prospectos] no pude leer la conversación {id_conversacion}: {e}")
            return None
        return filas[0] if filas else None

    def brief(self, id_conversacion: str) -> Brief | None:
        """El brief de una conversación concreta, o `None` si no se cerró."""
        try:
            with self._conexion() as conexion:
                fila = conexion.execute(
                    f"SELECT {_CAMPOS_BRIEF} FROM briefs WHERE id_conversacion = ?",
                    (id_conversacion,),
                ).fetchone()
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[prospectos] no pude leer el brief de {id_conversacion}: {e}")
            return None
        return self._a_brief(fila) if fila else None

    def prospectos(self) -> list[FichaProspecto]:
        """Todas las fichas, de la más recientemente vista a la más antigua.

        En una sola consulta y una sola conexión, no una por ficha: el panel
        la pide para el padrón y abrir el WAL muchas veces en la placa se nota
        (misma razón que `HistorialPacientes.pacientes()`).

        El `JOIN` (y no `LEFT JOIN`) conserva la regla de `ficha()`: un id
        registrado que no tenga ninguna conversación no produce ficha.
        """
        consulta = f"""
            SELECT p.id, p.nombre, p.empresa, p.contacto, p.creado_en, p.actualizado_en,
                   c.total,
                   u.id_conversacion, u.momento, u.canal, u.resumen, u.transcripcion,
                   u.aviso_texto, u.aviso_momento, u.consentimiento,
                   b.id_conversacion, b.id_prospecto, b.momento, b.empresa_y_contacto,
                   b.necesidad, b.caso_de_uso, b.canales, b.integraciones,
                   b.plazo_y_presupuesto, b.proximos_pasos, b.notas
            FROM prospectos p
            JOIN (SELECT id_prospecto, COUNT(*) AS total FROM conversaciones
                  GROUP BY id_prospecto) c
              ON c.id_prospecto = p.id
            JOIN (SELECT *, ROW_NUMBER() OVER (
                      PARTITION BY id_prospecto
                      ORDER BY momento DESC, id_conversacion DESC
                  ) AS orden FROM conversaciones) u
              ON u.id_prospecto = p.id AND u.orden = 1
            LEFT JOIN (SELECT {_CAMPOS_BRIEF}, ROW_NUMBER() OVER (
                      PARTITION BY id_prospecto ORDER BY momento DESC
                  ) AS orden FROM briefs) b
              ON b.id_prospecto = p.id AND b.orden = 1
            ORDER BY p.actualizado_en DESC
        """
        try:
            with self._conexion() as conexion:
                filas = conexion.execute(consulta).fetchall()
        except (sqlite3.Error, OSError) as e:
            logger.error(f"[prospectos] no pude listar los prospectos: {e}")
            return []
        return [
            FichaProspecto(
                prospecto=self._a_prospecto(fila[0:6]),
                total_conversaciones=int(fila[6]),
                ultima=ConversacionProspecto(
                    id_conversacion=fila[7],
                    id_prospecto=fila[0],
                    momento=fila[8],
                    canal=fila[9],
                    resumen=fila[10],
                    transcripcion=fila[11],
                    aviso_texto=fila[12],
                    aviso_momento=fila[13],
                    consentimiento=fila[14],
                ),
                ultimo_brief=self._a_brief(fila[15:26]) if fila[15] is not None else None,
            )
            for fila in filas
        ]

    # --- Ayudantes internos ---------------------------------------------------

    @staticmethod
    def _prospecto(conexion: sqlite3.Connection, id_prospecto: str) -> Prospecto | None:
        fila = conexion.execute(
            "SELECT id, nombre, empresa, contacto, creado_en, actualizado_en "
            "FROM prospectos WHERE id = ?",
            (id_prospecto,),
        ).fetchone()
        return AlmacenProspectos._a_prospecto(fila) if fila else None

    def _ultimo_brief(self, conexion: sqlite3.Connection, id_prospecto: str) -> Brief | None:
        fila = conexion.execute(
            f"SELECT {_CAMPOS_BRIEF} FROM briefs WHERE id_prospecto = ? "
            "ORDER BY momento DESC LIMIT 1",
            (id_prospecto,),
        ).fetchone()
        return self._a_brief(fila) if fila else None

    @staticmethod
    def _a_prospecto(fila: tuple[str, ...]) -> Prospecto:
        return Prospecto(
            id=fila[0],
            nombre=fila[1],
            empresa=fila[2],
            contacto=fila[3],
            creado_en=fila[4],
            actualizado_en=fila[5],
        )

    @staticmethod
    def _a_brief(fila: tuple[str, ...]) -> Brief:
        return Brief(
            id_conversacion=fila[0],
            id_prospecto=fila[1],
            momento=fila[2],
            empresa_y_contacto=fila[3],
            necesidad=fila[4],
            caso_de_uso=fila[5],
            canales=fila[6],
            integraciones=fila[7],
            plazo_y_presupuesto=fila[8],
            proximos_pasos=fila[9],
            notas=fila[10],
        )

    @staticmethod
    def _filas_conversaciones(
        conexion: sqlite3.Connection,
        condicion: str,
        parametros: tuple[str, ...],
        *,
        limite: int,
    ) -> list[ConversacionProspecto]:
        filas = conexion.execute(
            "SELECT id_conversacion, id_prospecto, momento, canal, resumen, transcripcion, "
            "aviso_texto, aviso_momento, consentimiento "
            f"FROM conversaciones {condicion} ORDER BY momento DESC LIMIT ?",
            (*parametros, limite),
        ).fetchall()
        return [
            ConversacionProspecto(
                id_conversacion=fila[0],
                id_prospecto=fila[1],
                momento=fila[2],
                canal=fila[3],
                resumen=fila[4],
                transcripcion=fila[5],
                aviso_texto=fila[6],
                aviso_momento=fila[7],
                consentimiento=fila[8],
            )
            for fila in filas
        ]


__all__ = [
    "AlmacenProspectos",
    "Brief",
    "ConversacionProspecto",
    "FichaProspecto",
    "Prospecto",
]
