"""Los pitidos de feedback.

Se generan en `tmp_path` y se releen con `wave` para comprobar lo que de verdad
importa: que el formato sea el nativo de la tarjeta, que las rampas estén (o se
oye un chasquido), que no haya saturación (o el `dmix` compartido con el agente
distorsiona) y que la cola no bloquee ni crezca.

Nada de esto toca la tarjeta: el reproductor se inyecta.
"""

from __future__ import annotations

import array
import asyncio
import wave
from itertools import pairwise
from pathlib import Path

import pytest

from voice_agent_botones.pitidos import (
    AMPLITUD,
    CANALES,
    CATALOGO,
    FRECUENCIA_MUESTREO,
    TAMANO_COLA,
    Pitidos,
    Tono,
)


class ReproductorFalso:
    """Registra lo que se le pide reproducir, sin tocar la tarjeta."""

    def __init__(self, *, falla: bool = False) -> None:
        self.reproducidos: list[Path] = []
        self.falla = falla

    async def __call__(self, ruta: Path) -> None:
        self.reproducidos.append(ruta)
        if self.falla:
            raise RuntimeError("aplay de mentira ha fallado")


def _muestras(ruta: Path) -> array.array[int]:
    with wave.open(str(ruta)) as w:
        return array.array("h", w.readframes(w.getnframes()))


# --- Formato ------------------------------------------------------------------


def test_el_formato_es_el_nativo_de_la_tarjeta(tmp_path: Path) -> None:
    """48 kHz estéreo 16 bits, para que la capa `plug` no convierta nada."""
    pitidos = Pitidos(tmp_path, reproductor=ReproductorFalso())
    pitidos.preparar()

    with wave.open(str(pitidos.ruta_de("si"))) as w:
        assert w.getframerate() == FRECUENCIA_MUESTREO
        assert w.getnchannels() == CANALES
        assert w.getsampwidth() == 2


def test_la_duracion_cuadra_con_el_catalogo(tmp_path: Path) -> None:
    pitidos = Pitidos(tmp_path, reproductor=ReproductorFalso())
    pitidos.preparar()

    for nombre, tonos in CATALOGO.items():
        esperados_ms = sum(t.ms for t in tonos)
        with wave.open(str(pitidos.ruta_de(nombre))) as w:
            reales_ms = w.getnframes() / w.getframerate() * 1000
        assert abs(reales_ms - esperados_ms) < 2, nombre


def test_se_genera_el_catalogo_entero(tmp_path: Path) -> None:
    pitidos = Pitidos(tmp_path, reproductor=ReproductorFalso())
    pitidos.preparar()
    for nombre in CATALOGO:
        assert pitidos.ruta_de(nombre).exists(), nombre


# --- Calidad de la señal ------------------------------------------------------


def test_las_rampas_evitan_el_chasquido(tmp_path: Path) -> None:
    """Sin rampa, la onda se corta a mitad de ciclo y el clic se oye más que el pitido."""
    pitidos = Pitidos(
        tmp_path,
        reproductor=ReproductorFalso(),
        catalogo={"prueba": (Tono(1000, 1000, 200),)},
    )
    pitidos.preparar()
    muestras = _muestras(pitidos.ruta_de("prueba"))

    assert muestras[0] == 0
    assert muestras[-1] == 0
    # Y el pico está en el centro, no en los extremos.
    centro = abs(muestras[len(muestras) // 2])
    assert centro > abs(muestras[10])


def test_no_hay_saturacion(tmp_path: Path) -> None:
    """La tarjeta se comparte con el agente por `dmix`, que suma los flujos."""
    pitidos = Pitidos(tmp_path, reproductor=ReproductorFalso())
    pitidos.preparar()

    tope = int(AMPLITUD * 32767) + 1
    for nombre in CATALOGO:
        pico = max(abs(m) for m in _muestras(pitidos.ruta_de(nombre)))
        assert pico <= tope, f"{nombre} llega a {pico}, por encima de {tope}"


def test_un_silencio_es_silencio_de_verdad(tmp_path: Path) -> None:
    pitidos = Pitidos(
        tmp_path,
        reproductor=ReproductorFalso(),
        catalogo={"nada": (Tono(0, 0, 50),)},
    )
    pitidos.preparar()
    assert set(_muestras(pitidos.ruta_de("nada"))) == {0}


def test_un_barrido_cambia_de_frecuencia(tmp_path: Path) -> None:
    """Se cuentan cruces por cero: en un barrido ascendente hay más al final."""
    pitidos = Pitidos(
        tmp_path,
        reproductor=ReproductorFalso(),
        catalogo={"sube": (Tono(300, 2000, 400),)},
    )
    pitidos.preparar()
    # Un solo canal, para no contar cada muestra dos veces.
    mono = _muestras(pitidos.ruta_de("sube"))[::CANALES]
    mitad = len(mono) // 2

    def cruces(trozo: array.array[int]) -> int:
        return sum(1 for a, b in pairwise(trozo) if (a < 0) != (b < 0))

    assert cruces(mono[mitad:]) > cruces(mono[:mitad]) * 2


# --- Nombres y regeneración ---------------------------------------------------


def test_el_nombre_lleva_el_resumen_de_los_tonos(tmp_path: Path) -> None:
    """Cambiar una frecuencia tiene que generar un fichero nuevo.

    Si el nombre no dependiera de los parámetros habría que acordarse de borrar a
    mano al afinar un tono, que es justo lo que no se recuerda.
    """
    uno = Pitidos(tmp_path, catalogo={"x": (Tono(1000, 1000, 90),)})
    otro = Pitidos(tmp_path, catalogo={"x": (Tono(1100, 1100, 90),)})
    assert uno.ruta_de("x") != otro.ruta_de("x")
    assert uno.ruta_de("x").name.startswith("x-")


def test_preparar_dos_veces_no_reescribe(tmp_path: Path) -> None:
    pitidos = Pitidos(tmp_path, reproductor=ReproductorFalso())
    pitidos.preparar()
    ruta = pitidos.ruta_de("si")
    antes = ruta.stat().st_mtime_ns

    pitidos.preparar()
    assert ruta.stat().st_mtime_ns == antes


def test_no_queda_ningun_fichero_parcial(tmp_path: Path) -> None:
    """Se escribe en un temporal y se renombra, para que nadie lea un WAV a medias."""
    Pitidos(tmp_path, reproductor=ReproductorFalso()).preparar()
    assert list(tmp_path.glob("*.parcial")) == []


# --- La cola ------------------------------------------------------------------


async def test_sonar_no_espera_a_que_suene(tmp_path: Path) -> None:
    reproductor = ReproductorFalso()
    pitidos = Pitidos(tmp_path, reproductor=reproductor)
    async with pitidos:
        pitidos.sonar("si")
        # `sonar` es síncrono: en este punto no puede haber sonado todavía nada.
        assert reproductor.reproducidos == []
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert reproductor.reproducidos == [pitidos.ruta_de("si")]


async def test_un_pitido_desconocido_no_revienta(tmp_path: Path) -> None:
    reproductor = ReproductorFalso()
    pitidos = Pitidos(tmp_path, reproductor=reproductor)
    async with pitidos:
        pitidos.sonar("este-no-existe")
        await asyncio.sleep(0)
    assert reproductor.reproducidos == []


async def test_la_cola_acotada_descarta_en_vez_de_crecer(tmp_path: Path) -> None:
    """Con un `aplay` colgado o alguien aporreando botones, se descarta.

    Un pitido que llega tarde ya no informa de nada, así que acumularlos solo
    empeora el problema.
    """
    pitidos = Pitidos(tmp_path, reproductor=ReproductorFalso())
    pitidos.preparar()
    # Sin tarea que consuma, la cola se llena y los siguientes se descartan.
    for _ in range(TAMANO_COLA + 10):
        pitidos.sonar("si")
    assert pitidos._cola.qsize() == TAMANO_COLA


async def test_un_fallo_al_reproducir_no_mata_la_tarea(tmp_path: Path) -> None:
    """Si el `aplay` de un pitido falla, los siguientes tienen que seguir sonando."""
    reproductor = ReproductorFalso(falla=True)
    pitidos = Pitidos(tmp_path, reproductor=reproductor)
    async with pitidos:
        pitidos.sonar("error")
        for _ in range(6):
            await asyncio.sleep(0)
        pitidos.sonar("si")
        for _ in range(6):
            await asyncio.sleep(0)

    assert len(reproductor.reproducidos) == 2


async def test_salir_del_contexto_para_la_tarea(tmp_path: Path) -> None:
    pitidos = Pitidos(tmp_path, reproductor=ReproductorFalso())
    async with pitidos:
        pass
    assert pitidos._tarea is None


@pytest.mark.parametrize("nombre", sorted(CATALOGO))
def test_todos_los_pitidos_duran_menos_de_medio_segundo(nombre: str) -> None:
    """Un feedback que dura más que la acción deja de ser feedback."""
    assert sum(t.ms for t in CATALOGO[nombre]) < 500
