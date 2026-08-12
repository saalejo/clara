"""El almacén de misiones: el único escritor de `misiones_agente.json`.

Lo que se fija: que lo que escribe el agente lo lee el panel sin traductor, que
dos escrituras a la vez —una herramienta en mitad de una llamada y el
planificador en su vuelta— no se pisan, que las misiones que vencieron con el
agente apagado no suenan al arrancar, y que el fichero no crece sin fin.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from voice_agent.misiones_agente import DIAS_HISTORICO, MAX_HISTORICO, AlmacenMisiones
from voice_agent_core.config import Settings
from voice_agent_core.misiones import EstadoMision, cargar_misiones
from voice_agent_core.tareas import TareaProgramada, TipoTarea

AHORA = datetime(2026, 8, 12, 10, 0)
LUEGO = AHORA + timedelta(hours=2)


def almacen_de(tmp_path: Path) -> AlmacenMisiones:
    return AlmacenMisiones(Settings(_env_file=None, data_dir=tmp_path))  # type: ignore[call-arg]


async def crear(almacen: AlmacenMisiones, **cambios: Any) -> Any:
    base: dict[str, Any] = {
        "cuando": LUEGO,
        "mision": "Retomar el control del día cinco.",
        "contacto_numero": "3046411802",
        "ahora": AHORA,
    }
    base.update(cambios)
    return await almacen.crear(**base)


class TestElIdaYVuelta:
    async def test_crear_escribe_un_json_que_el_panel_lee(self, tmp_path: Path) -> None:
        almacen = almacen_de(tmp_path)
        creada = await crear(almacen)
        # `cargar_misiones` es el lector real del panel: el round-trip es el
        # contrato entre los dos contenedores.
        leidas = cargar_misiones(tmp_path).pendientes
        assert [m.id for m in leidas] == [creada.id]
        assert leidas[0].contacto_numero == "3046411802"

    async def test_cargar_recupera_lo_que_habia(self, tmp_path: Path) -> None:
        creada = await crear(almacen_de(tmp_path))
        otro = almacen_de(tmp_path)
        await otro.cargar()
        assert [m.id for m in otro.pendientes()] == [creada.id]

    async def test_las_pendientes_salen_ordenadas_por_su_momento(self, tmp_path: Path) -> None:
        almacen = almacen_de(tmp_path)
        tarde = await crear(almacen, cuando=LUEGO + timedelta(hours=3))
        pronto = await crear(almacen, cuando=LUEGO)
        assert [m.id for m in almacen.pendientes()] == [pronto.id, tarde.id]


class TestLaConcurrencia:
    async def test_dos_escrituras_a_la_vez_no_se_pisan(self, tmp_path: Path) -> None:
        # Sin el asyncio.Lock, el await de la escritura entre leer y volcar
        # pierde entera la escritura de la otra tarea. Aquí eso se vería como
        # un fichero con menos misiones de las creadas.
        almacen = almacen_de(tmp_path)
        await asyncio.gather(*(crear(almacen, mision=f"encargo {i}") for i in range(8)))
        assert len(almacen.pendientes()) == 8
        assert len(cargar_misiones(tmp_path).pendientes) == 8

    async def test_ocho_creadas_a_la_vez_tienen_ocho_ids_distintos(self, tmp_path: Path) -> None:
        almacen = almacen_de(tmp_path)
        creadas = await asyncio.gather(*(crear(almacen) for _ in range(8)))
        assert len({m.id for m in creadas}) == 8


class TestElArranque:
    async def test_las_vencidas_quedan_caducadas_y_no_suenan(self, tmp_path: Path) -> None:
        # La regla del proyecto: los disparos perdidos se pierden. Una misión
        # vieja sonando a deshoras es peor que una misión perdida.
        almacen = almacen_de(tmp_path)
        vencida = await crear(almacen, cuando=AHORA - timedelta(hours=1))
        caducadas = await almacen.caducar_vencidas(ahora=AHORA)
        assert [m.id for m in caducadas] == [vencida.id]
        assert almacen.pendientes() == []
        assert almacen.buscar(vencida.id).estado is EstadoMision.CADUCADA  # type: ignore[union-attr]

    async def test_las_futuras_sobreviven(self, tmp_path: Path) -> None:
        # Para que el test de arriba no pase por caducarlo todo.
        almacen = almacen_de(tmp_path)
        futura = await crear(almacen, cuando=AHORA + timedelta(hours=1))
        await almacen.caducar_vencidas(ahora=AHORA)
        assert [m.id for m in almacen.pendientes()] == [futura.id]

    async def test_sin_vencidas_no_reescribe_nada(self, tmp_path: Path) -> None:
        almacen = almacen_de(tmp_path)
        await crear(almacen)
        assert await almacen.caducar_vencidas(ahora=AHORA) == []


class TestLaMutacion:
    async def test_editar_mueve_la_hora(self, tmp_path: Path) -> None:
        almacen = almacen_de(tmp_path)
        creada = await crear(almacen)
        nueva = LUEGO + timedelta(days=1)
        assert (await almacen.editar(creada.id, cuando=nueva)) is not None
        assert cargar_misiones(tmp_path).pendientes[0].cuando == nueva

    async def test_editar_sin_encargo_no_lo_borra(self, tmp_path: Path) -> None:
        almacen = almacen_de(tmp_path)
        creada = await crear(almacen, mision="el encargo original")
        await almacen.editar(creada.id, cuando=LUEGO + timedelta(hours=1))
        assert almacen.pendientes()[0].mision == "el encargo original"

    async def test_editar_una_desconocida_devuelve_none(self, tmp_path: Path) -> None:
        assert await almacen_de(tmp_path).editar("agenda-inventada", mision="x") is None

    async def test_editar_una_ya_ejecutada_devuelve_none(self, tmp_path: Path) -> None:
        # Reescribirla no la haría sonar otra vez y dejaría al modelo creyendo
        # que sí.
        almacen = almacen_de(tmp_path)
        creada = await crear(almacen)
        await almacen.marcar(creada.id, EstadoMision.EJECUTADA)
        assert await almacen.editar(creada.id, mision="otra cosa") is None

    async def test_cancelar_la_saca_del_calendario(self, tmp_path: Path) -> None:
        almacen = almacen_de(tmp_path)
        creada = await crear(almacen)
        assert (await almacen.cancelar(creada.id)) is not None
        assert almacen.pendientes() == []

    async def test_cancelar_una_desconocida_no_es_un_error(self, tmp_path: Path) -> None:
        # El fichero del panel conserva ids ya aplicados hasta que él los pode.
        assert await almacen_de(tmp_path).cancelar("agenda-inventada") is None

    async def test_aplicar_cancelaciones_del_panel(self, tmp_path: Path) -> None:
        almacen = almacen_de(tmp_path)
        una = await crear(almacen)
        otra = await crear(almacen)
        aplicadas = await almacen.aplicar_cancelaciones([una.id, "agenda-que-ya-no-existe"])
        assert [m.id for m in aplicadas] == [una.id]
        assert [m.id for m in almacen.pendientes()] == [otra.id]

    async def test_aplicar_cancelaciones_vacias_no_escribe(self, tmp_path: Path) -> None:
        assert await almacen_de(tmp_path).aplicar_cancelaciones([]) == []


class TestElReintento:
    async def test_hereda_la_carpeta_de_resultados_de_la_tarea(self, tmp_path: Path) -> None:
        tarea = TareaProgramada.model_validate(
            {
                "id": "revision-abuela",
                "cron": "0 9 * * 1",
                "tipo": TipoTarea.LLAMADA,
                "mision": "Pregúntale cómo se encuentra.",
                "contacto_numero": "3046411802",
                "contacto_nombre": "Abuela",
            }
        )
        reintento = await almacen_de(tmp_path).programar_reintento(tarea, LUEGO, ahora=AHORA)
        assert reintento.id_resultados == "revision-abuela"
        assert reintento.intento == 1
        assert reintento.origen == "reintento"
        assert reintento.contacto_nombre == "Abuela"

    async def test_el_reintento_de_un_reintento_sube_el_contador(self, tmp_path: Path) -> None:
        almacen = almacen_de(tmp_path)
        primero = await almacen.programar_reintento(await crear(almacen), LUEGO, ahora=AHORA)
        segundo = await almacen.programar_reintento(primero, LUEGO, ahora=AHORA)
        assert (primero.intento, segundo.intento) == (1, 2)

    async def test_dos_reintentos_del_mismo_origen_no_chocan(self, tmp_path: Path) -> None:
        almacen = almacen_de(tmp_path)
        origen = await crear(almacen)
        uno = await almacen.programar_reintento(origen, LUEGO, ahora=AHORA)
        otro = await almacen.programar_reintento(origen, LUEGO, ahora=AHORA)
        assert uno.id != otro.id


class TestLaPurga:
    async def test_las_terminales_viejas_se_van(self, tmp_path: Path) -> None:
        almacen = almacen_de(tmp_path)
        vieja = await crear(almacen, cuando=datetime.now() - timedelta(days=DIAS_HISTORICO + 1))
        await almacen.marcar(vieja.id, EstadoMision.EJECUTADA)
        # La purga corre dentro de _volcar, así que basta con escribir otra vez.
        await crear(almacen)
        assert almacen.buscar(vieja.id) is None

    async def test_las_terminales_recientes_se_quedan(self, tmp_path: Path) -> None:
        almacen = almacen_de(tmp_path)
        reciente = await crear(almacen, cuando=datetime.now() - timedelta(hours=1))
        await almacen.marcar(reciente.id, EstadoMision.EJECUTADA)
        await crear(almacen)
        assert almacen.buscar(reciente.id) is not None

    async def test_las_pendientes_no_se_purgan_jamas(self, tmp_path: Path) -> None:
        # Aunque sean más que el tope: lo que se acota es el historial, no el
        # calendario.
        almacen = almacen_de(tmp_path)
        for i in range(MAX_HISTORICO + 5):
            await crear(almacen, cuando=LUEGO + timedelta(minutes=i))
        assert len(almacen.pendientes()) == MAX_HISTORICO + 5


class TestElEspacioDeNombres:
    async def test_una_mision_no_puede_pisar_una_tarea_del_panel(self, tmp_path: Path) -> None:
        # Comparten la carpeta de resultados: dos encargos con el mismo id
        # mezclarían las respuestas de sus cuestionarios.
        from voice_agent_core.rutas import escribir_json_atomico, ruta_tareas
        from voice_agent_core.tareas import TareasConfig

        colision = f"agenda-{LUEGO:%Y%m%d-%H%M}-dead"
        escribir_json_atomico(
            ruta_tareas(tmp_path),
            TareasConfig(
                tareas=[
                    TareaProgramada.model_validate(
                        {"id": colision, "cron": "0 9 * * 1", "mision": "x"}
                    )
                ]
            ).model_dump(mode="json"),
        )
        creada = await crear(almacen_de(tmp_path))
        assert creada.id != colision
