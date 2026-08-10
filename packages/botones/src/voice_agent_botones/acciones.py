"""Qué hace cada gesto: la tabla, y nada más que la tabla.

Este módulo es datos puros. No ejecuta, no importa asyncio y no sabe qué es un
servicio de systemd: eso está en `demonio.py`. Separarlo permite que un test fije
el mapa entero, parametrizado, y que mover una acción de sitio sea un cambio
visible en un diff en lugar de un `if` enterrado en un bucle.

## Cómo se reparten nueve acciones entre tres teclas

Lo impone el hardware, medido en la placa (ver `gestos.py`): **`KEY_MUTE` no
distingue mantener pulsado** y el rocker de volumen sí. Así que los tres niveles
por duración viven solo en el rocker, y MUTE es un botón de una sola acción.

Sale mejor de lo que se había planeado. Al no necesitar contar clics en ninguna
tecla, el clic de MUTE no paga latencia de detección — y es la acción más
frecuente, la única en la que la latencia se nota, porque se silencia el micrófono
porque alguien acaba de entrar en la habitación *ahora*.

## Las tres reglas del mapa

1. **MUTE significa siempre «la acción obvia de este momento»**: silenciar el
   micrófono, o contestar si el teléfono está sonando. Es lo único que hay que
   memorizar, y en una interfaz sin pantalla eso vale más que la simetría.
2. **El volumen es volumen en los tres modos.** Nivel 1 del rocker no cambia de
   significado nunca.
3. **Durante una llamada, los gestos que tocan servicios no existen.** Parar el
   agente mientras hablas por teléfono no es algo que nadie quiera de verdad, y
   el modo lo decide un estado invisible.
"""

from __future__ import annotations

from enum import StrEnum

from voice_agent_botones.gestos import (
    KEY_MUTE,
    KEY_VOLUMEDOWN,
    KEY_VOLUMEUP,
    Gesto,
    Nivel,
)


class Modo(StrEnum):
    """En qué situación está el sistema cuando llega el gesto.

    Lo determina el demonio escuchando el canal de eventos del puente de
    telefonía. Sin puente, siempre es `NORMAL`.
    """

    NORMAL = "normal"
    LLAMADA_ENTRANTE = "llamada_entrante"
    LLAMADA_EN_CURSO = "llamada_en_curso"


class Verbo(StrEnum):
    """Lo que hay que hacer. `NADA` es un gesto sin acción en este modo."""

    NADA = "nada"
    MICRO = "micro"
    VOLUMEN_MAS = "volumen_mas"
    VOLUMEN_MENOS = "volumen_menos"
    AUTOCONTESTAR = "autocontestar"
    SOLO_TARJETA = "solo_tarjeta"
    AGENTE_ALTERNAR = "agente_alternar"
    AGENTE_REINICIAR = "agente_reiniciar"
    CONTESTAR = "contestar"
    RECHAZAR = "rechazar"
    COLGAR = "colgar"


# El mapa. Se declara como tabla y no como cadena de `if` para que el test que lo
# fija pueda recorrerlo entero: es la única defensa contra que una acción cambie
# de gesto sin que nadie se dé cuenta.
#
# Las combinaciones que no aparecen valen `NADA` (ver `verbo_de`). En particular,
# el nivel 3 durante una llamada no está a propósito.
MAPA: dict[tuple[Modo, int, Nivel], Verbo] = {
    # --- Sin llamada, o sin puente de telefonía ---------------------------
    (Modo.NORMAL, KEY_MUTE, Nivel.CORTO): Verbo.MICRO,
    (Modo.NORMAL, KEY_VOLUMEUP, Nivel.CORTO): Verbo.VOLUMEN_MAS,
    (Modo.NORMAL, KEY_VOLUMEDOWN, Nivel.CORTO): Verbo.VOLUMEN_MENOS,
    (Modo.NORMAL, KEY_VOLUMEUP, Nivel.LARGO): Verbo.AUTOCONTESTAR,
    (Modo.NORMAL, KEY_VOLUMEDOWN, Nivel.LARGO): Verbo.SOLO_TARJETA,
    (Modo.NORMAL, KEY_VOLUMEUP, Nivel.MUY_LARGO): Verbo.AGENTE_ALTERNAR,
    (Modo.NORMAL, KEY_VOLUMEDOWN, Nivel.MUY_LARGO): Verbo.AGENTE_REINICIAR,
    # --- El teléfono está sonando -----------------------------------------
    (Modo.LLAMADA_ENTRANTE, KEY_MUTE, Nivel.CORTO): Verbo.CONTESTAR,
    (Modo.LLAMADA_ENTRANTE, KEY_VOLUMEUP, Nivel.CORTO): Verbo.VOLUMEN_MAS,
    (Modo.LLAMADA_ENTRANTE, KEY_VOLUMEDOWN, Nivel.CORTO): Verbo.VOLUMEN_MENOS,
    (Modo.LLAMADA_ENTRANTE, KEY_VOLUMEUP, Nivel.LARGO): Verbo.RECHAZAR,
    # --- Hay una llamada en curso -----------------------------------------
    (Modo.LLAMADA_EN_CURSO, KEY_MUTE, Nivel.CORTO): Verbo.MICRO,
    (Modo.LLAMADA_EN_CURSO, KEY_VOLUMEUP, Nivel.CORTO): Verbo.VOLUMEN_MAS,
    (Modo.LLAMADA_EN_CURSO, KEY_VOLUMEDOWN, Nivel.CORTO): Verbo.VOLUMEN_MENOS,
    (Modo.LLAMADA_EN_CURSO, KEY_VOLUMEUP, Nivel.LARGO): Verbo.COLGAR,
}

# Verbos que PUEDEN pedir confirmación. Si de verdad la piden depende del estado
# —arrancar el agente es inocuo, pararlo no—, y esa decisión es del demonio, que
# es quien conoce el estado.
DESTRUCTIVOS = frozenset({Verbo.AGENTE_ALTERNAR, Verbo.AGENTE_REINICIAR, Verbo.SOLO_TARJETA})

# Verbos que necesitan el puente de telefonía en marcha.
NECESITAN_PUENTE = frozenset({Verbo.AUTOCONTESTAR, Verbo.CONTESTAR, Verbo.RECHAZAR, Verbo.COLGAR})


def verbo_de(modo: Modo, gesto: Gesto) -> Verbo:
    """Traduce un gesto a un verbo según el modo. Nunca falla."""
    return MAPA.get((modo, gesto.tecla, gesto.nivel), Verbo.NADA)


__all__ = [
    "DESTRUCTIVOS",
    "MAPA",
    "NECESITAN_PUENTE",
    "Modo",
    "Verbo",
    "verbo_de",
]
