"""El control de unidades de systemd compartido.

No hay bus de D-Bus en la batería, así que lo que se prueba aquí es la parte que no
lo necesita y que además es la que importa para la seguridad: **la lista blanca se
aplica ANTES de tocar el bus**.

Eso no es un detalle. El socket de D-Bus del gestor de usuario da poder sobre
*cualquier* servicio del usuario, y en el panel el nombre de la unidad llega por
una petición HTTP. Si la comprobación se hiciera después de abrir la conexión, un
error de programación en una vista se convertiría en un agujero.
"""

from __future__ import annotations

from typing import Any

import pytest

from voice_agent_core.systemd import ControlSystemd, ErrorDeControl, EstadoUnidad

PERMITIDAS = frozenset({"voice-agent.service", "voice-agent-telefonia.service"})


class ControlEspia(ControlSystemd):
    """Registra las llamadas al bus en lugar de hacerlas."""

    def __init__(self, permitidas: frozenset[str] = PERMITIDAS) -> None:
        super().__init__(permitidas)
        self.llamadas: list[tuple[str, tuple[Any, ...]]] = []

    def _llamar(self, metodo: str, firma: str, argumentos: tuple[Any, ...]) -> Any:
        self.llamadas.append((metodo, argumentos))
        return ("/org/freedesktop/systemd1/job/1",)


# --- La lista blanca ----------------------------------------------------------


@pytest.mark.parametrize("accion", ["arrancar", "parar", "reiniciar"])
def test_una_unidad_fuera_de_la_lista_se_rechaza(accion: str) -> None:
    control = ControlEspia()
    with pytest.raises(ErrorDeControl, match="no está permitida"):
        getattr(control, accion)("cualquier-cosa.service")


@pytest.mark.parametrize("accion", ["arrancar", "parar", "reiniciar"])
def test_el_rechazo_ocurre_antes_de_tocar_el_bus(accion: str) -> None:
    """Si se comprobara después de abrir la conexión, sería un agujero."""
    control = ControlEspia()
    with pytest.raises(ErrorDeControl):
        getattr(control, accion)("systemd-tmpfiles-clean.service")
    assert control.llamadas == []


def test_consultar_el_estado_tambien_valida() -> None:
    control = ControlEspia()
    with pytest.raises(ErrorDeControl, match="no está permitida"):
        control.estado("otra.service")
    assert control.llamadas == []


def test_la_lista_blanca_es_del_objeto_y_no_global() -> None:
    """Cada consumidor trae su propia política: el panel cuatro unidades, los botones dos."""
    uno = ControlEspia(frozenset({"a.service"}))
    otro = ControlEspia(frozenset({"b.service"}))
    assert uno.permitidas != otro.permitidas
    uno.arrancar("a.service")
    with pytest.raises(ErrorDeControl):
        uno.arrancar("b.service")
    otro.arrancar("b.service")


# --- Las llamadas al bus ------------------------------------------------------


@pytest.mark.parametrize(
    ("accion", "metodo"),
    [("arrancar", "StartUnit"), ("parar", "StopUnit"), ("reiniciar", "RestartUnit")],
)
def test_cada_accion_llama_al_metodo_de_systemd_que_toca(accion: str, metodo: str) -> None:
    control = ControlEspia()
    getattr(control, accion)("voice-agent.service")
    assert control.llamadas == [(metodo, ("voice-agent.service", "replace"))]


def test_devuelve_la_ruta_del_trabajo_y_no_el_resultado() -> None:
    """`StartUnit` vuelve en cuanto systemd encola: lo que da es el trabajo.

    Quien necesite saber si la unidad llegó de verdad a `active` tiene que sondear
    `estado()`. El agente tarda unos doce segundos en estar arriba.
    """
    assert (
        ControlEspia().arrancar("voice-agent.service").startswith("/org/freedesktop/systemd1/job")
    )


# --- EstadoUnidad -------------------------------------------------------------


@pytest.mark.parametrize(
    ("active_state", "activo", "arrancando", "fallido", "legible"),
    [
        ("active", True, False, False, "en marcha"),
        ("activating", False, True, False, "arrancando"),
        ("failed", False, False, True, "fallido (exit-code)"),
        ("inactive", False, False, False, "parado"),
        ("deactivating", False, False, False, "parado"),
    ],
)
def test_el_estado_se_traduce_a_castellano(
    active_state: str, activo: bool, arrancando: bool, fallido: bool, legible: str
) -> None:
    estado = EstadoUnidad(
        unidad="voice-agent.service",
        active_state=active_state,
        sub_state="",
        resultado="exit-code",
    )
    assert estado.activo is activo
    assert estado.arrancando is arrancando
    assert estado.fallido is fallido
    assert estado.legible == legible


@pytest.mark.parametrize(
    ("active_state", "parado"),
    [("inactive", True), ("deactivating", False), ("active", False), ("failed", False)],
)
def test_parado_no_es_lo_contrario_de_activo(active_state: str, parado: bool) -> None:
    """En medio hay un `deactivating` que no es ninguna de las dos cosas.

    Confundirlos está medido: dar la parada por hecha en `deactivating` anunciaba
    el final cuarenta milisegundos después de pedirlo, con el contenedor todavía
    muriéndose. La propiedad `legible` sí los junta, y ahí es correcto, porque para
    la portada del panel «parando» y «parado» se cuentan igual.
    """
    estado = EstadoUnidad(
        unidad="voice-agent.service", active_state=active_state, sub_state="", resultado=""
    )
    assert estado.parado is parado


def test_el_estado_es_inmutable() -> None:
    # Es una foto, no un objeto vivo: mutarla haría creer que se refresca sola.
    estado = EstadoUnidad(unidad="x.service", active_state="active", sub_state="", resultado="")
    with pytest.raises(AttributeError):
        estado.active_state = "failed"  # type: ignore[misc]


# --- Errores ------------------------------------------------------------------


def test_un_bus_que_no_esta_se_traduce_a_error_de_control() -> None:
    """El demonio y el panel esperan `ErrorDeControl`, no lo que lance jeepney."""

    class ControlRoto(ControlSystemd):
        def _llamar(self, metodo: str, firma: str, argumentos: tuple[Any, ...]) -> Any:
            raise ErrorDeControl("No se pudo hablar con systemd: no hay bus")

    with pytest.raises(ErrorDeControl, match="no hay bus"):
        ControlRoto(PERMITIDAS).arrancar("voice-agent.service")
