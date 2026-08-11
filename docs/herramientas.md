# Herramientas

Una herramienta es una función de Python que el modelo puede decidir llamar. Es
lo que convierte un chatbot en un agente: en vez de responder solo con lo que
tiene memorizado, puede consultar una base de datos, leer un sensor o llamar a
una API.

## Las siete herramientas del catálogo

| Nombre | Qué hace | Por qué existe |
|---|---|---|
| `buscar_en_documentos` | Consulta la base de conocimiento (RAG) | Le da al agente conocimiento propio y actualizable sin reentrenar nada |
| `registrar_alerta` | Persiste el triaje (verde/amarillo/rojo) en cuanto se decide | El falso negativo es la falla catastrófica: la alerta no puede esperar al final de la llamada |
| `finalizar_llamada` | Guarda el resumen estructurado de la llamada | Los cinco campos de la rúbrica, más el color del triaje que copia el sistema de la alerta |
| `historial_paciente` | Consulta las llamadas anteriores del número en curso | La memoria entre llamadas: quién llamó, qué triaje se decidió y qué quedó pendiente |
| `guardar_respuestas` | Persiste las respuestas de un cuestionario de misión | Las tareas programadas tipo cuestionario necesitan dejar constancia |
| `obtener_fecha_hora` | Devuelve la fecha y hora actuales | Un LLM no tiene reloj; si le preguntas la hora, se la inventa |
| `estado_del_sistema` | Temperatura, memoria, carga y tiempo encendida | Ejemplo de herramienta que lee el mundo real, y de paso es útil |

## Y siete más, cuando hay teléfono

Si la placa tiene un móvil emparejado y el puente de telefonía en marcha, se
añaden estas. Aparecen solas: el agente sondea el puente al arrancar. Ver
[`telefonia.md`](telefonia.md).

| Nombre | Qué hace |
|---|---|
| `estado_del_telefono` | Si hay móvil conectado, cuántos contactos y si hay llamada |
| `buscar_contacto` | Busca a alguien en la agenda y devuelve su número |
| `llamar_a_contacto` | Llama a alguien de la agenda, por su nombre |
| `llamar_a_numero` | Llama a un número que le acaban de dictar |
| `contestar_llamada` | Coge la llamada que está entrando |
| `colgar_llamada` | Cuelga la actual, o rechaza la que entra |
| `marcar_tonos` | Marca dígitos DTMF durante una llamada |

Viven en una lista aparte, `HERRAMIENTAS_TELEFONIA`, y **no** en `HERRAMIENTAS`.
Es deliberado: sin puente, el catálogo que ve el modelo es exactamente el de
siempre, y los tests que fijan ese catálogo no han tenido que cambiar.

## Cómo se declaran: *direct functions*

Pipecat 1.x deduce el esquema JSON que se le manda al modelo a partir de la
**firma tipada** y del **docstring** de la función. No hay que escribir el
esquema a mano ni mantenerlo sincronizado con el código.

```python
async def buscar_en_documentos(params: FunctionCallParams, consulta: str) -> None:
    """Busca información en la base de conocimiento del agente.

    Úsala siempre que te pregunten por algo que pueda estar documentado: la
    placa NanoPi, cómo funciona este agente, su configuración...

    Args:
        consulta: La pregunta o los términos a buscar, en español y con
            palabras completas.
    """
    recursos: AppResources = params.app_resources
    contexto = recursos.retriever.buscar_como_texto(consulta)
    await params.result_callback({"resultados": contexto})
```

De ahí sale exactamente esto:

```json
{
  "name": "buscar_en_documentos",
  "description": "Busca información en la base de conocimiento del agente.\n\nÚsala siempre que...",
  "parameters": {
    "type": "object",
    "properties": {
      "consulta": {"type": "string", "description": "La pregunta o los términos a buscar..."}
    },
    "required": ["consulta"]
  }
}
```

Tres reglas que hay que respetar:

1. La función es **`async`** y su primer parámetro se llama **`params`**,
   exactamente así. Pipecat lo valida en tiempo de ejecución.
2. Los demás parámetros son los argumentos que el modelo rellenará. Anótalos con
   tipos: de ahí sale el `type` del esquema. Los que no tengan valor por defecto
   van a `required`.
3. El resultado **no se devuelve con `return`**: se entrega llamando a
   `await params.result_callback(...)`. El valor de retorno de la función se
   descarta.

## El docstring es código

Es la consecuencia menos evidente de este diseño y conviene interiorizarla: el
docstring **no es documentación para quien lee el código**, es el texto que ve el
modelo, y es lo único que tiene para decidir cuándo llamar a la herramienta.

Por eso los docstrings de este proyecto están redactados como instrucciones
dirigidas al modelo ("Úsala siempre que...", "No respondas de memoria sobre esos
temas") y no como notas para el programador. Reescribir un docstring "para que
quede más claro" puede cambiar el comportamiento del agente.

`tests/test_tools.py` comprueba que los esquemas generados siguen siendo los
esperados, precisamente para que ese cambio no pase inadvertido.

## Acceso a recursos compartidos

Las herramientas necesitan cosas caras: el buscador del RAG carga un modelo de
embeddings y abre un índice. Construir eso dentro de cada llamada sería absurdo,
y guardarlo en variables globales haría el código imposible de probar.

Pipecat lo resuelve con `app_resources`: un objeto que se le entrega al
`PipelineWorker` y que llega intacto —por referencia, no copiado— a cada llamada
dentro de `FunctionCallParams`.

```python
# bot.py
recursos = AppResources(settings=settings, retriever=Retriever(settings))
PipelineWorker(pipeline, app_resources=recursos, ...)

# tools/knowledge.py
recursos: AppResources = params.app_resources
```

## Encender y apagar herramientas

Desde el panel se puede desactivar cualquiera. `herramientas_activas()` filtra el
registro **antes** de construir el contexto, así que el modelo ni siquiera sabe
que existe.

Se hace así, y no vetando la llamada cuando llega, porque para entonces el modelo
ya decidió usarla: negársela solo consigue confundirlo. La contrapartida es que
hay que acordarse del prompt — si sigue anunciando una herramienta apagada, el
modelo dirá tan tranquilo que la ha usado. El panel avisa cuando eso pasa.

## Herramientas de servidores MCP

Además de las locales, el agente puede exponerle al modelo las herramientas de
servidores externos que hablen **Model Context Protocol**. Se registran desde el
panel —comando para los de tipo `stdio`, o URL para `http` y `sse`— y sus
esquemas entran en el mismo contexto que los de las locales.

Dos cosas que conviene saber:

**Un servidor roto no impide arrancar.** Se configuran a mano desde un navegador,
así que equivocarse es lo normal; lo que falle se anota, se enseña en el panel y
el agente sigue con las herramientas que sí tenga. Conseguirlo tiene más miga de
la que parece: un servidor puede fallar con un `FileNotFoundError` limpio, con un
`RuntimeError` de anyio, o con un `CancelledError` que es `BaseException` y se
cuela por debajo de un `except Exception`. El comentario largo de
`src/voice_agent/mcp.py` lo explica.

**Las claves no pasan por el panel.** En el entorno y las cabeceras se admite
`${VARIABLE}`, y quien la resuelve es el agente contra su propio entorno. En la
base de datos del panel queda escrito el literal `${MI_CLAVE}`.

Ver [`panel.md`](panel.md) para el detalle.

## Herramientas que actúan sobre el mundo

Las tres primeras solo leen. Lo peor que puede pasar si el modelo se equivoca
con `obtener_fecha_hora` es que diga una hora mal. Marcar un número es otra
cosa: no se puede deshacer y al otro lado descuelga una persona.

De ahí tres reglas que conviene aplicar a cualquier herramienta futura que
cambie algo de verdad —mandar un mensaje, encender un aparato, pagar—:

**1. El pestillo `confirmado`.** Las herramientas que marcan llevan un argumento
booleano **obligatorio**. La primera pasada no marca: devuelve a quién y con qué
número, para que el agente lo diga en voz alta y espere el sí.

```python
async def llamar_a_contacto(params, nombre: str, confirmado: bool) -> None:
    """...
    Args:
        confirmado: Ponlo en `false` la primera vez. [...] Solo cuando te haya
            dicho que sí, vuelves a llamarme con `confirmado` en `true`.
    """
```

Un prompt que pide confirmar se lo salta cualquier modelo antes o después; un
argumento del esquema, no. Y además **es comprobable**: hay un test que verifica
que con `confirmado=False` no se llega a marcar. Que el argumento sea obligatorio
y no tenga valor por defecto es parte del truco — si fuera opcional, el modelo lo
omitiría y el valor por defecto decidiría por él.

**2. El modelo no desambigua.** Si hay dos "Ana", la herramienta devuelve
`estado: "ambiguo"` con la pregunta **ya redactada** y no marca, ni siquiera con
`confirmado=True`. Elegir entre dos personas no es una decisión de conversación.
La pregunta viene escrita desde el puente para que distinga por apellido y no
por número: una lista de nueve dígitos leída en voz alta no distingue a nadie.

**3. El modelo nunca ve el catálogo entero.** Solo resultados de búsqueda. Es lo
que impide que "recuerde" un número de tres turnos atrás y lo marque.

Y una regla de forma que ya se aplicaba pero aquí es crítica: **ninguna
herramienta deja escapar una excepción**. Un fallo es un dato que el modelo tiene
que poder contarle a la persona (`{"error": ..., "sugerencia": ...}`), no algo
que rompa el turno.

## Añadir una herramienta nueva

**1. Crea el módulo** en `src/voice_agent/tools/`:

```python
# src/voice_agent/tools/clima.py
from loguru import logger
from pipecat.services.llm_service import FunctionCallParams


async def consultar_el_tiempo(params: FunctionCallParams, ciudad: str) -> None:
    """Consulta la previsión meteorológica de una ciudad.

    Úsala cuando pregunten por el tiempo, la temperatura o si va a llover.

    Args:
        ciudad: Nombre de la ciudad, en español y sin abreviar.
    """
    logger.info(f"[herramienta] consultar_el_tiempo('{ciudad}')")
    # ... aquí la llamada a la API que corresponda ...
    await params.result_callback({"temperatura_celsius": 24, "estado": "despejado"})
```

**2. Regístrala** en `src/voice_agent/tools/__init__.py`:

```python
from voice_agent.tools.clima import consultar_el_tiempo

HERRAMIENTAS: list[DirectFunction] = [
    buscar_en_documentos,
    obtener_fecha_hora,
    estado_del_sistema,
    consultar_el_tiempo,  # <- nueva
]
```

Y ya está. `bot.py` pasa `HERRAMIENTAS` al `LLMContext`, que registra los
manejadores automáticamente: no hace falta llamar a `llm.register_function`.

**3. Menciónala en el prompt** (`prompts.py`) si quieres guiar cuándo usarla. El
docstring suele bastar, pero para comportamientos sutiles ("no anuncies que vas
a consultarla") el prompt del sistema es el sitio.

**4. Escribe el test.** Las herramientas se prueban sin red ni modelos, con un
doble de `FunctionCallParams`; mira `tests/test_tools.py`.

## Consejos para herramientas en un agente de voz

- **Devuelve datos, no prosa.** Un diccionario con campos claros. El modelo ya
  se encarga de redactarlo para que suene natural; si le das una frase hecha, la
  repetirá tal cual y sonará a robot.
- **Devuelve también una versión ya redactada cuando el formato importe.**
  `obtener_fecha_hora` devuelve `descripcion` en español además de los campos
  ISO, para que el modelo no traduzca "Monday" y se equivoque.
- **Sé rápido.** Mientras la herramienta corre, el agente calla. Más de un
  segundo se nota mucho en una conversación hablada.
- **Que un fallo sea un dato, no una excepción.** Si la herramienta no puede
  hacer su trabajo, devuelve un resultado que lo explique. Una excepción deja al
  modelo sin nada que decir.
- **Si la operación es lenta de verdad**, mira `@tool_options(cancel_on_interruption=False)`:
  el modelo sigue conversando y el resultado se inyecta después.
