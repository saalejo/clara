"""Agente de voz conversacional en español para NanoPi R4S.

Construido sobre Pipecat, con el LLM servido por Google AI Studio o Groq,
transcripción en streaming, síntesis de voz local, y una base de conocimiento
consultable (RAG sobre ChromaDB) que el modelo usa como una herramienta más.

Ver `docs/arquitectura.md` para el recorrido completo del audio a través del
pipeline.
"""

__version__ = "0.1.0"
