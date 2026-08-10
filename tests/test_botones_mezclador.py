"""El control del mezclador de ALSA.

Las salidas de `amixer` que se usan aquí están **capturadas de la placa**, no
inventadas: es la misma técnica que los tests del puente con sus payloads de
oFono, y es lo que hace que este fichero valga para algo.

Lo que más importa fijar son las dos mitades de la trampa del parseo, porque
ambas son fallos mudos:

1. `Limits: Playback 0 - 151` casa con el patrón obvio antes que el valor real.
   Al descubrirlo, un script de medición leyó volumen 0, «restauró» a 0 y dejó la
   tarjeta muda.
2. El micrófono trae reproducción y captura en la MISMA línea, con dos
   interruptores distintos: `[off]` para la una y `[on]` para la otra.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from voice_agent_botones.config import Ajustes, ModoMicrofono
from voice_agent_botones.mezclador import Mezclador

# --- Salidas reales de la placa -----------------------------------------------

# `amixer -c Device sget Mic`. Fíjate en la línea `Mono:`: la reproducción está en
# [off] y la captura en [on], las dos en la misma línea.
MIC_ABIERTO = """Simple mixer control 'Mic',0
  Capabilities: pvolume pvolume-joined cvolume cvolume-joined pswitch pswitch-joined cswitch cswitch-joined
  Playback channels: Mono
  Capture channels: Mono
  Limits: Playback 0 - 127 Capture 0 - 16
  Mono: Playback 14 [11%] [2.62dB] [off] Capture 14 [88%] [20.83dB] [on]
"""

MIC_SILENCIADO = MIC_ABIERTO.replace("[20.83dB] [on]", "[20.83dB] [off]")

MIC_SIN_GANANCIA = MIC_ABIERTO.replace("Capture 14 [88%]", "Capture 0 [0%]")

# `amixer -M -c Device sget Speaker`. La línea `Limits:` es la trampa, y `Mono:`
# viene vacía porque la salida es estéreo.
ALTAVOZ_AL_64 = """Simple mixer control 'Speaker',0
  Capabilities: pvolume pswitch pswitch-joined
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 151
  Mono:
  Front Left: Playback 113 [64%] [-7.19dB] [on]
  Front Right: Playback 113 [64%] [-7.19dB] [on]
"""

ALTAVOZ_AL_70 = ALTAVOZ_AL_64.replace("[64%]", "[70%]")
ALTAVOZ_AL_100 = ALTAVOZ_AL_64.replace("[64%]", "[100%]")
ALTAVOZ_APAGADO = ALTAVOZ_AL_64.replace("[-7.19dB] [on]", "[-7.19dB] [off]")


class EjecutorFalso:
    """Responde a `amixer` con salidas grabadas y registra lo que se le pidió."""

    def __init__(self, respuestas: list[str] | None = None, *, codigo: int = 0) -> None:
        self.ordenes: list[list[str]] = []
        self.respuestas = respuestas or []
        self.codigo = codigo

    async def __call__(self, orden: Sequence[str]) -> tuple[int, str]:
        self.ordenes.append(list(orden))
        salida = self.respuestas.pop(0) if self.respuestas else ""
        return self.codigo, salida


def _ajustes(**kwargs: Any) -> Ajustes:
    return Ajustes(_env_file=None, **kwargs)  # type: ignore[call-arg]


def _mezclador(
    respuestas: list[str] | None = None, **kwargs: Any
) -> tuple[Mezclador, EjecutorFalso]:
    ejecutor = EjecutorFalso(respuestas)
    return Mezclador(_ajustes(**kwargs), ejecutor=ejecutor), ejecutor


# --- La trampa del parseo -----------------------------------------------------


async def test_no_confunde_el_volumen_con_la_linea_de_limites() -> None:
    """`Limits: Playback 0 - 151` casa con el patrón obvio antes que el valor real.

    Este es el test que impide repetir el fallo que dejó la tarjeta muda: leer 0,
    creer que era el volumen y «restaurarlo» a 0.
    """
    mezclador, _ = _mezclador([ALTAVOZ_AL_64])
    estado = await mezclador._leer_salida()
    assert estado is not None
    assert estado[0] == 64


async def test_lee_el_interruptor_de_captura_y_no_el_de_reproduccion() -> None:
    """En el micrófono los dos vienen en la misma línea, y no coinciden.

    `Mono: Playback 14 [11%] [2.62dB] [off] Capture 14 [88%] [20.83dB] [on]`
    """
    mezclador, _ = _mezclador([MIC_ABIERTO])
    assert await mezclador.micro_abierto() is True


async def test_detecta_el_microfono_silenciado() -> None:
    mezclador, _ = _mezclador([MIC_SILENCIADO])
    assert await mezclador.micro_abierto() is False


async def test_en_modo_ganancia_abierto_es_tener_ganancia() -> None:
    mezclador, _ = _mezclador([MIC_SIN_GANANCIA], modo_micro=ModoMicrofono.GANANCIA)
    assert await mezclador.micro_abierto() is False

    mezclador, _ = _mezclador([MIC_ABIERTO], modo_micro=ModoMicrofono.GANANCIA)
    assert await mezclador.micro_abierto() is True


async def test_lee_el_interruptor_de_salida() -> None:
    mezclador, _ = _mezclador([ALTAVOZ_APAGADO])
    estado = await mezclador._leer_salida()
    assert estado == (64, False)


# --- Las órdenes exactas ------------------------------------------------------


async def test_la_orden_de_silenciar_es_la_esperada() -> None:
    mezclador, ejecutor = _mezclador([MIC_ABIERTO, MIC_SILENCIADO])
    await mezclador.alternar_micro()

    assert ejecutor.ordenes == [
        ["amixer", "-c", "Device", "sget", "Mic"],
        ["amixer", "-c", "Device", "sset", "Mic", "nocap"],
    ]


async def test_la_orden_de_reabrir_es_la_esperada() -> None:
    mezclador, ejecutor = _mezclador([MIC_SILENCIADO, MIC_ABIERTO])
    await mezclador.alternar_micro()
    assert ejecutor.ordenes[-1] == ["amixer", "-c", "Device", "sset", "Mic", "cap"]


async def test_en_modo_ganancia_las_ordenes_son_otras() -> None:
    mezclador, ejecutor = _mezclador(
        [MIC_ABIERTO, MIC_SIN_GANANCIA], modo_micro=ModoMicrofono.GANANCIA
    )
    await mezclador.alternar_micro()
    assert ejecutor.ordenes[-1] == ["amixer", "-c", "Device", "sset", "Mic", "0"]

    mezclador, ejecutor = _mezclador(
        [MIC_ABIERTO], modo_micro=ModoMicrofono.GANANCIA, ganancia_micro_abierto=12
    )
    await mezclador.fijar_micro(True)
    assert ejecutor.ordenes[-1] == ["amixer", "-c", "Device", "sset", "Mic", "12", "cap"]


async def test_la_orden_de_volumen_lleva_mapeado_y_unmute() -> None:
    """`-M` porque la escala cruda concentra el recorrido útil en un extremo.

    Y `unmute` para evitar el clásico «subo el volumen y no pasa nada» cuando el
    interruptor de salida estaba apagado.
    """
    mezclador, ejecutor = _mezclador([ALTAVOZ_AL_64, ALTAVOZ_AL_70])
    await mezclador.subir()

    assert ejecutor.ordenes == [
        ["amixer", "-M", "-c", "Device", "sget", "Speaker"],
        ["amixer", "-M", "-c", "Device", "sset", "Speaker", "6%+", "unmute"],
    ]


async def test_bajar_usa_el_signo_contrario() -> None:
    mezclador, ejecutor = _mezclador([ALTAVOZ_AL_70, ALTAVOZ_AL_64])
    await mezclador.bajar()
    assert ejecutor.ordenes[-1][-2] == "6%-"


async def test_el_paso_de_volumen_es_configurable() -> None:
    mezclador, ejecutor = _mezclador([ALTAVOZ_AL_64, ALTAVOZ_AL_70], paso_volumen=15)
    await mezclador.subir()
    assert ejecutor.ordenes[-1][-2] == "15%+"


async def test_la_tarjeta_es_configurable() -> None:
    mezclador, ejecutor = _mezclador([MIC_ABIERTO], tarjeta_alsa="OtraTarjeta")
    await mezclador.micro_abierto()
    assert ejecutor.ordenes[0][:4] == ["amixer", "-c", "OtraTarjeta", "sget"]


# --- El límite del volumen ----------------------------------------------------


async def test_detecta_que_el_volumen_ya_estaba_al_tope() -> None:
    """Sin esto, una pulsación que no cambia nada parece un botón roto."""
    mezclador, _ = _mezclador([ALTAVOZ_AL_100, ALTAVOZ_AL_100])
    resultado = await mezclador.subir()
    assert resultado == (100, True)


async def test_un_cambio_normal_no_es_tope() -> None:
    mezclador, _ = _mezclador([ALTAVOZ_AL_64, ALTAVOZ_AL_70])
    resultado = await mezclador.subir()
    assert resultado == (70, False)


async def test_reactivar_la_salida_no_cuenta_como_tope() -> None:
    """Si el interruptor estaba apagado, el `unmute` ya es un cambio audible."""
    mezclador, _ = _mezclador([ALTAVOZ_APAGADO, ALTAVOZ_AL_64])
    resultado = await mezclador.subir()
    assert resultado == (64, False)


# --- Nunca lanza --------------------------------------------------------------


async def test_un_amixer_que_falla_devuelve_none() -> None:
    mezclador, _ = _mezclador([""], codigo=1)
    assert await mezclador.micro_abierto() is None


async def test_una_salida_inesperada_devuelve_none() -> None:
    mezclador, _ = _mezclador(["esto no se parece a amixer"])
    assert await mezclador.micro_abierto() is None


async def test_un_amixer_que_no_esta_devuelve_none() -> None:
    """El demonio no puede morir porque falte una herramienta del sistema."""

    async def revienta(_orden: Sequence[str]) -> tuple[int, str]:
        raise FileNotFoundError(2, "No such file or directory", "amixer")

    mezclador = Mezclador(_ajustes(), ejecutor=revienta)
    assert await mezclador.micro_abierto() is None
    assert await mezclador.subir() is None


async def test_un_amixer_colgado_no_bloquea_para_siempre() -> None:
    import asyncio

    async def se_cuelga(_orden: Sequence[str]) -> tuple[int, str]:
        await asyncio.sleep(60)
        return 0, ""

    mezclador = Mezclador(_ajustes(timeout_amixer_segundos=0.01), ejecutor=se_cuelga)
    assert await mezclador.micro_abierto() is None


async def test_leer_el_estado_completo() -> None:
    mezclador, _ = _mezclador([MIC_ABIERTO, ALTAVOZ_AL_64])
    estado = await mezclador.leer()
    assert estado is not None
    assert estado.micro_abierto is True
    assert estado.volumen_pct == 64
    assert estado.salida_activa is True


@pytest.mark.parametrize("salida", [MIC_ABIERTO, MIC_SILENCIADO, ALTAVOZ_AL_64, ALTAVOZ_APAGADO])
async def test_ninguna_salida_real_hace_saltar_una_excepcion(salida: str) -> None:
    mezclador, _ = _mezclador([salida, salida])
    await mezclador.leer()
