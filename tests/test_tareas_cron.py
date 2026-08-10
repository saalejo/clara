"""El parser cron de core.

Es la pieza que comparte el panel (validar al guardar) con el agente (calcular
el próximo disparo): si acepta algo que luego no sabe planificar, la tarea
muere en silencio en otro contenedor. De ahí que la batería sea de tabla y
cubra las esquinas de vixie, no solo el camino feliz.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from voice_agent_core.cron import ErrorDeCron, ExpresionCron

# Miércoles 5 de agosto de 2026, 10:30. Referencia de casi todos los casos.
AHORA = datetime(2026, 8, 5, 10, 30)


@pytest.mark.parametrize(
    ("expresion", "esperado"),
    [
        # Cada minuto: el siguiente es el minuto que viene.
        ("* * * * *", datetime(2026, 8, 5, 10, 31)),
        # Hora y minuto fijos, todavía hoy.
        ("0 22 * * *", datetime(2026, 8, 5, 22, 0)),
        # Hora ya pasada hoy: cae mañana.
        ("0 8 * * *", datetime(2026, 8, 6, 8, 0)),
        # Laborables a las 8: el 5 es miércoles y las 8 ya pasaron -> jueves.
        ("0 8 * * 1-5", datetime(2026, 8, 6, 8, 0)),
        # Sábado (6): el próximo es el 8 de agosto.
        ("30 9 * * 6", datetime(2026, 8, 8, 9, 30)),
        # Domingo como 0 y como 7 son el mismo día (el 9 de agosto).
        ("0 12 * * 0", datetime(2026, 8, 9, 12, 0)),
        ("0 12 * * 7", datetime(2026, 8, 9, 12, 0)),
        # Pasos: cada cuarto de hora.
        ("*/15 * * * *", datetime(2026, 8, 5, 10, 45)),
        # Paso sobre rango.
        ("30 8-20/6 * * *", datetime(2026, 8, 5, 14, 30)),
        # Lista.
        ("0 8,22 * * *", datetime(2026, 8, 5, 22, 0)),
        # Día del mes ya pasado: al mes que viene.
        ("0 9 1 * *", datetime(2026, 9, 1, 9, 0)),
        # Fin de mes: agosto tiene 31, el siguiente 31 es este mes.
        ("0 9 31 * *", datetime(2026, 8, 31, 9, 0)),
        # Mes concreto: diciembre.
        ("0 0 25 12 *", datetime(2026, 12, 25, 0, 0)),
        # Regla OR de vixie: día 1 O lunes; el lunes 10 llega antes que el 1/9.
        ("0 8 1 * 1", datetime(2026, 8, 10, 8, 0)),
    ],
)
def test_siguiente(expresion: str, esperado: datetime) -> None:
    assert ExpresionCron.parse(expresion).siguiente(AHORA) == esperado


def test_el_31_se_salta_los_meses_cortos() -> None:
    # Tras el 31 de agosto, el siguiente 31 es en octubre: septiembre no tiene.
    assert ExpresionCron.parse("0 9 31 * *").proximas(AHORA, 2) == [
        datetime(2026, 8, 31, 9, 0),
        datetime(2026, 10, 31, 9, 0),
    ]


def test_siguiente_es_estrictamente_futuro() -> None:
    # Clavados en un disparo, el siguiente es el de después: así el
    # planificador encadena siguiente(ahora) sin dispararse dos veces.
    exacta = datetime(2026, 8, 5, 8, 0)
    assert ExpresionCron.parse("0 8 * * *").siguiente(exacta) == datetime(2026, 8, 6, 8, 0)


def test_los_segundos_no_adelantan_el_disparo() -> None:
    # A las 07:59:59 el disparo de las 8 sigue siendo hoy.
    casi = datetime(2026, 8, 5, 7, 59, 59)
    assert ExpresionCron.parse("0 8 * * *").siguiente(casi) == datetime(2026, 8, 5, 8, 0)


def test_proximas_devuelve_n_en_orden() -> None:
    proximas = ExpresionCron.parse("0 8 * * 1-5").proximas(AHORA, 3)
    assert proximas == [
        datetime(2026, 8, 6, 8, 0),
        datetime(2026, 8, 7, 8, 0),
        datetime(2026, 8, 10, 8, 0),  # el fin de semana se salta
    ]


def test_expresion_imposible_no_cuelga() -> None:
    # El 31 de febrero no existe: tiene que fallar, no buscar para siempre.
    with pytest.raises(ErrorDeCron, match="no se cumple nunca"):
        ExpresionCron.parse("0 0 31 2 *").siguiente(AHORA)


@pytest.mark.parametrize(
    "expresion",
    [
        "",  # vacía
        "0 8 * *",  # cuatro campos
        "0 8 * * * *",  # seis campos
        "60 * * * *",  # minuto fuera de rango
        "* 24 * * *",  # hora fuera de rango
        "* * 0 * *",  # día 0
        "* * 32 * *",  # día 32
        "* * * 13 *",  # mes 13
        "* * * * 8",  # día de semana 8
        "*/0 * * * *",  # paso cero
        "5-1 * * * *",  # rango al revés
        "a b c d e",  # basura
        "1;2 * * * *",  # separador inválido
    ],
)
def test_sintaxis_invalida(expresion: str) -> None:
    with pytest.raises(ErrorDeCron):
        ExpresionCron.parse(expresion)


def test_el_mensaje_de_error_es_en_espanol() -> None:
    # El texto llega tal cual al formulario del panel: tiene que ser legible.
    with pytest.raises(ErrorDeCron, match="5 campos"):
        ExpresionCron.parse("0 8")
