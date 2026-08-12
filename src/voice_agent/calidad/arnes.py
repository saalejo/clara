"""Clara por texto: el mismo cerebro que en producción, sin voz.

Reconstruye lo que hace `web.py` al montar una llamada —prompt de sistema
efectivo, catálogo de herramientas filtrado por el panel, y el bucle de function
calling— pero contra un cliente de LLM inyectable y sin pipeline de audio. Las
herramientas que llama son **las de verdad**: `registrar_alerta` escribe una
alerta real (en el sandbox del ensayo), `buscar_en_documentos` consulta el RAG
real y deja su traza. Por eso el arnés mide de verdad y no una imitación.

El puente con las herramientas es `_ParamsEjecucion`, un sustituto de
`FunctionCallParams` con lo único que ellas usan: `app_resources` y
`result_callback`. Es el mismo truco que `tests/test_tools.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from google.genai import types
from loguru import logger
from pipecat.adapters.schemas.direct_function import DirectFunction
from pipecat.adapters.schemas.function_schema import FunctionSchema

from voice_agent.calidad.cliente import ClienteLLM, sumar_uso
from voice_agent.resources import AppResources
from voice_agent.tools import esquema_de, nombre_de
from voice_agent_core.calidad import Turno, UsoLLM
from voice_agent_core.config import Settings

#: Tope de rondas de herramientas dentro de un mismo turno de Clara. Un modelo
#: sano llama una o dos veces (buscar, registrar) y contesta; si encadena cinco
#: rondas sin hablar, algo va mal y se le corta para no colgarse ni gastar de más.
MAX_RONDAS_HERRAMIENTAS = 5


@dataclass
class _ParamsEjecucion:
    """Sustituto de `FunctionCallParams` con lo único que las herramientas usan."""

    app_resources: Any
    resultado: Any = None

    async def result_callback(self, resultado: Any) -> None:
        self.resultado = resultado


def _declaraciones_de(
    herramientas: Sequence[FunctionSchema | DirectFunction],
) -> list[types.FunctionDeclaration]:
    """Traduce el esquema Pipecat de cada herramienta a una declaración de Gemini.

    `esquema_de` devuelve el mismo diccionario que ve el modelo en producción
    (`{"name", "description", "parameters"}`); aquí se envuelve en el tipo del
    SDK. `tests/test_calidad_arnes.py` fija esta traducción contra la salida real
    de Pipecat, para que un cambio de `to_default_dict()` se vea en un test y no
    en la demo.
    """
    declaraciones: list[types.FunctionDeclaration] = []
    for herramienta in herramientas:
        esquema = esquema_de(herramienta)
        parametros = esquema.get("parameters") or {"type": "object", "properties": {}}
        declaraciones.append(
            types.FunctionDeclaration(
                name=str(esquema["name"]),
                description=str(esquema.get("description", "")),
                parameters_json_schema=parametros,
            )
        )
    return declaraciones


class ArnesClara:
    """Clara conversando por texto, con sus herramientas reales."""

    def __init__(
        self,
        cliente: ClienteLLM,
        settings: Settings,
        prompt_sistema: str,
        herramientas: Sequence[FunctionSchema | DirectFunction],
        recursos: AppResources,
    ) -> None:
        """Prepara a Clara con su prompt, sus herramientas y su cliente de LLM."""
        self._cliente = cliente
        self._settings = settings
        self._recursos = recursos
        #: Solo las invocables (las *direct functions*): un `FunctionSchema` es
        #: descripción, no código. Hoy todas son invocables, pero no cuesta nada.
        self._invocables: dict[str, DirectFunction] = {
            nombre_de(h): h for h in herramientas if not isinstance(h, FunctionSchema)
        }
        herramientas_gemini: list[Any] | None = (
            [types.Tool(function_declarations=_declaraciones_de(herramientas))]
            if herramientas
            else None
        )
        self._config = types.GenerateContentConfig(
            system_instruction=prompt_sistema,
            temperature=settings.llm_temperature,
            max_output_tokens=settings.llm_max_tokens,
            # Paridad con `build_llm`: Gemini 2.5 piensa por defecto y aquí no
            # aporta; se apaga para no gastar tokens ni tiempo de más.
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            tools=herramientas_gemini,
        )
        self.uso = UsoLLM()

    async def responder(self, historial: list[types.Content]) -> tuple[str, list[Turno]]:
        """Hace hablar a Clara: despacha las herramientas que pida y devuelve su texto.

        Modifica `historial` en el sitio, añadiendo el turno del modelo y las
        respuestas de herramienta, para que la siguiente vuelta tenga el contexto
        correcto de function calling.

        Returns:
            El texto final de Clara y los turnos de herramienta que ejecutó.
        """
        turnos: list[Turno] = []
        # Se permite una vuelta más que rondas de herramientas: la última es la
        # oportunidad de que Clara conteste con texto tras la última herramienta.
        for ronda in range(MAX_RONDAS_HERRAMIENTAS + 1):
            respuesta = await self._cliente.generar(
                modelo=self._settings.gemini_model,
                contents=historial,
                config=self._config,
            )
            sumar_uso(self.uso, respuesta)

            contenido = respuesta.candidates[0].content if respuesta.candidates else None
            if contenido is not None:
                historial.append(contenido)

            llamadas = list(respuesta.function_calls or [])
            if not llamadas:
                return (respuesta.text or "", turnos)
            if ronda == MAX_RONDAS_HERRAMIENTAS:
                # Ya se ejecutaron MAX rondas y sigue pidiendo herramientas: se
                # corta sin ejecutar más, para no colgarse ni gastar de más.
                break

            partes: list[types.Part] = []
            for llamada in llamadas:
                argumentos = dict(llamada.args or {})
                turno = await self._ejecutar(str(llamada.name), argumentos)
                turnos.append(turno)
                resultado = turno.detalle.get("resultado")
                partes.append(
                    types.Part.from_function_response(
                        name=str(llamada.name),
                        response=resultado if isinstance(resultado, dict) else {},
                    )
                )
            # El rol de la respuesta de herramienta en Gemini es "user".
            historial.append(types.Content(role="user", parts=partes))

        logger.warning(
            f"Clara superó las {MAX_RONDAS_HERRAMIENTAS} rondas de herramientas en un turno."
        )
        return ("", turnos)

    async def _ejecutar(self, nombre: str, argumentos: dict[str, Any]) -> Turno:
        """Ejecuta una herramienta y captura su resultado como un turno."""
        herramienta = self._invocables.get(nombre)
        if herramienta is None:
            logger.warning(f"El modelo pidió una herramienta inexistente: {nombre!r}")
            return Turno(
                rol="herramienta",
                texto=nombre,
                detalle={
                    "argumentos": argumentos,
                    "resultado": {"error": f"Herramienta desconocida: {nombre}"},
                },
            )
        params = _ParamsEjecucion(app_resources=self._recursos)
        try:
            await herramienta(params, **argumentos)
            resultado = params.resultado if params.resultado is not None else {}
        except Exception as e:
            # Un fallo de herramienta no debe tumbar el ensayo: se le devuelve el
            # error al modelo y se sigue, que además prueba su robustez.
            logger.warning(f"La herramienta {nombre} falló: {e}")
            resultado = {"error": f"La herramienta falló: {e}"}
        return Turno(
            rol="herramienta",
            texto=nombre,
            detalle={"argumentos": argumentos, "resultado": resultado},
        )
