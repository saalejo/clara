"""La lectura del device HID.

No hace falta hardware: un `/dev/input/eventN` es una secuencia de estructuras de
tamaño fijo, así que se le pasa al lector el extremo de lectura de un `os.pipe()`
y se escriben eventos empaquetados con `struct`. Es la misma técnica que usan los
tests del puente de telefonía con sus payloads grabados de oFono.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import Any

import pytest

from voice_agent_botones.entrada import EV_KEY, FORMATO, TAMANO_EVENTO, LectorDeBotones
from voice_agent_botones.gestos import KEY_MUTE, KEY_VOLUMEUP

EV_SYN = 0x00
EV_MSC = 0x04
MSC_SCAN = 0x04


def _evento(tipo: int, codigo: int, valor: int) -> bytes:
    return struct.pack(FORMATO, 0, 0, tipo, codigo, valor)


class Tuberia:
    """Un `os.pipe()` que hace de device, con su extremo de escritura a mano."""

    def __init__(self) -> None:
        self.lectura, self.escritura = os.pipe()
        os.set_blocking(self.lectura, False)
        self.aperturas = 0

    def abrir(self, _ruta: Path) -> int:
        self.aperturas += 1
        return self.lectura

    def escribir(self, *eventos: bytes) -> None:
        os.write(self.escritura, b"".join(eventos))

    def cerrar_escritura(self) -> None:
        os.close(self.escritura)


@pytest.fixture
def tuberia() -> Tuberia:
    return Tuberia()


def _lector(tuberia: Tuberia, **kwargs: Any) -> LectorDeBotones:
    opciones = {"acaparar": False, "espera_segundos": 0.01, "abridor": tuberia.abrir}
    opciones.update(kwargs)
    return LectorDeBotones(Path("/dev/input/inventado"), **opciones)  # type: ignore[arg-type]


# --- Parseo -------------------------------------------------------------------


def test_el_tamano_del_evento_es_el_de_un_sistema_de_64_bits() -> None:
    # Si esto cambiara, el troceado del búfer leería basura desplazada.
    assert TAMANO_EVENTO == 24


async def test_lee_una_pulsacion_y_una_soltada(tuberia: Tuberia) -> None:
    lector = _lector(tuberia)
    await lector.abrir()
    tuberia.escribir(
        _evento(EV_KEY, KEY_MUTE, 1),
        _evento(EV_SYN, 0, 0),
        _evento(EV_KEY, KEY_MUTE, 0),
        _evento(EV_SYN, 0, 0),
    )

    primera = await lector.leer(timeout=1.0)
    segunda = await lector.leer(timeout=1.0)
    await lector.cerrar()

    assert primera is not None and primera.pulsada is True
    assert primera.tecla == KEY_MUTE
    assert segunda is not None and segunda.pulsada is False


async def test_varios_eventos_en_una_sola_lectura(tuberia: Tuberia) -> None:
    """Una lectura puede traer varios eventos de golpe y hay que trocearla."""
    lector = _lector(tuberia)
    await lector.abrir()
    tuberia.escribir(
        *[_evento(EV_KEY, KEY_VOLUMEUP, v) for v in (1, 0, 1, 0, 1, 0)],
    )

    leidas = [await lector.leer(timeout=1.0) for _ in range(6)]
    await lector.cerrar()

    assert all(p is not None for p in leidas)
    assert [p.pulsada for p in leidas if p is not None] == [True, False] * 3


async def test_un_evento_partido_se_completa_en_la_siguiente_lectura(
    tuberia: Tuberia,
) -> None:
    """No debería pasar en un device de eventos, pero contemplarlo cuesta una línea."""
    lector = _lector(tuberia)
    await lector.abrir()
    crudo = _evento(EV_KEY, KEY_MUTE, 1)
    tuberia.escribir(crudo[:10])

    assert await lector.leer(timeout=0.05) is None  # todavía no hay evento completo

    tuberia.escribir(crudo[10:])
    pulsacion = await lector.leer(timeout=1.0)
    await lector.cerrar()

    assert pulsacion is not None
    assert pulsacion.tecla == KEY_MUTE


async def test_descarta_lo_que_no_es_una_tecla_de_interes(tuberia: Tuberia) -> None:
    lector = _lector(tuberia)
    await lector.abrir()
    tuberia.escribir(
        _evento(EV_MSC, MSC_SCAN, 0x700E2),  # scancode crudo
        _evento(EV_SYN, 0, 0),
        _evento(EV_KEY, 30, 1),  # KEY_A: el device no la emite, pero por si acaso
        _evento(EV_KEY, KEY_MUTE, 1),  # esta sí
    )

    pulsacion = await lector.leer(timeout=1.0)
    await lector.cerrar()

    assert pulsacion is not None
    assert pulsacion.tecla == KEY_MUTE


async def test_la_autorrepeticion_no_genera_una_pulsacion_nueva(tuberia: Tuberia) -> None:
    """`value == 2` es autorrepetición del kernel: se ignora.

    Este device no la manda —no declara `EV_REP`— pero el código no debe romperse
    si otro la manda.
    """
    lector = _lector(tuberia)
    await lector.abrir()
    tuberia.escribir(
        _evento(EV_KEY, KEY_VOLUMEUP, 1),
        _evento(EV_KEY, KEY_VOLUMEUP, 2),
        _evento(EV_KEY, KEY_VOLUMEUP, 2),
        _evento(EV_KEY, KEY_VOLUMEUP, 0),
    )

    leidas = []
    while (p := await lector.leer(timeout=0.05)) is not None:
        leidas.append(p)
    await lector.cerrar()

    assert [p.pulsada for p in leidas] == [True, False]


# --- Tiempos ------------------------------------------------------------------


async def test_marca_las_pulsaciones_con_el_reloj_inyectado(tuberia: Tuberia) -> None:
    # El reloj se consulta UNA vez por lectura del device, no una por evento: los
    # eventos que llegan en la misma lectura comparten instante, que es lo correcto
    # porque el kernel los entregó juntos.
    instantes = iter([500.0, 900.0])
    lector = _lector(tuberia, reloj=lambda: next(instantes))
    await lector.abrir()

    tuberia.escribir(_evento(EV_KEY, KEY_MUTE, 1))
    primera = await lector.leer(timeout=1.0)
    tuberia.escribir(_evento(EV_KEY, KEY_MUTE, 0))
    segunda = await lector.leer(timeout=1.0)
    await lector.cerrar()

    assert primera is not None and primera.momento == 500.0
    assert segunda is not None and segunda.momento == 900.0


async def test_los_eventos_de_una_misma_lectura_comparten_instante(
    tuberia: Tuberia,
) -> None:
    """El kernel los entregó juntos, así que fecharlos por separado sería inventar.

    Importa porque de esa marca sale la duración del mantenido: si cada evento de
    una misma lectura llevara un instante distinto, un rebote de dos eventos
    pegados podría dar una duración negativa.
    """
    lector = _lector(tuberia, reloj=lambda: 42.0)
    await lector.abrir()
    tuberia.escribir(
        _evento(EV_KEY, KEY_VOLUMEUP, 1),
        _evento(EV_KEY, KEY_VOLUMEUP, 0),
    )

    primera = await lector.leer(timeout=1.0)
    segunda = await lector.leer(timeout=1.0)
    await lector.cerrar()

    assert primera is not None and segunda is not None
    assert primera.momento == segunda.momento == 42.0


async def test_leer_devuelve_none_al_vencer_el_timeout(tuberia: Tuberia) -> None:
    lector = _lector(tuberia)
    await lector.abrir()
    assert await lector.leer(timeout=0.01) is None
    await lector.cerrar()


async def test_un_timeout_no_positivo_no_espera(tuberia: Tuberia) -> None:
    """El bucle de gestos usa timeout cero para atender un vencimiento cumplido."""
    lector = _lector(tuberia)
    await lector.abrir()
    assert await lector.leer(timeout=0.0) is None
    await lector.cerrar()


# --- Apertura y reapertura ----------------------------------------------------


async def test_espera_a_que_el_device_aparezca() -> None:
    """En el arranque de la placa es normal que el USB tarde en enumerar."""
    tuberia = Tuberia()
    intentos = 0

    def abridor(ruta: Path) -> int:
        nonlocal intentos
        intentos += 1
        if intentos < 3:
            raise FileNotFoundError(2, "No such file or directory", str(ruta))
        return tuberia.abrir(ruta)

    lector = _lector(tuberia, abridor=abridor)
    await lector.abrir()
    await lector.cerrar()

    assert intentos == 3


async def test_cerrar_dos_veces_no_falla(tuberia: Tuberia) -> None:
    lector = _lector(tuberia)
    await lector.abrir()
    await lector.cerrar()
    await lector.cerrar()


async def test_el_gestor_de_contexto_abre_y_cierra(tuberia: Tuberia) -> None:
    async with _lector(tuberia) as lector:
        tuberia.escribir(_evento(EV_KEY, KEY_MUTE, 1))
        assert await lector.leer(timeout=1.0) is not None
