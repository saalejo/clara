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
from voice_agent.traza import TrazaLlamada
from voice_agent_core.config import Settings
from voice_agent_core.evaluaciones import Alerta
from voice_agent_core.historial import HistorialPacientes


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
        traza: El registro documental de la llamada en curso, o `None` fuera
            de una llamada. Lo crea `web.py` por conexión: la trazabilidad es
            de la llamada, no del proceso, así que dos llamadas seguidas no
            pueden compartirla.
    """

    settings: Settings
    retriever: Retriever
    telefonia: ClienteTelefonia | None = None
    traza: TrazaLlamada | None = None
    #: La última alerta registrada en esta llamada, para el resumen de
    #: respaldo si el paciente cuelga sin despedirse.
    ultima_alerta: Alerta | None = None
    #: Si el modelo llegó a guardar el resumen con `finalizar_llamada`. Cuando
    #: no, el pipeline escribe uno de respaldo al desmontarse.
    resumen_guardado: bool = False
    #: La memoria entre llamadas por número de teléfono, compartida por todo
    #: el proceso, o `None` en un agente sin ella (los tests, `bot.py`).
    historial: HistorialPacientes | None = None
    #: El número del otro extremo de la llamada en curso, si el puente lo
    #: identificó; vacío en el navegador o con número oculto. Es lo que ata
    #: las anotaciones del historial a una ficha.
    numero_llamada: str = ""
