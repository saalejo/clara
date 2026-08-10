"""Normalización de nombres y números, y la clave fonética española.

Son funciones puras, así que se prueban con tablas. Cada caso de la tabla
fonética viene de un error de transcripción real o plausible de Whisper `tiny`
con nombres propios españoles, que es exactamente para lo que existe la función.
"""

from __future__ import annotations

import pytest

from voice_agent_telefonia.normaliza import (
    clave_fonetica,
    mismo_numero,
    normalizar_nombre,
    normalizar_numero,
    puntuar,
)


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("María José", "maria jose"),
        ("  ANA   PÉREZ  ", "ana perez"),
        ("Jean-Luc", "jean luc"),
        ("O'Connor", "o connor"),
        ("Mª Ángeles", "m angeles"),
        ("Peña", "peña"),  # la eñe se conserva: distingue palabras
        ("Núñez", "nuñez"),
        ("", ""),
        ("   ", ""),
        ("+++", ""),
    ],
)
def test_normalizar_nombre(entrada: str, esperado: str) -> None:
    assert normalizar_nombre(entrada) == esperado


@pytest.mark.parametrize(
    ("uno", "otro"),
    [
        ("Bárbara", "Varvara"),  # b/v
        ("Sánchez", "Sanches"),  # seseo
        ("Jiménez", "Giménez"),  # ge/je
        ("Llanos", "Yanos"),  # yeísmo
        ("Quique", "Kike"),  # qu/k
        ("Hernán", "Ernán"),  # hache muda
        ("Ximena", "Simena"),  # equis
        ("Anna", "Ana"),  # dobles
        ("Cecilia", "Sesilia"),  # el caso que rompe si se pliega mal el orden
        ("Guillermo", "Guiyermo"),  # u muda + yeísmo
        ("Beatriz", "Beatris"),
    ],
)
def test_suenan_igual(uno: str, otro: str) -> None:
    assert clave_fonetica(uno) == clave_fonetica(otro), (
        f"{uno!r} -> {clave_fonetica(uno)!r} vs {otro!r} -> {clave_fonetica(otro)!r}"
    )


@pytest.mark.parametrize(
    ("uno", "otro"),
    [
        ("Ana", "Elena"),
        ("Pérez", "López"),
        ("Carlos", "Marcos"),
        # "gui" suena con g dura y "gi" como jota: son sonidos distintos, y
        # plegarlos juntos sería un error, no una mejora.
        ("Guillermo", "Gillermo"),
    ],
)
def test_no_suenan_igual(uno: str, otro: str) -> None:
    assert clave_fonetica(uno) != clave_fonetica(otro)


def test_la_che_no_se_pierde_con_la_hache_muda() -> None:
    """La hache es muda, pero la 'ch' sí suena: no pueden plegarse igual."""
    assert clave_fonetica("Chávez") != clave_fonetica("Ávez")


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("300 123 45 67", "3001234567"),
        ("+57 300 1234567", "+573001234567"),
        ("(300) 123-4567", "3001234567"),
        ("0057 300 1234567", "+573001234567"),  # 00 -> +
        ("011 57 3001234567", "+573001234567"),  # 011 -> + (marcación desde América)
        ("", ""),
        ("no es un número", ""),
    ],
)
def test_normalizar_numero(entrada: str, esperado: str) -> None:
    assert normalizar_numero(entrada) == esperado


@pytest.mark.parametrize(
    ("uno", "otro", "iguales"),
    [
        ("+573001234567", "3001234567", True),  # con y sin prefijo de país
        ("300 123 45 67", "+57 300 123 45 67", True),
        ("3001234567", "3001234568", False),
        ("123456", "123456", True),  # cortos: comparación exacta
        ("123456", "999123456", False),  # y sin colar por los últimos dígitos
        ("", "3001234567", False),
    ],
)
def test_mismo_numero(uno: str, otro: str, iguales: bool) -> None:
    assert mismo_numero(uno, otro) is iguales


class TestPuntuar:
    def test_identico_puntua_cien(self) -> None:
        assert puntuar("Ana Pérez", "ana perez") == 100

    def test_prefijos_en_orden(self) -> None:
        """'ana pe' es como la gente busca de viva voz."""
        assert puntuar("ana pe", "Ana Pérez") == 90

    def test_orden_distinto(self) -> None:
        assert puntuar("pérez ana", "Ana Pérez") == 80

    def test_solo_apellido(self) -> None:
        assert puntuar("pérez", "Ana Pérez") >= 80

    def test_transcripcion_fonetica(self) -> None:
        """El caso que justifica la clave fonética."""
        assert puntuar("Varvara", "Bárbara") >= 55

    def test_desconocido_puntua_bajo(self) -> None:
        assert puntuar("Ana", "Juan Carlos Rodríguez") < 45

    def test_cadena_vacia_no_revienta(self) -> None:
        assert puntuar("", "Ana") == 0
        assert puntuar("Ana", "") == 0
