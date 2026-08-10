"""Mando físico del agente de voz: los botones de la tarjeta de sonido USB.

Ver `packages/botones/README.md` para el hardware medido y las decisiones que lo
explican. Las tres que hay que conocer antes de tocar nada:

- **El botón del micrófono no emite nada.** Es hardware puro; desde el software
  es indistinguible de un botón desconectado.
- **`KEY_MUTE` no distingue mantener pulsado**, así que los niveles por duración
  solo pueden vivir en el rocker de volumen.
- **El micrófono y el volumen se controlan en el mezclador de ALSA**, no dentro
  del agente, y por eso el agente no necesita ni una línea de cambio.
"""

from __future__ import annotations

__version__ = "0.1.0"
