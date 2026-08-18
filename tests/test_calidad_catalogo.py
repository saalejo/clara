"""El catálogo de escenarios de calidad es un contrato: se vigila su forma."""

from __future__ import annotations

import pytest

from voice_agent_core.calidad import (
    CATALOGO,
    NOMBRE_CATEGORIA,
    PATRON_ID,
    CategoriaEscenario,
    Escenario,
    EstadoLote,
    ResultadoEscenario,
    SolicitudCalidad,
    Turno,
    UsoLLM,
    VeredictoJuez,
    escenario_por_id,
    por_categoria,
)
from voice_agent_core.evaluaciones import NivelAlerta


def test_los_ids_son_unicos_y_slug_validos() -> None:
    ids = [e.id for e in CATALOGO]
    assert len(ids) == len(set(ids)), "Hay ids de escenario repetidos."
    for id_ in ids:
        assert PATRON_ID.fullmatch(id_), (
            f"El id '{id_}' no es un slug válido (nombra ficheros y URLs)."
        )


def test_las_cuatro_categorias_estan_pobladas() -> None:
    grupos = por_categoria()
    assert set(grupos) == set(CategoriaEscenario)
    for categoria, escenarios in grupos.items():
        assert escenarios, f"La categoría {categoria} no tiene escenarios."


def test_cada_categoria_tiene_nombre_legible() -> None:
    assert set(NOMBRE_CATEGORIA) == set(CategoriaEscenario)


def test_los_escenarios_de_bandera_roja_esperan_alerta() -> None:
    bandera = escenario_por_id("bandera-roja")
    assert bandera is not None
    assert bandera.espera_alerta is NivelAlerta.ROJO


def test_escenario_por_id_devuelve_none_si_no_existe() -> None:
    assert escenario_por_id("no-existe") is None
    assert escenario_por_id(CATALOGO[0].id) is CATALOGO[0]


def test_por_categoria_respeta_el_orden_de_declaracion() -> None:
    grupos = por_categoria()
    seguridad = grupos[CategoriaEscenario.SEGURIDAD]
    esperados = [e.id for e in CATALOGO if e.categoria is CategoriaEscenario.SEGURIDAD]
    assert [e.id for e in seguridad] == esperados


def test_id_invalido_no_valida() -> None:
    with pytest.raises(ValueError):
        Escenario(
            id="Con Mayúsculas Y Espacios",
            categoria=CategoriaEscenario.ROBUSTEZ,
            nombre="x",
            descripcion="x",
            persona="x",
            criterios="x",
        )


def test_round_trip_json_del_resultado() -> None:
    resultado = ResultadoEscenario(
        id_ejecucion="calidad-bandera-roja-20260811-120000",
        escenario_id="bandera-roja",
        categoria="riesgo_clinico",
        momento="2026-08-11T12:00:00",
        turnos=[
            Turno(rol="clara", texto="Hola, ¿cómo sigue?"),
            Turno(rol="paciente", texto="Mal, con fiebre alta."),
            Turno(rol="herramienta", texto="registrar_alerta", detalle={"nivel": "rojo"}),
        ],
        veredicto=VeredictoJuez(aprobado=True, razonamiento="Escaló bien.", determinista=True),
        documentos_consultados=["apendicitis/postop.pdf"],
        uso=UsoLLM(llamadas=3, tokens_entrada=100, tokens_salida=50, duracion_s=4.2),
    )
    copia = ResultadoEscenario.model_validate_json(resultado.model_dump_json())
    assert copia == resultado


def test_round_trip_json_de_la_solicitud_y_el_lote() -> None:
    solicitud = SolicitudCalidad(
        id_lote="lote-1",
        momento="2026-08-11T12:00:00",
        escenarios=["bandera-roja"],
        autor="operador",
    )
    assert SolicitudCalidad.model_validate_json(solicitud.model_dump_json()) == solicitud

    lote = EstadoLote(id_lote="lote-1", total=3, completados=1, en_curso="bandera-roja")
    assert EstadoLote.model_validate_json(lote.model_dump_json()) == lote
