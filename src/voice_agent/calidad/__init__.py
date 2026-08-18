"""Arnés de pruebas de calidad adversarias contra Clara, sin voz.

Este paquete vive en la imagen **pesada** (la del agente): sí puede importar
Pipecat, el LLM y las herramientas. Conversa contra el mismo cerebro que atiende
a los pacientes —prompt de sistema, RAG y herramientas reales— pero por texto,
para poder ensayar los ataques (inyección, hostilidad, banderas rojas…) a solas
y medir cómo responde.

El contrato con el panel (catálogo de escenarios y forma de los resultados) vive
en `voice_agent_core.calidad`, porque el panel no puede importar `voice_agent`.
El panel encola una `SolicitudCalidad` en disco y arranca la unidad oneshot
`clara-calidad.service`; este paquete la ejecuta y deja los
`ResultadoEscenario` donde el panel los lee.
"""

from __future__ import annotations
