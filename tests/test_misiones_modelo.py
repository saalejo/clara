"""Los modelos de las misiones puntuales y el contrato que comparten con las tareas.

Lo que se fija aquí es lo que rompe en silencio: que una tarea del panel y una
misión del agente siguen siendo intercambiables para el planificador (si dejan
de serlo, mypy lo caza en estos tests), que un `cuando` con zona horaria no se
cuela —enmudecería el planificador entero— y que un id inventado sigue valiendo
como nombre de carpeta.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from voice_agent_core.misiones import (
    PREFIJO_AGENDA,
    EncargoLlamada,
    EstadoMision,
    MisionesAgente,
    MisionPuntual,
    cargar_cancelaciones,
    cargar_misiones,
    id_de_reintento,
    interpretar_cuando,
    nuevo_id_mision,
)
from voice_agent_core.rutas import (
    escribir_json_atomico,
    ruta_misiones_agente,
    ruta_misiones_canceladas,
)
from voice_agent_core.tareas import TareaProgramada, TipoTarea

CUANDO = datetime(2026, 8, 13, 17, 0)


def mision(**cambios: object) -> MisionPuntual:
    base: dict[str, object] = {
        "id": "agenda-20260813-1700-a3f9",
        "cuando": CUANDO,
        "mision": "Retomar el control del día cinco.",
        "contacto_numero": "3046411802",
    }
    base.update(cambios)
    return MisionPuntual.model_validate(base)


def tarea_llamada(**cambios: object) -> TareaProgramada:
    base: dict[str, object] = {
        "id": "revision-abuela",
        "cron": "0 9 * * 1",
        "mision": "Pregúntale cómo se encuentra.",
        "tipo": TipoTarea.LLAMADA,
        "contacto_numero": "3046411802",
    }
    base.update(cambios)
    return TareaProgramada.model_validate(base)


class TestElContratoCompartido:
    """`EncargoLlamada` es lo que permite un solo ejecutor para dos calendarios.

    Estas dos asignaciones no comprueban nada en tiempo de ejecución: son para
    **mypy**, que sí revisa los tests. Si alguien le añade un campo al Protocol
    y se olvida de uno de los dos implementadores, el fallo sale aquí y no en
    la placa a las nueve de la mañana.
    """

    def test_una_tarea_del_panel_sirve_de_encargo(self) -> None:
        encargo: EncargoLlamada = tarea_llamada()
        assert encargo.id_resultados == encargo.id
        assert encargo.intento == 0

    def test_una_mision_puntual_sirve_de_encargo(self) -> None:
        encargo: EncargoLlamada = mision()
        assert encargo.id_resultados == encargo.id

    def test_el_reintento_guarda_bajo_el_id_de_la_tarea_original(self) -> None:
        # Si esto se rompe, las respuestas del segundo intento caen en una
        # carpeta que la página de Resultados de la tarea no mira.
        encargo: EncargoLlamada = mision(id_tarea_origen="revision-abuela")
        assert encargo.id_resultados == "revision-abuela"
        assert encargo.id != encargo.id_resultados


class TestLaFecha:
    def test_un_cuando_con_zona_horaria_queda_naive(self) -> None:
        # El planificador compara con datetime.now(), que es naive: un aware
        # lanzaría TypeError dentro de correr(), que se lo traga con
        # logger.exception. Síntoma: las tareas dejan de sonar y no hay nada
        # roto a la vista.
        con_zona = datetime(2026, 8, 13, 17, 0, tzinfo=timezone(timedelta(hours=-5)))
        assert mision(cuando=con_zona).cuando.tzinfo is None

    def test_el_cuando_naive_se_respeta_tal_cual(self) -> None:
        assert mision().cuando == CUANDO

    @pytest.mark.parametrize(
        "texto",
        [
            "2026-08-13 17:00",
            "2026-08-13T17:00",
            "2026-08-13 17:00:00",
            "2026-08-13T17:00:00",
            "2026/08/13 17:00",
        ],
    )
    def test_interpretar_cuando_acepta_lo_que_escriba_el_modelo(self, texto: str) -> None:
        # Se le pide un solo formato en el docstring, pero escribe el que le
        # sale; rechazarlo por una T sería fallar por nada.
        assert interpretar_cuando(texto, "America/Bogota") == CUANDO

    def test_un_iso_con_offset_se_traduce_a_hora_de_la_placa(self) -> None:
        resultado = interpretar_cuando("2026-08-13T17:00:00-05:00", "America/Bogota")
        assert resultado.tzinfo is None

    def test_una_fecha_ininteligible_lanza_valueerror(self) -> None:
        with pytest.raises(ValueError, match="No entiendo la fecha"):
            interpretar_cuando("mañana a las cinco", "America/Bogota")

    def test_una_zona_desconocida_no_revienta(self) -> None:
        # Un ajuste mal escrito no puede tumbar una llamada a la herramienta.
        assert interpretar_cuando("2026-08-13 17:00", "Marte/Olympus") == CUANDO


class TestLosIdentificadores:
    def test_el_id_vale_como_nombre_de_carpeta(self) -> None:
        with pytest.raises(ValueError, match="no vale"):
            mision(id="../../etc/passwd")

    def test_dos_misiones_para_la_misma_hora_no_chocan(self) -> None:
        primero = nuevo_id_mision(CUANDO, set())
        segundo = nuevo_id_mision(CUANDO, {primero})
        assert primero != segundo

    def test_el_id_lleva_el_prefijo_reservado(self) -> None:
        # Es lo que impide que una misión pise la carpeta de resultados de una
        # tarea del panel; `TareaForm` cierra el trato por el otro lado.
        assert nuevo_id_mision(CUANDO, set()).startswith(f"{PREFIJO_AGENDA}-")

    def test_el_id_inventado_es_un_id_valido(self) -> None:
        mision(id=nuevo_id_mision(CUANDO, set()))  # no lanza

    def test_el_id_de_reintento_deriva_del_origen(self) -> None:
        derivado = id_de_reintento("revision-abuela", 1, CUANDO)
        assert derivado.startswith("revision-abuela-r1")
        mision(id=derivado)  # sigue valiendo como carpeta

    def test_el_id_de_reintento_de_un_origen_larguisimo_se_recorta(self) -> None:
        derivado = id_de_reintento("a" * 200, 3, CUANDO)
        assert len(derivado) < 60
        mision(id=derivado)


class TestLaLectura:
    def test_sin_fichero_devuelve_vacio(self, tmp_path: Path) -> None:
        # El panel arranca antes que el agente, y hasta que alguien no agende
        # nada por voz este fichero no existe. Es el caso normal.
        assert cargar_misiones(tmp_path).misiones == []
        assert cargar_cancelaciones(tmp_path).ids == []

    def test_un_json_roto_degrada_sin_lanzar(self, tmp_path: Path) -> None:
        ruta = ruta_misiones_agente(tmp_path)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text("{esto no es json", encoding="utf-8")
        assert cargar_misiones(tmp_path).misiones == []

    def test_unas_cancelaciones_rotas_degradan_sin_lanzar(self, tmp_path: Path) -> None:
        ruta = ruta_misiones_canceladas(tmp_path)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text("[", encoding="utf-8")
        assert cargar_cancelaciones(tmp_path).ids == []

    def test_el_ida_y_vuelta_conserva_la_mision(self, tmp_path: Path) -> None:
        # El round-trip ES el contrato entre el agente y el panel.
        original = MisionesAgente(misiones=[mision()])
        escribir_json_atomico(ruta_misiones_agente(tmp_path), original.model_dump(mode="json"))
        assert cargar_misiones(tmp_path).misiones == original.misiones

    def test_las_pendientes_salen_ordenadas_y_sin_las_terminadas(self) -> None:
        tarde = mision(id="agenda-b", cuando=CUANDO + timedelta(hours=2))
        pronto = mision(id="agenda-a", cuando=CUANDO)
        hecha = mision(id="agenda-c", estado=EstadoMision.EJECUTADA)
        config = MisionesAgente(misiones=[tarde, hecha, pronto])
        assert [m.id for m in config.pendientes] == ["agenda-a", "agenda-b"]
