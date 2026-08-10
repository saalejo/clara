"""Analizador de las vCard que devuelve el móvil por PBAP.

No se usa una librería porque el problema es más pequeño de lo que parece —de
cada ficha solo interesan el nombre y los teléfonos— y porque las que hay
arrastran dependencias por algo que cabe en doscientas líneas.

## Lo que de verdad complica esto

**PBAP entrega vCard 2.1 por defecto**, no 3.0, y la 2.1 no sabe de UTF-8: los
acentos viajan como `QUOTED-PRINTABLE`. Una ficha de "María" llega así:

    N;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:Mar=C3=ADa;;;;

Si no se descodifica, la agenda entera se llena de nombres rotos y el buscador
deja de encontrar a media familia. Es el error clásico de PBAP y es silencioso:
no falla nada, simplemente no aparece nadie.

**Las líneas se parten a los 76 caracteres.** En vCard el plegado se marca con
una línea siguiente que empieza por espacio o tabulador; en QUOTED-PRINTABLE, en
cambio, se marca con un `=` al final de la línea. Hay que soportar los dos.

**El campo `N` viene por partes** (apellido; nombre; medio; prefijo; sufijo) y
`FN` es el nombre ya compuesto. `FN` es mejor cuando está, porque es el que el
usuario ve en su móvil, pero no siempre viene.
"""

from __future__ import annotations

import quopri
import re
from collections.abc import Iterator

from voice_agent_core.telefonia import Contacto, NumeroContacto
from voice_agent_telefonia.normaliza import normalizar_numero

#: `TIPO;PARAM=valor;PARAM=valor:contenido`
_LINEA = re.compile(r"^(?P<nombre>[A-Za-z0-9.\-]+)(?P<params>(?:;[^:]*)*):(?P<valor>.*)$")

#: Traducción de los tipos de teléfono de vCard a los cuatro que usamos. Se
#: reduce a propósito: al modelo le sirve "móvil o fijo", y más matices solo
#: harían las respuestas habladas más largas sin ayudar a nadie.
_TIPOS = {
    "cell": "movil",
    "mobile": "movil",
    "home": "casa",
    "work": "trabajo",
    "voice": "otro",
    "pref": "otro",
}


def _desplegar(texto: str) -> list[str]:
    """Junta las líneas partidas, con las dos convenciones que se usan a la vez.

    Devuelve las líneas lógicas, ya enteras.
    """
    lineas: list[str] = []
    for cruda in texto.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        # Plegado de vCard: la continuación empieza por espacio o tabulador.
        if lineas and cruda[:1] in (" ", "\t"):
            lineas[-1] += cruda[1:]
            continue
        # Plegado de QUOTED-PRINTABLE: la línea anterior acaba en `=`.
        if lineas and lineas[-1].endswith("="):
            lineas[-1] = lineas[-1][:-1] + cruda.lstrip()
            continue
        lineas.append(cruda)
    return lineas


def _descodificar(valor: str, params: str) -> str:
    """Aplica QUOTED-PRINTABLE y el juego de caracteres si la línea lo pide."""
    p = params.lower()
    if "quoted-printable" not in p:
        return valor

    crudo = quopri.decodestring(valor.encode("ascii", "replace"))
    juego = "utf-8"
    if (m := re.search(r"charset=([^;:]+)", p)) is not None:
        juego = m.group(1).strip()
    try:
        return crudo.decode(juego, errors="replace")
    except LookupError:
        # Un charset que Python no conoce no debe tirar la agenda entera.
        return crudo.decode("utf-8", errors="replace")


def _nombre_desde_n(valor: str) -> str:
    """Compone un nombre legible a partir del campo `N` de vCard.

    `N` es `apellidos;nombre;medio;prefijo;sufijo`. Se devuelve en el orden en
    que se dice en voz alta —nombre y luego apellidos—, que es también el orden
    en el que la gente lo busca.
    """
    partes = [p.strip() for p in valor.split(";")]
    partes += [""] * (5 - len(partes))
    apellidos, nombre, medio, prefijo, _sufijo = partes[:5]
    trozos = [prefijo, nombre, medio, apellidos]
    return " ".join(t for t in trozos if t).strip()


def _fichas(lineas: list[str]) -> Iterator[list[str]]:
    """Trocea el fichero en fichas, de BEGIN a END."""
    actual: list[str] | None = None
    for linea in lineas:
        despojada = linea.strip()
        if despojada.upper().startswith("BEGIN:VCARD"):
            actual = []
        elif despojada.upper().startswith("END:VCARD"):
            if actual is not None:
                yield actual
            actual = None
        elif actual is not None:
            actual.append(linea)


def analizar_vcards(texto: str) -> list[Contacto]:
    """Convierte el contenido de un fichero .vcf en contactos.

    Es deliberadamente tolerante: una ficha rota se salta sin ruido en vez de
    tirar la descarga entera. La agenda de un móvil real siempre trae alguna
    entrada rara —un contacto sin nombre, otro sin teléfono, el buzón de voz— y
    perder los 300 buenos por uno malo sería absurdo.

    Args:
        texto: El contenido completo del .vcf descargado por PBAP.

    Returns:
        Los contactos que tienen, como mínimo, un nombre y un teléfono.
    """
    contactos: list[Contacto] = []

    for ficha in _fichas(_desplegar(texto)):
        nombre_completo = ""
        nombre_estructurado = ""
        numeros: list[NumeroContacto] = []

        for linea in ficha:
            m = _LINEA.match(linea.strip())
            if m is None:
                continue
            campo = m.group("nombre").upper()
            params = m.group("params")
            valor = _descodificar(m.group("valor"), params)

            if campo == "FN":
                nombre_completo = valor.strip()
            elif campo == "N":
                nombre_estructurado = _nombre_desde_n(valor)
            elif campo == "TEL":
                numero = normalizar_numero(valor)
                if not numero:
                    continue
                tipo = "otro"
                for etiqueta, traducido in _TIPOS.items():
                    if etiqueta in params.lower():
                        tipo = traducido
                        # `cell` gana a `voice`/`pref`, que son genéricos.
                        if traducido != "otro":
                            break
                numeros.append(NumeroContacto(tipo=tipo, numero=numero))

        nombre = nombre_completo or nombre_estructurado
        if not nombre or not numeros:
            continue

        # Un mismo número repetido en la ficha (pasa cuando el móvil sincroniza
        # la misma cuenta dos veces) se queda en uno.
        vistos: set[str] = set()
        unicos: list[NumeroContacto] = []
        for n in numeros:
            if n.numero not in vistos:
                vistos.add(n.numero)
                unicos.append(n)

        contactos.append(Contacto(nombre=nombre, numeros=unicos))

    return contactos


__all__ = ["analizar_vcards"]
