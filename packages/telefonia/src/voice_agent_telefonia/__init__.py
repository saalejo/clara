"""Puente Bluetooth manos libres (HFP-HF) entre el móvil y el agente de voz.

Ver `packages/telefonia/README.md` para la arquitectura y las tres decisiones
que la explican: por qué es un paquete aparte, por qué corre nativo en vez de en
un contenedor, y por qué usa `dbus-fast` en lugar del `jeepney` que ya emplea el
panel.
"""

from __future__ import annotations

__version__ = "0.1.0"
