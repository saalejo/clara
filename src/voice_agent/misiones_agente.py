"""El almacén de misiones puntuales: el único escritor de su fichero.

La doctrina de `rutas.py` es **un fichero, un escritor**. Aquí el escritor es
este objeto, y es uno solo para todo el proceso: las herramientas que corren
dentro de una conversación y el planificador que mira el reloj apuntan al
MISMO `AlmacenMisiones`. Por eso el planificador no relee `misiones_agente.json`
por mtime como sí hace con `tareas.json` — tendría dos copias de la misma
verdad dentro del mismo proceso, y la carrera está garantizada: la herramienta
escribe, el planificador conserva su copia vieja y dispara algo ya cancelado.
El mtime solo gobierna lo que escribe el panel.

El `asyncio.Lock` no es paranoia. Una herramienta corre en la tarea del
pipeline de la llamada y el planificador en la suya, en el mismo bucle de
eventos; sin él, el `await` que hay entre leer la lista y volcarla pierde
entera la escritura de la otra.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger

from voice_agent_core.misiones import (
    ESTADOS_TERMINALES,
    EncargoLlamada,
    EstadoMision,
    MisionesAgente,
    MisionPuntual,
    cargar_misiones,
    id_de_reintento,
    nuevo_id_mision,
)
from voice_agent_core.rutas import escribir_json_atomico, ruta_misiones_agente
from voice_agent_core.tareas import cargar_tareas

if TYPE_CHECKING:
    from collections.abc import Collection

    from voice_agent_core.config import Settings

#: Cuántas misiones ya terminadas se conservan. Son el historial que enseña el
#: panel; más allá de esto, la bitácora ya cuenta lo que pasó.
MAX_HISTORICO = 50

#: Y cuánto se conservan, aunque quepan. Sin las dos reglas el fichero crece
#: para siempre, y lo lee el panel en cada carga de la página.
DIAS_HISTORICO = 7


class AlmacenMisiones:
    """Las misiones puntuales del agente, en memoria y en disco.

    La copia en memoria es la verdad; el fichero es su reflejo, y se reescribe
    entero en cada cambio con `escribir_json_atomico`. Si el volcado falla, la
    copia en memoria **se revierte**: un almacén que dice una cosa y un fichero
    que dice otra es peor que un cambio perdido, porque el panel lee el fichero
    y el planificador lee el objeto.
    """

    def __init__(self, settings: Settings) -> None:
        """Prepara el almacén vacío; no toca el disco hasta `cargar`.

        Args:
            settings: Configuración del agente, por `data_dir` y por los dos
                ajustes de reintento.
        """
        self._settings = settings
        self._misiones: list[MisionPuntual] = []
        self._pestillo = asyncio.Lock()

    # --- Arranque -------------------------------------------------------------

    async def cargar(self) -> None:
        """Lee el fichero. Nunca lanza: `cargar_misiones` ya degrada."""
        async with self._pestillo:
            self._misiones = list(cargar_misiones(self._settings.data_dir).misiones)
        logger.info(f"[misiones] {len(self.pendientes())} misión/es puntual/es pendientes")

    async def caducar_vencidas(self, ahora: datetime | None = None) -> list[MisionPuntual]:
        """Marca como caducadas las pendientes cuyo momento ya pasó.

        Se llama **al arrancar**, antes de que el planificador dé su primera
        vuelta, y es donde se cumple la regla de que los disparos perdidos se
        pierden: si el agente estaba apagado a la hora de una misión, esa
        misión no suena a deshoras. Devuelve las caducadas para que quien
        llame las anote en la bitácora — el almacén no escribe ahí.

        Args:
            ahora: El reloj, inyectable en los tests.

        Returns:
            Las misiones que acaban de quedar caducadas.
        """
        momento = ahora or datetime.now()
        async with self._pestillo:
            vencidas = [m for m in self._misiones if m.pendiente and m.cuando < momento]
            if not vencidas:
                return []
            for mision in vencidas:
                mision.estado = EstadoMision.CADUCADA
            await self._volcar()
        for mision in vencidas:
            retraso = int((momento - mision.cuando).total_seconds() // 60)
            logger.warning(
                f"[misiones] '{mision.id}' caducó: su hora pasó hace {retraso} min "
                "con el agente apagado"
            )
        return vencidas

    # --- Consulta -------------------------------------------------------------

    def pendientes(self) -> list[MisionPuntual]:
        """Las que aún no han sonado, ordenadas por su momento.

        Síncrono y sin pestillo a propósito: devuelve una lista nueva, y los
        objetos que hay dentro solo los muta este almacén desde el bucle de
        eventos. Que sea síncrono es lo que permite consultarla desde
        `_disparar_puntuales` sin ceder el control a mitad de la vuelta.
        """
        return sorted((m for m in self._misiones if m.pendiente), key=lambda m: m.cuando)

    def buscar(self, id_mision: str) -> MisionPuntual | None:
        """Devuelve la misión con ese id, en cualquier estado, o `None`."""
        return next((m for m in self._misiones if m.id == id_mision), None)

    def _ocupados(self) -> set[str]:
        """Ids que un id nuevo no puede repetir.

        Incluye los de las tareas del panel porque **comparten carpeta de
        resultados**: dos encargos distintos con el mismo id mezclarían las
        respuestas de sus cuestionarios en la misma carpeta.
        """
        ids = {m.id for m in self._misiones}
        ids |= {t.id for t in cargar_tareas(self._settings.data_dir).tareas}
        return ids

    # --- Mutación -------------------------------------------------------------

    async def crear(
        self,
        *,
        cuando: datetime,
        mision: str,
        contacto_numero: str,
        contacto_nombre: str = "",
        guardar_respuestas: bool = False,
        origen: str = "voz",
        intento: int = 0,
        id_tarea_origen: str = "",
        ahora: datetime | None = None,
    ) -> MisionPuntual:
        """Agenda una misión puntual nueva y la deja escrita.

        Args:
            cuando: El momento del disparo, naive en hora de la placa.
            mision: El encargo, redactado para el modelo.
            contacto_numero: El número que se marcará.
            contacto_nombre: A quién se llama, para el prompt.
            guardar_respuestas: Si es un cuestionario.
            origen: `voz` o `reintento`.
            intento: Cuántas veces se marcó antes por este mismo encargo.
            id_tarea_origen: La tarea del panel de la que esto es reintento.
            ahora: El reloj, inyectable en los tests.

        Returns:
            La misión creada.

        Raises:
            OSError: Si no se pudo escribir el fichero.
        """
        async with self._pestillo:
            nueva = MisionPuntual(
                id=nuevo_id_mision(cuando, self._ocupados()),
                cuando=cuando,
                mision=mision,
                contacto_numero=contacto_numero,
                contacto_nombre=contacto_nombre,
                guardar_respuestas=guardar_respuestas,
                origen=origen,
                intento=intento,
                id_tarea_origen=id_tarea_origen,
                creada_en=ahora or datetime.now(),
            )
            self._misiones.append(nueva)
            await self._volcar()
        logger.info(f"[misiones] agendada '{nueva.id}' para {nueva.cuando:%d/%m %H:%M}")
        return nueva

    async def editar(
        self,
        id_mision: str,
        *,
        cuando: datetime | None = None,
        mision: str = "",
    ) -> MisionPuntual | None:
        """Cambia el momento o el encargo de una misión **pendiente**.

        Solo las pendientes: reescribir una que ya sonó no la haría sonar otra
        vez y dejaría al modelo creyendo que sí.

        Args:
            id_mision: El id exacto de la misión.
            cuando: El momento nuevo, o `None` para dejarlo como está.
            mision: El encargo nuevo, o vacío para dejarlo como está.

        Returns:
            La misión ya cambiada, o `None` si no existe o no está pendiente.

        Raises:
            OSError: Si no se pudo escribir el fichero.
        """
        async with self._pestillo:
            actual = next((m for m in self._misiones if m.id == id_mision and m.pendiente), None)
            if actual is None:
                return None
            if cuando is not None:
                actual.cuando = cuando
            if mision.strip():
                actual.mision = mision.strip()
            await self._volcar()
        logger.info(f"[misiones] '{actual.id}' movida a {actual.cuando:%d/%m %H:%M}")
        return actual

    async def cancelar(self, id_mision: str) -> MisionPuntual | None:
        """Retira una misión pendiente del calendario.

        Args:
            id_mision: El id exacto de la misión.

        Returns:
            La misión cancelada, o `None` si no existe o ya no estaba
            pendiente. Cancelar algo que ya sonó no es un error: el panel
            puede tener el botón de una misión que se disparó mientras se
            miraba la página.

        Raises:
            OSError: Si no se pudo escribir el fichero.
        """
        return await self.marcar(id_mision, EstadoMision.CANCELADA)

    async def marcar(self, id_mision: str, estado: EstadoMision) -> MisionPuntual | None:
        """Cambia el estado de una misión pendiente.

        Args:
            id_mision: El id exacto de la misión.
            estado: El estado nuevo.

        Returns:
            La misión cambiada, o `None` si no existe o no estaba pendiente.

        Raises:
            OSError: Si no se pudo escribir el fichero.
        """
        async with self._pestillo:
            actual = next((m for m in self._misiones if m.id == id_mision and m.pendiente), None)
            if actual is None:
                return None
            actual.estado = estado
            await self._volcar()
        logger.info(f"[misiones] '{actual.id}' pasa a {estado}")
        return actual

    async def aplicar_cancelaciones(self, ids: Collection[str]) -> list[MisionPuntual]:
        """Cancela de golpe las misiones que el panel pidió retirar.

        Los ids que no correspondan a nada pendiente se ignoran sin ruido: el
        fichero del panel conserva ids ya aplicados hasta que él mismo los
        pode, así que llegan repetidos en cada vuelta y eso es lo normal.

        Args:
            ids: Los identificadores que dejó apuntados el panel.

        Returns:
            Las misiones que esta llamada acaba de cancelar.

        Raises:
            OSError: Si no se pudo escribir el fichero.
        """
        pedidos = set(ids)
        if not pedidos:
            return []
        async with self._pestillo:
            afectadas = [m for m in self._misiones if m.pendiente and m.id in pedidos]
            if not afectadas:
                return []
            for mision in afectadas:
                mision.estado = EstadoMision.CANCELADA
            await self._volcar()
        for mision in afectadas:
            logger.info(f"[misiones] '{mision.id}' cancelada desde el panel")
        return afectadas

    async def programar_reintento(
        self, encargo: EncargoLlamada, cuando: datetime, *, ahora: datetime | None = None
    ) -> MisionPuntual:
        """Agenda otro intento de un encargo que no cuajó.

        El reintento hereda la carpeta de resultados del encargo original —vía
        `id_tarea_origen`— para que las respuestas de un cuestionario que se
        conteste al segundo intento aparezcan donde alguien las va a buscar.

        Args:
            encargo: La tarea o misión que no cuajó.
            cuando: Cuándo se vuelve a marcar.
            ahora: El reloj, inyectable en los tests.

        Returns:
            La misión de reintento.

        Raises:
            OSError: Si no se pudo escribir el fichero.
        """
        intento = encargo.intento + 1
        async with self._pestillo:
            nueva = MisionPuntual(
                id=self._id_reintento_libre(encargo.id, intento, cuando),
                cuando=cuando,
                mision=encargo.mision,
                contacto_numero=encargo.contacto_numero,
                contacto_nombre=encargo.contacto_nombre,
                guardar_respuestas=encargo.guardar_respuestas,
                origen="reintento",
                intento=intento,
                id_tarea_origen=encargo.id_resultados,
                creada_en=ahora or datetime.now(),
            )
            self._misiones.append(nueva)
            await self._volcar()
        logger.info(
            f"[misiones] reintento {intento} de '{encargo.id}' agendado "
            f"para {cuando:%d/%m %H:%M} como '{nueva.id}'"
        )
        return nueva

    def _id_reintento_libre(self, origen: str, intento: int, cuando: datetime) -> str:
        """El id derivado, o uno inventado si por lo que sea ya está cogido."""
        candidato = id_de_reintento(origen, intento, cuando)
        ocupados = self._ocupados()
        if candidato not in ocupados:
            return candidato
        return nuevo_id_mision(cuando, ocupados)

    # --- Disco ----------------------------------------------------------------

    async def _volcar(self) -> None:
        """Purga lo viejo y reescribe el fichero entero. Con el pestillo puesto.

        La purga vive aquí dentro y no en un método aparte para que no se
        pueda olvidar: cualquier camino que escriba pasa por aquí.

        Raises:
            OSError: Si no se pudo escribir. La copia en memoria queda como
                estaba antes del cambio.
        """
        previas = list(self._misiones)
        self._misiones = self._purgadas()
        datos = MisionesAgente(generado_en=datetime.now(), misiones=self._misiones).model_dump(
            mode="json"
        )
        try:
            escribir_json_atomico(ruta_misiones_agente(self._settings.data_dir), datos)
        except OSError as e:
            # Revertir es lo que impide que el objeto y el fichero se
            # contradigan: el planificador lee el objeto y el panel el fichero.
            self._misiones = previas
            logger.error(f"[misiones] no se pudo escribir el fichero: {e}. Cambio revertido.")
            raise

    def _purgadas(self) -> list[MisionPuntual]:
        """La lista sin las terminales viejas o sobrantes."""
        pendientes = [m for m in self._misiones if m.pendiente]
        limite = datetime.now() - timedelta(days=DIAS_HISTORICO)
        terminales = [
            m for m in self._misiones if m.estado in ESTADOS_TERMINALES and m.cuando >= limite
        ]
        terminales.sort(key=lambda m: m.cuando, reverse=True)
        return pendientes + terminales[:MAX_HISTORICO]


__all__ = ["DIAS_HISTORICO", "MAX_HISTORICO", "AlmacenMisiones"]
