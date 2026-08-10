"""Recursos compartidos que las herramientas reciben en cada llamada.

Las herramientas del agente necesitan cosas caras de construir: el buscador de
la base de conocimiento, que carga un modelo de embeddings y abre el índice, y
la propia configuración. Instanciarlas dentro de cada herramienta sería
absurdo, y guardarlas en variables globales del módulo haría el código
imposible de probar aisladamente.

Pipecat resuelve esto con `app_resources`: un objeto cualquiera que se le
entrega al `PipelineWorker` y que llega intacto —por referencia, no copiado— a
todas las llamadas a herramientas dentro de `FunctionCallParams`. Aquí se
define ese objeto.
"""

from __future__ import annotations

from dataclasses import dataclass

from voice_agent.rag.retriever import Retriever
from voice_agent.telefonia import ClienteTelefonia
from voice_agent_core.config import Settings


@dataclass
class AppResources:
    """Estado compartido por todas las herramientas de una sesión.

    Attributes:
        settings: La configuración efectiva del agente.
        retriever: El buscador sobre la base de conocimiento.
        telefonia: El cliente del puente de telefonía, o `None` si este agente
            no tiene teléfono. Es opcional y va al final a propósito: así los
            tests que construyen `AppResources` a mano siguen valiendo sin
            tocarlos, y un agente sin puente es exactamente el de siempre.
    """

    settings: Settings
    retriever: Retriever
    telefonia: ClienteTelefonia | None = None
