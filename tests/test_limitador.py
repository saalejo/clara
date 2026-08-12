"""El freno de intentos que sostiene la puerta de Clara y el login del panel.

Todo con reloj falso: es lo que permite comprobar un castigo de quince minutos
sin dormir quince minutos, y la razón de que este limitador exista en vez de
usar `django.core.cache`.
"""

from __future__ import annotations

from voice_agent_core.limitador import (
    IP_DESCONOCIDA,
    MAX_CLAVES,
    LimitadorDeIntentos,
    ip_del_cliente,
)


class RelojFalso:
    """Un reloj que solo avanza cuando se lo mandan."""

    def __init__(self) -> None:
        self.ahora = 1_000.0

    def __call__(self) -> float:
        return self.ahora

    def avanzar(self, segundos: float) -> None:
        self.ahora += segundos


def _limitador(reloj: RelojFalso) -> LimitadorDeIntentos:
    return LimitadorDeIntentos(max_intentos=3, ventana_secs=60.0, bloqueo_secs=900.0, reloj=reloj)


def test_tolera_los_primeros_fallos() -> None:
    reloj = RelojFalso()
    limitador = _limitador(reloj)
    for _ in range(2):
        limitador.anotar_fallo("1.1.1.1")
        assert limitador.permitido("1.1.1.1")


def test_el_intento_del_tope_cierra_la_puerta() -> None:
    reloj = RelojFalso()
    limitador = _limitador(reloj)
    for _ in range(3):
        limitador.anotar_fallo("1.1.1.1")
    assert not limitador.permitido("1.1.1.1")


def test_el_castigo_dura_lo_que_dice_y_no_lo_que_dura_la_ventana() -> None:
    # La distinción importa: las marcas caen fuera de la ventana (60 s) mucho
    # antes de que el castigo (900 s) termine. Purgarlas sin mirar el bloqueo
    # convertiría un castigo de quince minutos en uno de uno.
    reloj = RelojFalso()
    limitador = _limitador(reloj)
    for _ in range(3):
        limitador.anotar_fallo("1.1.1.1")
    reloj.avanzar(120.0)
    assert not limitador.permitido("1.1.1.1")
    reloj.avanzar(800.0)
    assert limitador.permitido("1.1.1.1")


def test_los_fallos_viejos_no_cuentan() -> None:
    reloj = RelojFalso()
    limitador = _limitador(reloj)
    limitador.anotar_fallo("1.1.1.1")
    limitador.anotar_fallo("1.1.1.1")
    reloj.avanzar(61.0)
    limitador.anotar_fallo("1.1.1.1")
    assert limitador.permitido("1.1.1.1")


def test_acertar_limpia_el_historial() -> None:
    reloj = RelojFalso()
    limitador = _limitador(reloj)
    limitador.anotar_fallo("1.1.1.1")
    limitador.anotar_fallo("2.2.2.2")
    limitador.olvidar("1.1.1.1")
    assert limitador.segundos_restantes("1.1.1.1") == 0
    limitador.anotar_fallo("2.2.2.2")
    limitador.anotar_fallo("2.2.2.2")
    assert not limitador.permitido("2.2.2.2")


def test_dos_ips_no_comparten_cubo() -> None:
    reloj = RelojFalso()
    limitador = _limitador(reloj)
    for _ in range(3):
        limitador.anotar_fallo("1.1.1.1")
    assert not limitador.permitido("1.1.1.1")
    assert limitador.permitido("2.2.2.2")


def test_segundos_restantes_sirve_para_el_retry_after() -> None:
    reloj = RelojFalso()
    limitador = _limitador(reloj)
    assert limitador.segundos_restantes("1.1.1.1") == 0
    for _ in range(3):
        limitador.anotar_fallo("1.1.1.1")
    assert 890 < limitador.segundos_restantes("1.1.1.1") <= 901


def test_un_limitador_desactivado_no_revienta_con_el_cubo_vacio() -> None:
    # `max_intentos=0` es el valor que apaga la cuota de llamadas. Antes de la
    # guarda de `_vigentes`, esto lanzaba IndexError al mirar `marcas[-1]`.
    limitador = LimitadorDeIntentos(max_intentos=0)
    assert limitador.segundos_restantes("1.1.1.1") == 0


def test_el_diccionario_no_crece_sin_limite() -> None:
    # Sin esta poda, un barrido desde muchas direcciones convertiría la
    # defensa en el ataque.
    reloj = RelojFalso()
    limitador = _limitador(reloj)
    for i in range(MAX_CLAVES + 200):
        limitador.anotar_fallo(f"10.0.{i // 256}.{i % 256}")
        reloj.avanzar(0.001)
    assert len(limitador._fallos) <= MAX_CLAVES


def test_limpiar_lo_vacia() -> None:
    limitador = LimitadorDeIntentos(max_intentos=1)
    limitador.anotar_fallo("1.1.1.1")
    limitador.limpiar()
    assert limitador.permitido("1.1.1.1")


# --- De quién es la petición -------------------------------------------------


def test_manda_la_cabecera_de_cloudflare() -> None:
    cabeceras = {"cf-connecting-ip": "203.0.113.7", "x-forwarded-for": "198.51.100.1"}
    assert ip_del_cliente(cabeceras, "127.0.0.1") == "203.0.113.7"


def test_la_cabecera_se_lee_sin_mirar_mayusculas() -> None:
    assert ip_del_cliente({"CF-Connecting-Ip": "203.0.113.7"}, None) == "203.0.113.7"


def test_del_reenviado_se_coge_el_primero() -> None:
    # El primero es el cliente; los demás son los proxies por los que pasó.
    assert ip_del_cliente({"x-forwarded-for": "203.0.113.7, 10.0.0.1"}, None) == "203.0.113.7"


def test_sin_cabeceras_vale_el_socket() -> None:
    assert ip_del_cliente({}, "192.168.1.40") == "192.168.1.40"


def test_sin_nada_se_dice_que_no_se_sabe() -> None:
    assert ip_del_cliente({}, None) == IP_DESCONOCIDA
    assert ip_del_cliente({"cf-connecting-ip": "  "}, None) == IP_DESCONOCIDA
