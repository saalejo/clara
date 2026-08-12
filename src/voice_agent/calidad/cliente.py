"""El cliente del LLM del arnés, tras un protocolo para poder falsearlo.

El runner hace tres papeles con el mismo modelo: **Clara** (con herramientas),
el **paciente simulado** y el **juez**. Los tres pasan por `ClienteLLM`, un
protocolo mínimo sobre `generate_content`. Que sea un protocolo es lo que permite
que los tests inyecten un doble con respuestas guionizadas y se prueben el bucle
de despacho de herramientas y la orquestación sin tocar la red.
"""

from __future__ import annotations

from typing import Any, Protocol

from voice_agent_core.calidad import UsoLLM


class ClienteLLM(Protocol):
    """Lo único que el arnés necesita de un cliente de LLM."""

    async def generar(self, *, modelo: str, contents: Any, config: Any) -> Any:
        """Genera una respuesta y la devuelve tal cual la da el SDK."""
        ...


class ClienteGemini:
    """El cliente real: `google.genai` por su SDK nativo, como `build_llm`.

    Se usa el SDK nativo y no el endpoint OpenAI-compatible por el mismo motivo
    documentado en `services.build_llm`: el shim emite tool-calls fantasma que
    envenenan el historial. Aquí, además, el arnés depende de function calling
    en cada turno, así que el shim lo rompería aún antes.
    """

    def __init__(self, api_key: str) -> None:
        """Abre el cliente de `google.genai` con la clave de AI Studio."""
        from google import genai

        self._client = genai.Client(api_key=api_key)

    async def generar(self, *, modelo: str, contents: Any, config: Any) -> Any:
        """Genera una respuesta con el SDK de Gemini y la devuelve tal cual."""
        return await self._client.aio.models.generate_content(
            model=modelo, contents=contents, config=config
        )


def sumar_uso(uso: UsoLLM, respuesta: Any) -> None:
    """Acumula en `uso` los tokens de una respuesta del SDK.

    El SDK expone `usage_metadata` con `prompt_token_count` y
    `candidates_token_count`; un doble de prueba puede omitirlo y aquí no pasa
    nada. Cuenta la llamada aunque no haya metadatos, para poder medir el gasto.
    """
    uso.llamadas += 1
    meta = getattr(respuesta, "usage_metadata", None)
    if meta is not None:
        uso.tokens_entrada += getattr(meta, "prompt_token_count", 0) or 0
        uso.tokens_salida += getattr(meta, "candidates_token_count", 0) or 0
