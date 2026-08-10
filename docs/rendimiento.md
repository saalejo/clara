# Rendimiento

Todas las cifras de este documento están **medidas en la propia NanoPi R4S**, no
estimadas. Se indica cómo reproducir cada medida.

## El hardware que hay debajo

| | |
|---|---|
| SoC | Rockchip RK3399 (aarch64) |
| Núcleos rápidos | 2 × Cortex-A72 @ 1.80 GHz (`cpu4`, `cpu5`) |
| Núcleos eficientes | 4 × Cortex-A53 @ 1.416 GHz (`cpu0`–`cpu3`) |
| RAM | 3.8 GB |
| GPU para inferencia | Ninguna utilizable — la Mali-T860 no sirve para ONNX ni CTranslate2 |
| Temperatura en reposo | ~33 °C, disipación pasiva |

Todo lo que corre en local lo hace en la CPU, en coma entera de 8 bits.

## Presupuesto de latencia

Desde que dejas de hablar hasta que empiezas a oír la respuesta:

| Etapa | `STT_BACKEND=whisper` | `STT_BACKEND=deepgram` | Dónde se ajusta |
|---|---|---|---|
| VAD confirma el silencio | 0.2 s | 0.2 s | `VAD_STOP_SECS` |
| Cierre del turno del usuario | 0.6 s | 0.6 s | `USER_SPEECH_TIMEOUT` |
| Transcripción | ~3.0 s | **~0.7 s** | `STT_BACKEND` |
| Primer token del LLM | 0.7–1.0 s | 0.7–1.0 s | `OPENROUTER_MODEL` |
| Primer fragmento de audio de Piper | ~0.3 s | ~0.3 s | `TTS_VOICE` |
| **Total hasta oír algo** | **≈ 4.8 s** | **≈ 2.5 s** | |

Con Whisper local **la transcripción se lleva más de la mitad del total**. Es la
consecuencia directa de hacer reconocimiento de voz en una CPU ARM sin
acelerador, y no hay ajuste que lo arregle.

Medido en una conversación completa con Deepgram, incluyendo una consulta al
RAG por herramienta: **1.6 segundos** desde que el VAD detecta el final del
habla hasta que el agente empieza a generar la respuesta.

Las cifras reales de cada ejecución están en el log: el pipeline arranca con
`enable_metrics=True` y cada servicio informa de su tiempo hasta el primer byte
(TTFB) y de su tiempo de proceso.

## Latencia percibida: las muletillas

Los números de arriba son la latencia **real**. La percibida es otra cosa, y se
puede mejorar sin tocar ni un milisegundo de la real.

El problema del silencio de espera no es que dure dos segundos: es que no
informa. Quien pregunta no sabe si le han oído, si el agente está pensando o si
se ha colgado, y lo natural es repetir la pregunta —lo que además interrumpe la
respuesta que venía en camino.

`src/voice_agent/fillers.py` reproduce frases cortas pregrabadas en esos huecos.
Se sintetizan con Piper **una sola vez**, se cachean en `data/fillers` como PCM
en crudo a la frecuencia del pipeline, y a partir de ahí reproducirlas es copiar
bytes: instantáneo. Sintetizarlas al vuelo no serviría, porque Piper tarda entre
tres y ocho décimas en soltar el primer fragmento, que es justo el hueco a tapar.

Hay dos disparadores, con criterios deliberadamente distintos:

| Disparador | Cuándo suena | Por qué |
|---|---|---|
| Llamada a herramienta | De inmediato | Consultar el RAG son segundos garantizados: no hay riesgo de pisar una respuesta rápida |
| El modelo tarda | Tras `FILLER_DELAY_SECS` | Muchas respuestas arrancan antes; una muletilla innecesaria molesta más de lo que ayuda |

Medido en conversación real con `FILLER_DELAY_SECS=1.2`:

```
19:40:13.519  el modelo decide consultar el RAG
19:40:13.601  «Déjame consultarlo»            <- 82 ms después
19:40:15.212  empieza la respuesta real       <- 1.6 s más tarde
```

El umbral de 1.2 s no es arbitrario. Con 0.8 s, la muletilla genérica («a
ver...») se adelantaba a la de consulta («déjame consultarlo»), que es bastante
más informativa, y encima la bloqueaba por el intervalo mínimo. A 1.2 s las
respuestas rápidas —que arrancan hacia el segundo— no llevan muletilla, porque
no les hace falta, y las consultas llevan la suya.

### Qué pasa al cambiar de voz

La caché se invalida sola, porque el nombre de cada fichero incluye un resumen
del **texto**, la **voz** y la **frecuencia de muestreo**. Cambiar cualquiera de
las tres produce rutas distintas, así que las muletillas afectadas se
resintetizan en el siguiente arranque. Medido al pasar de `es_ES-davefx-medium` a
`es_MX-claude-high`:

| | Arranque | Qué hace |
|---|---|---|
| Voz nueva, sin descargar | 40 s | Descarga la voz y sintetiza las 8 muletillas |
| Volver a una voz ya usada | 27 s | Nada: las sirve de caché |

Son unos seis segundos de más, **una sola vez**. Los ficheros de las voces
anteriores no se borran, así que alternar entre voces sale gratis a partir de la
segunda vez.

El orden de construcción importa y no es casual: el banco de muletillas se crea
**después** del servicio de síntesis, que es quien descarga la voz si falta.
Construirlo antes hacía que cambiar a una voz no descargada matara el arranque
con un `No such file or directory` antes de que nadie la bajase. El banco sabe
descargarla por su cuenta de todos modos, para no depender de ese orden.

## Whisper local frente a Deepgram

Misma batería de siete frases sintetizadas con Piper, con medio segundo de
silencio al principio para imitar el pre-roll que deja el VAD en una
conversación real. La métrica es la tasa de error por palabra (WER), calculada
sobre distancia de edición ignorando acentos y puntuación:

| | Frases perfectas | Error por palabra | Latencia media |
|---|---|---|---|
| Whisper `tiny`, local | 3 de 7 | **33.1 %** | 3.60 s |
| Deepgram `nova-3`, nube | **7 de 7** | **0.0 %** | **0.61 s** |

La diferencia no es de grado, es de naturaleza. Ejemplos del mismo audio:

| Dicho | Whisper `tiny` | Deepgram `nova-3` |
|---|---|---|
| ¿Cuántos núcleos tiene esta placa? | «¡Honso nucleos que en esta placa!» | «¿Cuántos núcleos tiene esta placa?» |
| ¿Qué hora es? | «¡Qué olas!» | «¿Qué hora es?» |
| ¿Cuánta memoria RAM queda libre? | «cuantamemoría real y me queda libre» | «¿Cuánta memoria RAM queda libre?» |

Los errores de Whisper no son pequeñas imprecisiones: producen frases sin
sentido que el modelo de lenguaje no puede interpretar, y lo correcto —lo que
hace, porque se lo pide el prompt— es pedir que se lo repitan.

**Cuándo usar cada uno.** Whisper si te importa no depender de la red, no pagar
por minuto, o que el audio no salga de la placa. Deepgram si te importa que el
agente entienda lo que le dices.

### Un detalle al cambiar de backend

Pipecat calibra las latencias P99 de sus servicios de STT **suponiendo
`VAD_STOP_SECS=0.2`**. Si lo subes, la red de seguridad de la estrategia de fin
de turno se colapsa a cero y avisa en cada intervención:

```
VAD stop_secs (0.4s) >= STT p99 latency (0.35s). STT wait timeout collapsed to 0s
```

Por eso el perfil `headset` usa 0.2 s, y por eso el proyecto declara
explícitamente `DEEPGRAM_TTFS_P99=0.9` —medido desde esta placa, 0.61 s de
media— en lugar del 0.35 s que trae Pipecat: así el perfil `speaker`, que
necesita un `stop_secs` de 0.6 s por el eco, tampoco dispara el aviso.

## Whisper: elección de modelo

Medido con frases en español sintetizadas con Piper, `compute_type=int8`,
`beam_size=1`:

| Modelo | Tiempo por intervención | Veredicto |
|---|---|---|
| `tiny` | **2.9 s** | El valor por defecto |
| `base` | 5.1 s | El doble de lento y no transcribió mejor |
| `small` | 16.6 s | Inutilizable para conversar |

`tiny` es el doble de rápido que `base` y en estas pruebas no salió perdiendo en
calidad. Es un resultado poco intuitivo: no des por hecho que un modelo mayor
transcribe mejor sin medirlo con tu audio y tu idioma.

### La longitud de la intervención importa mucho más que el modelo

Este es el hallazgo relevante, y va en contra de lo que uno esperaría:

| Intervención | Duración | `tiny` | `base` | `small` |
|---|---|---|---|---|
| "Necesito consultar el estado de la placa y la temperatura del procesador." | 4.2 s | correcta | correcta | correcta |
| "¿Me puedes decir cuántos núcleos tiene el procesador de esta placa, por favor?" | 3.5 s | correcta | casi | correcta |
| "Hola, buenas tardes." | 1.3 s | correcta | correcta | correcta |
| "¿A qué temperatura está el procesador?" | 1.8 s | **falla** | **falla** | **falla** |
| "¿Cuántos núcleos tiene esta placa?" | 1.6 s | **falla** | **falla** | **falla** |
| "¿Qué hora es?" | 0.9 s | **falla** | **falla** | correcta |

Las frases largas se transcriben bien; las preguntas cortas se degradan, y
**subir de modelo no lo arregla** (`small`, catorce veces más lento que `tiny`,
falla en las mismas). El error típico es que se come o deforma las primeras
palabras: "¿Cuántos núcleos tiene esta placa?" sale como "vamos a los núcleos
que en esta placa".

Se descartaron dos hipótesis, midiéndolas:

- **El remuestreo del audio de prueba.** Rehecho con `soxr` en calidad VHQ en
  lugar de una interpolación lineal sin filtro antialias: mismo resultado.
- **El `initial_prompt` de faster-whisper**, sesgando el decodificador con el
  vocabulario del dominio: mismo resultado (2 de 7 aciertos exactos en ambos
  casos).

La causa es de Whisper: siempre procesa ventanas de 30 segundos y con menos de
dos segundos de habla apenas tiene contexto con el que desambiguar.

### Advertencia sobre la validez de estas cifras

**Estas pruebas usan voz sintetizada con Piper, no voz humana.** Es la única
forma de automatizarlas, pero no es un sustituto justo: la salida de Piper es
más rápida y más plana que el habla real, que es la distribución con la que
Whisper se entrenó. Es perfectamente posible que la precisión con una persona
hablando a ritmo normal sea bastante mejor que la de esta tabla.

Lo que sí se puede afirmar de estas medidas es lo relativo —los tiempos de cada
modelo, y que el problema está en la brevedad y no en el tamaño del modelo—, no
lo absoluto. **La precisión real hay que medirla hablando.** Si las preguntas
cortas te fallan, la solución no es subir de modelo: es `STT_BACKEND=deepgram`.

### Un detalle importante: el coste no depende de lo que hables

| Duración del audio | Tiempo de `tiny` |
|---|---|
| 2.79 s | 3.19 s |
| 3.94 s | 3.20 s |
| 4.26 s | 3.25 s |

Whisper siempre procesa ventanas de **30 segundos**: rellena con silencio lo que
falte. Por eso decir "sí" cuesta lo mismo que decir una frase larga, y por eso
no sirve de nada intentar acortar las intervenciones para ganar velocidad.

### Anchura del haz de búsqueda

| `beam_size` | Tiempo (`tiny`) |
|---|---|
| 1 (voraz) | 3.2 s |
| 5 (valor por defecto de faster-whisper) | 3.6 s |

Un 12 % más caro sin ninguna mejora observable en frases cortas. Por eso el
proyecto fija `beam_size=1`, sobrescribiendo el valor por defecto de la
librería.

## Hilos: la sorpresa del big.LITTLE

La intuición dice que en un big.LITTLE conviene usar solo los núcleos rápidos y
fijar el proceso a ellos con `taskset`, para que el reparto no quede limitado
por los A53 lentos. **Lo medimos y resultó ser falso.**

| Configuración | Tiempo (`tiny`) |
|---|---|
| 1 hilo | 5.20 s |
| 2 hilos | 3.21 s |
| 4 hilos | 3.00 s |
| 6 hilos | **2.70 s** |
| 2 hilos fijados a los A72 (`taskset -c 4,5`) | 3.18 s |
| 4 hilos fijados a los A53 (`taskset -c 0-3`) | 4.44 s |

Dos conclusiones:

- **Fijar a los A72 no aporta nada**: 3.18 s frente a 3.21 s sin fijar. El
  planificador de Linux ya coloca los hilos activos en los núcleos rápidos por
  su cuenta. La orden `taskset` solo sirve para empeorar las cosas si te
  equivocas de núcleos.
- **Más hilos siempre ayudó**, incluso incluyendo los A53. Los núcleos lentos
  suman en vez de frenar.

El valor por defecto es **4 hilos**, no 6, a pesar de que 6 sea 0.3 s más
rápido: hay que dejar núcleos libres para el hilo de audio en tiempo real, el
VAD y el bucle de eventos. Ahorrar 0.3 segundos no compensa arriesgar cortes en
la reproducción.

## Piper

| Frase | Audio generado | Tiempo | Factor |
|---|---|---|---|
| 1 | 2.79 s | 1.10 s | 0.39× |
| 2 | 4.26 s | 1.58 s | 0.37× |
| 3 | 3.94 s | 1.45 s | 0.37× |

Piper genera audio unas **2.6 veces más rápido que el tiempo real**, y como
Pipecat lo reproduce en cuanto sale el primer fragmento, la espera percibida es
de unas décimas. No es un cuello de botella.

Las voces `high` (`es_MX-claude-high`, `es_AR-daniela-high`) suenan mejor pero
consumen bastante más CPU; en esta placa conviene quedarse en `medium`.

## Arranque

| Fase | Tiempo |
|---|---|
| Importar ChromaDB | ~15 s |
| Importar el transporte de audio de Pipecat | ~7 s |
| Cargar Whisper `tiny` | ~4 s |
| Cargar la voz de Piper | ~9 s |
| Cargar el modelo de embeddings | ~3 s |
| **Total hasta el saludo** | **~40 s** |

Es lento, pero se paga una sola vez. Por eso la unidad de systemd usa
`TimeoutStartSec=300`: con el valor por defecto, systemd daría el arranque por
fallido.

La descarga inicial de modelos (`make models`) tarda unos 80 s y solo ocurre la
primera vez, porque todo queda cacheado en `data/models`, que es un volumen.

## Cómo reproducir estas medidas

Latencia real de una conversación, servicio por servicio:

```bash
LOG_LEVEL=DEBUG make run
```

y busca en el log las líneas de métricas (`TTFBMetricsData`,
`ProcessingMetricsData`) de cada servicio.

Comparar modelos o número de hilos sin hablar por el micrófono: sintetiza una
frase con Piper y pásasela a Whisper, que es exactamente lo que se hizo para
esta tabla. El guion cabe en veinte líneas y está descrito en `docs/rag.md`
para el caso análogo del RAG.
