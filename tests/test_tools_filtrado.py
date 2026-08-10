"""Filtrado de las herramientas que el panel deja ver al modelo."""

from __future__ import annotations

from voice_agent.tools import (
    HERRAMIENTAS,
    esquema_de,
    herramientas_activas,
    nombre_de,
)


def test_sin_nada_desactivado_estan_todas() -> None:
    assert herramientas_activas(frozenset()) == HERRAMIENTAS


def test_la_lista_devuelta_es_una_copia() -> None:
    # Si fuera la misma lista, filtrar en un sitio mutaría el registro global.
    activas = herramientas_activas(frozenset())
    activas.clear()
    assert len(HERRAMIENTAS) == 4


def test_desactivar_una_la_quita() -> None:
    nombres = {nombre_de(h) for h in herramientas_activas({"estado_del_sistema"})}
    assert nombres == {"buscar_en_documentos", "obtener_fecha_hora", "guardar_respuestas"}


def test_un_nombre_desconocido_no_molesta() -> None:
    # Puede venir de una versión anterior o de un servidor MCP que ya no está.
    assert len(herramientas_activas({"herramienta_fantasma"})) == 4


def test_se_pueden_desactivar_todas() -> None:
    # Raro, pero no es un error: el agente sigue conversando sin herramientas.
    todas = {nombre_de(h) for h in HERRAMIENTAS}
    assert herramientas_activas(todas) == []


def test_el_nombre_coincide_con_el_del_esquema() -> None:
    # `nombre_de` se usa para filtrar; si no coincidiera con lo que ve el modelo,
    # desactivar una herramienta desde el panel no tendría efecto.
    for herramienta in HERRAMIENTAS:
        assert nombre_de(herramienta) == esquema_de(herramienta)["name"]


def test_el_esquema_publicado_lleva_lo_que_el_panel_necesita() -> None:
    esquema = esquema_de(next(h for h in HERRAMIENTAS if nombre_de(h) == "buscar_en_documentos"))
    assert esquema["name"] == "buscar_en_documentos"
    assert str(esquema["description"]).strip()
    assert "consulta" in esquema["parameters"]["properties"]  # type: ignore[index]
