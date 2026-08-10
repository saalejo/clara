"""Análisis de los mensajes de oFono, con payloads grabados de la placa.

Estos diccionarios son los que devuelve `org.ofono.VoiceCallManager.GetCalls`
de verdad, ya desenvueltos de sus `Variant`. Probarlos aquí es lo que justifica
haber separado el análisis del I/O: `llamada_desde_propiedades` es una función
pura, así que toda la máquina de estados se puede verificar sin móvil, sin
Bluetooth y sin bus.
"""

from __future__ import annotations

import pytest

from voice_agent_core.telefonia import LLAMADA_DE_APP, NUMERO_OCULTO, EstadoLlamada
from voice_agent_telefonia.llamadas import (
    estado_legible,
    id_de_ruta,
    llamada_desde_propiedades,
)

RUTA = "/hfp/org/bluez/hci0/dev_04_01_BB_5D_68_12/voicecall01"


def test_id_de_ruta_no_expone_la_mac() -> None:
    """La ruta completa lleva dentro la MAC del móvil; el id que sale del
    puente, no."""
    assert id_de_ruta(RUTA) == "voicecall01"


@pytest.mark.parametrize(
    ("de_ofono", "esperado"),
    [
        ("incoming", EstadoLlamada.ENTRANTE),
        ("waiting", EstadoLlamada.EN_ESPERA),
        ("dialing", EstadoLlamada.MARCANDO),
        ("alerting", EstadoLlamada.SONANDO),
        ("active", EstadoLlamada.EN_CURSO),
        ("held", EstadoLlamada.RETENIDA),
        ("disconnected", EstadoLlamada.TERMINADA),
    ],
)
def test_los_siete_estados_de_ofono(de_ofono: str, esperado: EstadoLlamada) -> None:
    assert estado_legible(de_ofono) == esperado


def test_un_estado_desconocido_se_da_por_terminada() -> None:
    """Mejor dar una llamada por acabada que quedarse con una fantasma."""
    assert estado_legible("teletransportando") is EstadoLlamada.TERMINADA


#: Una llamada entrante tal y como la entrega oFono.
ENTRANTE = {
    "State": "incoming",
    "LineIdentification": "+573001234567",
    "Name": "",
    "Multiparty": False,
    "Emergency": False,
}


class TestLlamadaEntrante:
    def test_se_marca_como_entrante(self) -> None:
        llamada = llamada_desde_propiedades(RUTA, ENTRANTE)
        assert llamada.entrante
        assert llamada.estado is EstadoLlamada.ENTRANTE
        assert llamada.viva

    def test_sin_nombre_de_red_usa_el_numero(self) -> None:
        assert llamada_desde_propiedades(RUTA, ENTRANTE).quien == "+573001234567"

    def test_el_nombre_de_agenda_gana(self) -> None:
        llamada = llamada_desde_propiedades(RUTA, ENTRANTE)
        llamada.nombre_agenda = "Mamá"
        assert llamada.quien == "Mamá"


def test_numero_oculto() -> None:
    """Sin traducir 'withheld', el agente diría literalmente 'te llama
    withheld'."""
    llamada = llamada_desde_propiedades(
        RUTA, {"State": "incoming", "LineIdentification": "withheld"}
    )
    assert llamada.numero == NUMERO_OCULTO
    assert llamada.quien == NUMERO_OCULTO


def test_llamada_saliente() -> None:
    """`dialing` es el estado inicial de una saliente de verdad.

    Comprobado marcando desde el puente a un número real: la llamada aparece
    en `dialing` y con el número correcto.
    """
    llamada = llamada_desde_propiedades(RUTA, {"State": "dialing", "LineIdentification": "+571"})
    assert not llamada.entrante


def test_llamada_en_curso_con_hora_de_inicio() -> None:
    llamada = llamada_desde_propiedades(
        RUTA,
        {
            "State": "active",
            "LineIdentification": "+573001234567",
            "StartTime": "2026-07-28T22:35:12Z",
        },
    )
    assert llamada.estado is EstadoLlamada.EN_CURSO
    assert llamada.inicio is not None
    assert llamada.inicio.year == 2026


def test_hora_de_inicio_ilegible_no_revienta() -> None:
    llamada = llamada_desde_propiedades(RUTA, {"State": "active", "StartTime": "ayer"})
    assert llamada.inicio is None


def test_llamada_terminada_no_esta_viva() -> None:
    assert not llamada_desde_propiedades(RUTA, {"State": "disconnected"}).viva


def test_propiedades_vacias_no_revientan() -> None:
    """oFono manda a veces objetos a medio poblar mientras se establece el
    enlace."""
    llamada = llamada_desde_propiedades(RUTA, {})
    assert llamada.estado is EstadoLlamada.TERMINADA
    assert llamada.quien == NUMERO_OCULTO


class TestDireccionDeLaLlamada:
    """La dirección solo se puede deducir del estado INICIAL.

    Lo enseñó una llamada de verdad: el puente anunció como *saliente* una
    llamada que estaba entrando. `CallAdded` llega con `State: "incoming"`,
    pero cuando el puente reaccionaba y releía `GetCalls`, esa llamada ya
    estaba en `active` — y `active` no dice nada de la dirección.

    Por eso `Servicio` se queda con la dirección de la señal (`_direcciones`) y
    no la vuelve a deducir. Estos tests fijan el porqué.
    """

    def test_incoming_es_entrante(self) -> None:
        assert llamada_desde_propiedades(RUTA, {"State": "incoming"}).entrante

    def test_solo_dialing_cuenta_como_saliente(self) -> None:
        """Y ni siquiera `dialing` basta solo: ver `TestLlamadaDeAplicacion`."""
        assert not llamada_desde_propiedades(RUTA, {"State": "dialing"}).entrante

    def test_alerting_se_trata_como_entrante(self) -> None:
        """Contra el estándar de HFP, y a propósito.

        Medido con un TECNO POVA 5 Pro: entrega las llamadas ENTRANTES como
        `alerting` con un identificador de relleno (`10000000`). Tratarlo como
        saliente —que es lo que dice el estándar— hacía que el agente no
        avisara nunca de una llamada entrante. Ver `_ESTADOS_SALIENTES`.
        """
        assert llamada_desde_propiedades(RUTA, {"State": "alerting"}).entrante

    def test_active_no_permite_deducir_la_direccion(self) -> None:
        """Este es el caso que provocó el fallo: una llamada ya contestada
        parece entrante mire quien la mire, así que releer el estado no vale
        para decidir la dirección de una llamada que ya avanzó."""
        assert llamada_desde_propiedades(RUTA, {"State": "active"}).entrante


class TestLlamadaDeAplicacion:
    """WhatsApp y demás: llegan al manos libres, pero sin número.

    Las apps de VoIP se integran en Android como `ConnectionService`
    autogestionado, así que sus llamadas SÍ se pueden contestar y colgar desde
    el manos libres. Lo que no viaja es el número: el identificador de llamada
    de HFP solo entiende de números de teléfono y Android manda un relleno.

    Medido con una llamada de WhatsApp real contra este montaje: llegó
    `LineIdentification: "10000000"`, y el agente anunció «te llama 10000000».
    """

    def test_el_relleno_no_se_dice_en_voz_alta(self) -> None:
        llamada = llamada_desde_propiedades(
            RUTA, {"State": "alerting", "LineIdentification": "10000000"}
        )
        assert llamada.quien == LLAMADA_DE_APP
        assert "10000000" not in llamada.quien

    def test_una_llamada_de_app_es_entrante(self) -> None:
        """Llegan como `alerting`, que el estándar reserva para las salientes."""
        llamada = llamada_desde_propiedades(
            RUTA, {"State": "alerting", "LineIdentification": "10000000"}
        )
        assert llamada.entrante

    def test_dialing_con_relleno_es_una_app_que_entra(self) -> None:
        """El segundo estado con el que llegan, medido en una llamada real.

        El mismo móvil entregó una llamada de WhatsApp ENTRANTE en `dialing`, y
        no en `alerting`. El puente la dio por saliente y no la anunció: en la
        habitación nadie se enteró de que estaban llamando. Lo que la delata es
        el relleno, porque una saliente de verdad lleva el número marcado.
        """
        llamada = llamada_desde_propiedades(
            RUTA, {"State": "dialing", "LineIdentification": "10000000"}
        )
        assert llamada.entrante

    def test_dialing_con_un_numero_de_verdad_sigue_siendo_saliente(self) -> None:
        """El contrapeso: marcar desde el móvil no puede anunciarse como entrante."""
        llamada = llamada_desde_propiedades(
            RUTA, {"State": "dialing", "LineIdentification": "+573001234567"}
        )
        assert not llamada.entrante

    def test_se_reconoce_como_llamada_de_app(self) -> None:
        """`es_de_app` es lo que avisa de que no hay que fiarse de su estado."""
        llamada = llamada_desde_propiedades(
            RUTA, {"State": "active", "LineIdentification": "10000000"}
        )
        assert llamada.es_de_app

    def test_una_del_operador_no_lo_es(self) -> None:
        llamada = llamada_desde_propiedades(
            RUTA, {"State": "active", "LineIdentification": "+573001234567"}
        )
        assert not llamada.es_de_app

    def test_un_numero_normal_sigue_diciendose(self) -> None:
        llamada = llamada_desde_propiedades(
            RUTA, {"State": "incoming", "LineIdentification": "+573001234567"}
        )
        assert llamada.quien == "+573001234567"

    def test_el_nombre_de_la_agenda_gana_al_relleno(self) -> None:
        llamada = llamada_desde_propiedades(
            RUTA, {"State": "alerting", "LineIdentification": "10000000"}
        )
        llamada.nombre_agenda = "Mamá"
        assert llamada.quien == "Mamá"
