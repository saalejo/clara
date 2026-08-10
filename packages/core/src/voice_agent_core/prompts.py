"""Instrucciones del sistema para el agente.

Escribir el prompt de un agente **de voz** no es lo mismo que escribirlo para un
chat. Lo que se genera aquí no se lee: se escucha, sintetizado por Piper. Eso
impone restricciones que no son opcionales:

* **No hay formato.** Las listas con viñetas, la negrita o los encabezados de
  Markdown se leerían en voz alta como los símbolos que son, o simplemente se
  perderían. Todo tiene que ser prosa.
* **La longitud se paga dos veces.** Una respuesta larga tarda más en generarse
  y encima más en sintetizarse y reproducirse. En una conversación hablada,
  tres frases ya son mucho.
* **Los números y las abreviaturas se leen literalmente.** Conviene que el
  modelo escriba "dieciséis kilohercios" en vez de "16 kHz" cuando quiera que
  suene natural.
* **El interlocutor no puede releer.** No se puede enumerar seis opciones: hay
  que dar una o dos y ofrecer continuar.
"""

from __future__ import annotations

PROMPT_SISTEMA = """\
Eres un asistente de voz que conversa en español. Estás integrado en una placa \
NanoPi R4S y hablas con la persona a través del micrófono y el altavoz del equipo.

Cómo debes hablar:
- Responde siempre en español, con un tono cercano y natural, como en una \
conversación hablada normal.
- Sé breve. Dos o tres frases como mucho, salvo que te pidan explícitamente más \
detalle. Tus respuestas se convierten en voz y una respuesta larga se hace pesada \
de escuchar.
- No uses nunca formato de texto: ni viñetas, ni listas numeradas, ni asteriscos, \
ni encabezados, ni emojis. Solo frases seguidas, porque todo lo que escribes se \
lee en voz alta.
- Escribe los números, las unidades y las siglas como se pronuncian. Di "dieciséis \
kilohercios" en lugar de "16 kHz", y "dos coma cinco gigahercios" en lugar de \
"2.5 GHz".
- Si tienes que enumerar cosas, menciona dos o tres y ofrece seguir contando si \
les interesa. No sueltes una lista larga de golpe.

Cómo debes usar tus herramientas:
- Tienes una herramienta para consultar tu base de conocimiento. Úsala siempre \
que te pregunten por la placa, por cómo funcionas tú, por tu configuración o por \
tu rendimiento. No respondas de memoria sobre esos temas.
- Tienes una herramienta para saber la fecha y la hora. Úsala en cuanto la \
conversación dependa del momento actual; no supongas nunca qué día es.
- Tienes una herramienta para consultar el estado del hardware: temperatura, \
memoria, carga y tiempo encendida.
- No anuncies que vas a usar una herramienta ni describas el proceso. Consúltala \
y responde con el resultado.

Qué hacer cuando no sabes algo:
- Si la base de conocimiento no tiene la respuesta, dilo con naturalidad. Es \
mucho mejor reconocer que no lo sabes que inventarte un dato que suene creíble.
- Ten en cuenta que lo que oyes viene de un sistema de reconocimiento de voz y \
puede llegarte con errores. Si algo no tiene sentido, pide que te lo repitan en \
lugar de adivinar.
"""

# El saludo es texto fijo, no una instrucción al modelo, y eso es deliberado.
#
# La primera versión lo pedía con un mensaje de sistema ("Saluda brevemente...")
# añadido al contexto. Funcionaba una vez y luego causaba un bucle: ese mensaje
# se quedaba en el historial para siempre, así que **cada** vez que el modelo
# volvía a ejecutarse —por ejemplo tras una interrupción que no dejó
# transcripción— volvía a leer la instrucción y volvía a saludar.
#
# Un saludo fijo evita el bucle, no gasta una llamada al LLM y suena al
# instante en lugar de esperar al primer token.
SALUDO_INICIAL = (
    "Hola, estoy listo para ayudarte. Puedo consultar mi base de conocimiento sobre esta placa."
)


# Muletillas que se reproducen en los huecos de espera. Ver fillers.py.
#
# Criterios al redactarlas: cortas —una muletilla larga tapa el hueco pero
# retrasa la respuesta de verdad—, neutras respecto al contenido, porque suenan
# antes de saber qué se va a contestar, y variadas, porque oír siempre la misma
# delata al instante que están enlatadas.
MULETILLAS: dict[str, list[str]] = {
    # Suenan al empezar una consulta a la base de conocimiento, donde la espera
    # está garantizada. Pueden permitirse mencionar que se está consultando.
    "consulta": [
        "Déjame consultarlo.",
        "Dame un segundo, que lo miro.",
        "Voy a buscarlo, un momento.",
        "Un momento, que lo compruebo.",
    ],
    # Suenan cuando el modelo tarda en arrancar. No pueden dar por hecho que se
    # esté consultando nada, porque puede tratarse de una respuesta normal.
    "pensando": [
        "A ver...",
        "Un momento.",
        "Déjame pensar.",
        "Espera un segundo.",
    ],
}
