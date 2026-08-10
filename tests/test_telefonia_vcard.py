"""Análisis de las vCard que devuelve un Android por PBAP.

Las fichas de este fichero están escritas a mano imitando lo que manda un móvil
de verdad, incluidas las dos cosas que rompen a los analizadores ingenuos: el
`QUOTED-PRINTABLE` de la vCard 2.1 y el plegado de líneas.
"""

from __future__ import annotations

from voice_agent_telefonia.vcard import analizar_vcards

VCARD_21_QUOTED_PRINTABLE = """BEGIN:VCARD
VERSION:2.1
N;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:P=C3=A9rez;Mar=C3=ADa;;;
FN;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:Mar=C3=ADa P=C3=A9rez
TEL;CELL:+57 300 123 45 67
END:VCARD
"""

VCARD_30 = """BEGIN:VCARD
VERSION:3.0
N:Gómez;Ana;;;
FN:Ana Gómez
TEL;TYPE=CELL:3009876543
TEL;TYPE=HOME:6012223344
END:VCARD
"""

# Sin FN: hay que componer el nombre a partir de N, y en el orden en que se
# dice, no en el que viene.
VCARD_SIN_FN = """BEGIN:VCARD
VERSION:3.0
N:Rodríguez;Juan Carlos;;;
TEL;TYPE=CELL:3001112233
END:VCARD
"""

# Sin teléfono no sirve de nada: no se puede llamar a un nombre.
VCARD_SIN_TEL = """BEGIN:VCARD
VERSION:3.0
N:Nadie;Don;;;
FN:Don Nadie
END:VCARD
"""

# El plegado a 76 caracteres, con las dos convenciones a la vez.
VCARD_PLEGADA = """BEGIN:VCARD
VERSION:2.1
FN;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:Mar=C3=ADa de los =
=C3=81ngeles
TEL;CELL:3004445566
END:VCARD
"""

VCARD_REPETIDA = """BEGIN:VCARD
VERSION:3.0
FN:Sara Ruiz
TEL;TYPE=CELL:3005556677
TEL;TYPE=VOICE:300 555 66 77
END:VCARD
"""


def test_quoted_printable_se_descodifica() -> None:
    """El error clásico de PBAP: sin esto la agenda se llena de 'Mar=C3=ADa'."""
    (contacto,) = analizar_vcards(VCARD_21_QUOTED_PRINTABLE)
    assert contacto.nombre == "María Pérez"
    assert contacto.numeros[0].numero == "+573001234567"
    assert contacto.numeros[0].tipo == "movil"


def test_vcard_30_con_varios_numeros() -> None:
    (contacto,) = analizar_vcards(VCARD_30)
    assert contacto.nombre == "Ana Gómez"
    assert [n.tipo for n in contacto.numeros] == ["movil", "casa"]
    assert contacto.numero_preferido == "3009876543"


def test_sin_fn_el_nombre_se_compone_de_n() -> None:
    (contacto,) = analizar_vcards(VCARD_SIN_FN)
    # Nombre y luego apellidos, que es como se dice y como se busca.
    assert contacto.nombre == "Juan Carlos Rodríguez"


def test_una_ficha_sin_telefono_se_descarta() -> None:
    assert analizar_vcards(VCARD_SIN_TEL) == []


def test_lineas_plegadas() -> None:
    (contacto,) = analizar_vcards(VCARD_PLEGADA)
    assert contacto.nombre == "María de los Ángeles"


def test_el_mismo_numero_dos_veces_se_queda_en_uno() -> None:
    """Pasa cuando el móvil sincroniza la misma cuenta por dos vías."""
    (contacto,) = analizar_vcards(VCARD_REPETIDA)
    assert len(contacto.numeros) == 1


def test_una_ficha_rota_no_tira_las_demas() -> None:
    """Una agenda real siempre trae alguna entrada rara; perder las buenas por
    una mala sería absurdo."""
    texto = VCARD_30 + "BEGIN:VCARD\nesto no es una vcard\nEND:VCARD\n" + VCARD_SIN_FN
    contactos = analizar_vcards(texto)
    assert [c.nombre for c in contactos] == ["Ana Gómez", "Juan Carlos Rodríguez"]


def test_fichero_vacio() -> None:
    assert analizar_vcards("") == []
