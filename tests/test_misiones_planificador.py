"""El planificador con misiones puntuales: disparo, caducidad, cancelación y reintento.

Lo que se fija: que una misión puntual suena una sola vez y a su hora, que la
que se pasó de hora no suena (la regla de que los disparos perdidos se
pierden), que una cancelación del panel llega por su fichero, y las reglas del
reintento — solo si no cuajó, nunca tras una conversación, con tope, y anotado
bajo el id de la tarea original para que la página de Resultados lo vea.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from voice_agent import tareas_programadas
from voice_agent.misiones_agente import AlmacenMisiones
from voice_agent.tareas_programadas import (
    MARGEN_TICK_SECS,
    MisionesLlamada,
    ProgramadorTareas,
    SalaActual,
)
from voice_agent_core.config import Settings
from voice_agent_core.misiones import CancelacionesMisiones, EstadoMision
from voice_agent_core.rutas import (
    escribir_json_atomico,
    ruta_bitacora_tareas,
    ruta_misiones_canceladas,
)
from voice_agent_core.tareas import TareaProgramada, TipoTarea
from voice_agent_core.telefonia import EstadoLlamada, EstadoTelefonia, Llamada

AHORA = datetime(2026, 8, 12, 17, 0)


class RelojFalso:
    """Un `datetime.now` que se mueve a mano."""

    def __init__(self, momento: datetime) -> None:
        self.momento = momento

    def __call__(self) -> datetime:
        return self.momento


def llamada(estado: EstadoLlamada = EstadoLlamada.SONANDO) -> Llamada:
    return Llamada(id="voicecall01", estado=estado, numero="3046411802", entrante=False)


class ClienteFalso:
    """Un puente de mentira al que nadie contesta, salvo que se le programe."""

    def __init__(self, estados: list[list[Llamada]] | None = None) -> None:
        self.estados = estados if estados is not None else [[llamada()]] * 50
        self.marcadas: list[str] = []
        self.colgadas: list[str | None] = []

    async def marcar(self, numero: str) -> Llamada:
        self.marcadas.append(numero)
        return llamada()

    async def colgar(self, id_llamada: str | None = None) -> None:
        self.colgadas.append(id_llamada)

    async def estado(self) -> EstadoTelefonia:
        llamadas = self.estados.pop(0) if self.estados else []
        return EstadoTelefonia(disponible=True, llamadas=llamadas)


def tarea_llamada(**cambios: Any) -> TareaProgramada:
    base: dict[str, Any] = {
        "id": "revision-abuela",
        "tipo": TipoTarea.LLAMADA,
        "cron": "0 17 * * *",
        "mision": "Pregúntale cómo se encuentra.",
        "contacto_nombre": "Abuela",
        "contacto_numero": "3046411802",
    }
    base.update(cambios)
    return TareaProgramada.model_validate(base)


def bitacora(tmp_path: Path) -> list[dict[str, Any]]:
    ruta = ruta_bitacora_tareas(tmp_path)
    if not ruta.is_file():
        return []
    return [json.loads(linea) for linea in ruta.read_text(encoding="utf-8").splitlines()]


async def preparar(
    tmp_path: Path,
    cliente: Any = None,
    *,
    momento: datetime = AHORA,
    con_puente: bool = True,
    **ajustes: Any,
) -> tuple[ProgramadorTareas, AlmacenMisiones, RelojFalso, ClienteFalso]:
    settings = Settings(_env_file=None, data_dir=tmp_path, **ajustes)  # type: ignore[call-arg]
    almacen = AlmacenMisiones(settings)
    await almacen.cargar()
    puente = cliente if cliente is not None else ClienteFalso()
    reloj = RelojFalso(momento)
    programador = ProgramadorTareas(
        settings,
        SalaActual(),
        cast(Any, puente) if con_puente else None,
        MisionesLlamada(),
        ahora=reloj,
        almacen=almacen,
    )
    return programador, almacen, reloj, cast(ClienteFalso, puente)


async def crear(almacen: AlmacenMisiones, **cambios: Any) -> Any:
    base: dict[str, Any] = {
        "cuando": AHORA,
        "mision": "Retomar el control del día cinco.",
        "contacto_numero": "3046411802",
        "ahora": AHORA,
    }
    base.update(cambios)
    return await almacen.crear(**base)


@pytest.fixture(autouse=True)
def _tiempos_cortos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Los sondeos y timeouts reales son de segundos; aquí, de milisegundos."""
    monkeypatch.setattr(tareas_programadas, "SONDEO_LLAMADA_SECS", 0.01)
    monkeypatch.setattr(tareas_programadas, "TIMEOUT_SALIENTE_SECS", 0.3)
    monkeypatch.setattr(tareas_programadas, "SONDEO_CONFIRMACION_SECS", 0.001)


class TestElDisparo:
    async def test_no_dispara_antes_de_la_hora(self, tmp_path: Path) -> None:
        programador, almacen, _, cliente = await preparar(
            tmp_path, momento=AHORA - timedelta(minutes=5)
        )
        await crear(almacen)
        await programador._disparar_puntuales()
        assert cliente.marcadas == []
        assert len(almacen.pendientes()) == 1

    async def test_dispara_a_su_hora_una_sola_vez(self, tmp_path: Path) -> None:
        # Con un solo intento configurado no se agenda reintento, así que lo
        # que queda en el calendario es exactamente lo que se disparó (o no).
        programador, almacen, _, cliente = await preparar(tmp_path, tareas_reintentos_max=1)
        creada = await crear(almacen)
        await programador._disparar_puntuales()
        await programador._disparar_puntuales()
        assert cliente.marcadas == ["3046411802"]
        assert almacen.buscar(creada.id).estado is EstadoMision.EJECUTADA  # type: ignore[union-attr]
        assert almacen.pendientes() == []

    async def test_la_primera_futura_corta_el_barrido(self, tmp_path: Path) -> None:
        # Vienen ordenadas, así que en cuanto una no toca, ninguna de las
        # siguientes toca tampoco.
        programador, almacen, _, cliente = await preparar(tmp_path)
        await crear(almacen, cuando=AHORA + timedelta(hours=1))
        await crear(almacen, cuando=AHORA + timedelta(hours=2))
        await programador._disparar_puntuales()
        assert cliente.marcadas == []

    async def test_sin_almacen_no_pasa_nada(self, tmp_path: Path) -> None:
        settings = Settings(_env_file=None, data_dir=tmp_path)  # type: ignore[call-arg]
        programador = ProgramadorTareas(settings, SalaActual(), None, MisionesLlamada())
        await programador._disparar_puntuales()  # no lanza


class TestLaCaducidad:
    async def test_una_vencida_de_hace_rato_no_suena(self, tmp_path: Path) -> None:
        # Los disparos perdidos se pierden: llamar a un postoperado horas
        # tarde es peor que no llamarlo.
        programador, almacen, _, cliente = await preparar(tmp_path)
        creada = await crear(almacen, cuando=AHORA - timedelta(hours=3))
        await programador._disparar_puntuales()
        assert cliente.marcadas == []
        assert almacen.buscar(creada.id).estado is EstadoMision.CADUCADA  # type: ignore[union-attr]

    async def test_la_caducada_queda_anotada_en_la_bitacora(self, tmp_path: Path) -> None:
        # La pérdida tiene que ser visible; si no, es una promesa que se
        # evapora en silencio.
        programador, almacen, _, _ = await preparar(tmp_path)
        await crear(almacen, cuando=AHORA - timedelta(hours=3))
        await programador._disparar_puntuales()
        assert [e["resultado"] for e in bitacora(tmp_path)] == ["caducada"]

    async def test_el_retraso_de_un_tick_todavia_suena(self, tmp_path: Path) -> None:
        # No es una ventana de gracia: es la resolución del propio tick. Sin
        # este margen no sonaría ninguna misión jamás, porque el planificador
        # siempre las ve con algo de retraso.
        programador, almacen, _, cliente = await preparar(tmp_path)
        await crear(almacen, cuando=AHORA - timedelta(seconds=MARGEN_TICK_SECS - 5))
        await programador._disparar_puntuales()
        assert cliente.marcadas == ["3046411802"]

    async def test_sin_puente_se_caduca_y_se_anota(self, tmp_path: Path) -> None:
        programador, almacen, _, _ = await preparar(tmp_path, con_puente=False)
        creada = await crear(almacen)
        await programador._disparar_puntuales()
        assert almacen.buscar(creada.id).estado is EstadoMision.CADUCADA  # type: ignore[union-attr]
        assert [e["resultado"] for e in bitacora(tmp_path)] == ["error"]


class TestLasCancelacionesDelPanel:
    async def test_una_cancelacion_impide_el_disparo(self, tmp_path: Path) -> None:
        programador, almacen, _, cliente = await preparar(tmp_path)
        creada = await crear(almacen)
        escribir_json_atomico(
            ruta_misiones_canceladas(tmp_path),
            CancelacionesMisiones(ids=[creada.id]).model_dump(mode="json"),
        )
        await programador._aplicar_cancelaciones_si_cambio()
        await programador._disparar_puntuales()
        assert cliente.marcadas == []
        assert almacen.buscar(creada.id).estado is EstadoMision.CANCELADA  # type: ignore[union-attr]

    async def test_la_cancelacion_queda_anotada(self, tmp_path: Path) -> None:
        programador, almacen, _, _ = await preparar(tmp_path)
        creada = await crear(almacen)
        escribir_json_atomico(
            ruta_misiones_canceladas(tmp_path),
            CancelacionesMisiones(ids=[creada.id]).model_dump(mode="json"),
        )
        await programador._aplicar_cancelaciones_si_cambio()
        assert [e["resultado"] for e in bitacora(tmp_path)] == ["cancelada"]

    async def test_un_id_desconocido_no_molesta(self, tmp_path: Path) -> None:
        # El fichero del panel conserva ids ya aplicados hasta que los pode:
        # llegan repetidos en cada vuelta y eso es lo normal.
        programador, almacen, _, cliente = await preparar(tmp_path)
        await crear(almacen)
        escribir_json_atomico(
            ruta_misiones_canceladas(tmp_path),
            CancelacionesMisiones(ids=["agenda-que-ya-no-existe"]).model_dump(mode="json"),
        )
        await programador._aplicar_cancelaciones_si_cambio()
        await programador._disparar_puntuales()
        assert cliente.marcadas == ["3046411802"]

    async def test_sin_fichero_no_pasa_nada(self, tmp_path: Path) -> None:
        programador, almacen, _, _ = await preparar(tmp_path)
        await crear(almacen)
        await programador._aplicar_cancelaciones_si_cambio()  # no lanza
        assert len(almacen.pendientes()) == 1


class TestElReintento:
    async def test_sin_respuesta_programa_otro_intento(self, tmp_path: Path) -> None:
        programador, almacen, _, _ = await preparar(tmp_path)
        await programador._ejecutar_llamada(tarea_llamada(), AHORA)
        pendientes = almacen.pendientes()
        assert len(pendientes) == 1
        assert pendientes[0].intento == 1
        assert pendientes[0].origen == "reintento"

    async def test_el_reintento_se_agenda_con_la_espera_configurada(self, tmp_path: Path) -> None:
        programador, almacen, _, _ = await preparar(tmp_path, tareas_reintento_espera_min=45)
        await programador._ejecutar_llamada(tarea_llamada(), AHORA)
        assert almacen.pendientes()[0].cuando == AHORA + timedelta(minutes=45)

    async def test_una_contestada_no_reintenta(self, tmp_path: Path) -> None:
        # Volver a llamar a quien ya cogió el teléfono es acosarlo.
        misiones = MisionesLlamada()

        class ClienteQueDescuelga(ClienteFalso):
            async def estado(self) -> EstadoTelefonia:
                est = await super().estado()
                if any(c.estado is EstadoLlamada.EN_CURSO for c in est.llamadas):
                    await misiones.tomar_si_en_curso(cast(Any, ClienteFalso([est.llamadas])))
                return est

        cliente = ClienteQueDescuelga(
            estados=[
                [llamada()],
                [llamada(estado=EstadoLlamada.EN_CURSO)],
                [],  # colgaron
            ]
        )
        programador, almacen, _, _ = await preparar(tmp_path, cliente)
        programador._misiones = misiones
        await programador._ejecutar_llamada(tarea_llamada(), AHORA)
        assert [e["resultado"] for e in bitacora(tmp_path)] == ["llamada_contestada"]
        assert almacen.pendientes() == []

    async def test_para_al_llegar_al_maximo(self, tmp_path: Path) -> None:
        programador, almacen, _, _ = await preparar(tmp_path, tareas_reintentos_max=2)
        await programador._ejecutar_llamada(tarea_llamada(), AHORA)  # intento 0 -> agenda el 1
        primero = almacen.pendientes()[0]
        await programador._ejecutar_llamada(primero, AHORA)  # intento 1 -> ya no agenda nada
        # El primer reintento sigue en la lista porque quien lo marca como
        # ejecutado es `_disparar_puntuales`, no `_ejecutar_llamada`; lo que
        # importa es que no haya nacido un segundo.
        assert [m.intento for m in almacen.pendientes()] == [1]

    async def test_un_solo_intento_no_reintenta_nunca(self, tmp_path: Path) -> None:
        programador, almacen, _, _ = await preparar(tmp_path, tareas_reintentos_max=1)
        await programador._ejecutar_llamada(tarea_llamada(), AHORA)
        assert almacen.pendientes() == []

    async def test_sin_puente_no_se_reintenta(self, tmp_path: Path) -> None:
        # Reintentar contra un puente caído no lo levanta.
        programador, almacen, _, _ = await preparar(tmp_path, con_puente=False)
        await programador._ejecutar_llamada(tarea_llamada(), AHORA)
        assert almacen.pendientes() == []
        assert [e["resultado"] for e in bitacora(tmp_path)] == ["error"]

    async def test_el_reintento_anota_bajo_el_id_de_la_tarea_original(self, tmp_path: Path) -> None:
        # La página de Resultados del panel filtra la bitácora por el nombre
        # de la tarea: un reintento apuntado con su id propio no se vería.
        programador, almacen, _, _ = await preparar(tmp_path)
        await programador._ejecutar_llamada(tarea_llamada(), AHORA)
        reintento = almacen.pendientes()[0]
        await programador._ejecutar_llamada(reintento, AHORA)

        entradas = bitacora(tmp_path)
        assert {e["id_tarea"] for e in entradas} == {"revision-abuela"}
        assert entradas[1]["id_mision"] == reintento.id
        assert entradas[1]["intento"] == 1

    async def test_una_tarea_normal_no_lleva_id_de_mision(self, tmp_path: Path) -> None:
        programador, _, _, _ = await preparar(tmp_path, tareas_reintentos_max=1)
        await programador._ejecutar_llamada(tarea_llamada(), AHORA)
        assert "id_mision" not in bitacora(tmp_path)[0]
