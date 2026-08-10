"""Recuperación aumentada por generación (RAG) sobre ChromaDB.

El flujo completo es::

    corpus/*.md ──► chunking.py ──► embeddings.py ──► store.py (ChromaDB)
                                                          │
                        tools/knowledge.py ◄── retriever.py

La ingesta (`ingest.py`) se ejecuta fuera de línea; el agente solo consulta.
"""
