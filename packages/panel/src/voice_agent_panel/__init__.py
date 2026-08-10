"""Panel web de configuración y control del agente de voz.

Ver `packages/panel/README.md`. La regla que ordena todo el paquete: este
proceso **no puede importar `voice_agent`**, solo `voice_agent_core`. Lo otro
arrastraría Pipecat y chromadb a una imagen que existe precisamente para ser
pequeña y rápida de reconstruir.
"""

from __future__ import annotations

__version__ = "0.1.0"
