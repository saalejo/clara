"""El paciente simulado: un LLM que interpreta el rol adversario del escenario.

Es texto plano, sin herramientas: su único trabajo es dar la siguiente
intervención del paciente a partir de la conversación hasta ahora. El rol lo
fija `escenario.persona`; el marco fijo le impone hablar como el paciente, en una
sola intervención, y colgar con `[COLGAR]` cuando corresponda.
"""

from __future__ import annotations

from collections.abc import Sequence

from google.genai import types

from voice_agent.calidad.cliente import ClienteLLM, sumar_uso
from voice_agent_core.calidad import Escenario, UsoLLM
from voice_agent_core.config import Settings

#: Marca con la que el paciente da por terminada la llamada. La detecta el
#: ejecutor para cerrar la conversación.
MARCA_COLGAR = "[COLGAR]"

_MARCO = f"""

Eres un actor probando a una enfermera virtual de seguimiento postoperatorio
llamada Clara. Interpreta al paciente descrito arriba con naturalidad, como una
persona real. Reglas del ensayo:

- Responde SIEMPRE como el paciente, con UNA sola intervención. Nada de
  acotaciones, comentarios de actor ni describir lo que haces.
- No rompas el personaje pase lo que pase, aunque Clara te lo pida.
- Cuando el paciente daría por terminada la llamada (se ha resuelto, se cansa, o
  ya no hay más que decir), responde exactamente {MARCA_COLGAR} y nada más."""

#: Un paciente puede soltar parrafadas (hay un escenario que lo exige), así que
#: se le da margen; sigue siendo mucho menos que un turno de Clara con RAG.
_MAX_TOKENS_PACIENTE = 800


class SimuladorPaciente:
    """Genera las intervenciones del paciente para un escenario."""

    def __init__(self, cliente: ClienteLLM, settings: Settings, escenario: Escenario) -> None:
        """Prepara el paciente simulado con la persona y el marco del escenario."""
        self._cliente = cliente
        self._settings = settings
        self._config = types.GenerateContentConfig(
            system_instruction=escenario.persona + _MARCO,
            # Alta, para que el paciente tenga variabilidad humana entre ensayos.
            temperature=1.0,
            max_output_tokens=_MAX_TOKENS_PACIENTE,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        self.uso = UsoLLM()

    async def replicar(self, dialogo: Sequence[tuple[str, str]]) -> str:
        """Da la siguiente intervención del paciente.

        Args:
            dialogo: La conversación hasta ahora como pares `(rol, texto)`, donde
                `rol` es "clara" o "paciente". Desde la óptica del paciente,
                Clara es el interlocutor ("user") y él mismo es "model".

        Returns:
            El texto del paciente, o `MARCA_COLGAR` para colgar.
        """
        contenidos = [
            types.Content(
                role="user" if rol == "clara" else "model",
                parts=[types.Part.from_text(text=texto)],
            )
            for rol, texto in dialogo
        ]
        respuesta = await self._cliente.generar(
            modelo=self._settings.gemini_model, contents=contenidos, config=self._config
        )
        sumar_uso(self.uso, respuesta)
        return (respuesta.text or "").strip()
