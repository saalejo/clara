# Tareas programadas

Una tarea programada es **una misión con horario**: un prompt que el agente
ejecuta solo, sin que nadie le hable. A la hora que marca su expresión cron,
el agente anuncia algo en la sala, hace un cuestionario, o llama por teléfono
a alguien con un encargo — y si la misión lo pide, guarda las respuestas donde
el panel las enseña.

Se gestionan desde el panel, en **Tareas**. A diferencia del resto de la
configuración, **no pasan por el botón de desplegar**: cada alta, edición o
borrado se exporta al momento a `data/config/tareas.json` y el agente lo
recoge en caliente, sin reiniciarse. Una tarea recién creada nace apagada,
como los hooks: se estrena cuando alguien la habilita.

## Los dos tipos

**Sala** — a la hora del disparo, el planificador mete en el pipeline de la
sala un mensaje de sistema con la misión y dispara un turno: el modelo habla
por el altavoz por iniciativa propia, con sus herramientas montadas. Si no hay
tarjeta de sonido o hay una llamada en curso, la misión se aplaza hasta 10
minutos; si no se despeja, se anota como `sin_sala` y no se recupera.

**Llamada** — el planificador marca el número congelado de la tarea por el
puente de telefonía. Si contestan, el pipeline de la llamada arranca con un
prompt de misión ("acabas de llamar a X, tu encargo es...") en lugar del guion
de contestador. Si nadie contesta en 60 segundos, cuelga y lo anota como
`sin_respuesta`. Dos límites heredados de la telefonía: solo llamadas de
operador (nada de WhatsApp; ver `docs/telefonia.md`), y gastan minutos del
plan del móvil.

**Quién habla primero.** En una llamada normal el agente es quien descuelga,
así que saluda él, con un texto fijo (`telefonia_saludo_llamada`): quien llama
espera oír algo ya. En una llamada de MISIÓN es al revés — el agente es quien
llamó, y el convenio telefónico manda que hable primero quien contesta
("¿Aló?"). Por eso el saludo automático se omite en misiones: el agente se
queda callado hasta que STT transcribe lo que diga la persona, y ahí se
dispara el turno del modelo, que ya trae la instrucción de presentarse
enseguida. Si esto vuelve a fallar en una llamada real —el agente hablando
antes de que la persona diga nada—, mirar `telefonia_llamada.py:_conversar`
(el `if mision is None:` que guarda el saludo automático).

## La expresión cron

Cinco campos: `minuto hora día-del-mes mes día-de-la-semana`, con `*`, listas
(`8,20`), rangos (`1-5`), pasos (`*/15`) y domingo como 0 o 7.

```
0 8 * * 1-5      laborables a las 8:00
*/30 9-21 * * *  cada media hora, de 9 a 21
0 17 * * 0       domingos a las 5 de la tarde
0 9 1 * *        el día 1 de cada mes
```

El formulario valida al guardar con el mismo parser que usa el agente
(`voice_agent_core/cron.py`) y enseña las tres próximas ejecuciones: si la
vista previa no dice lo que esperabas, la expresión no dice lo que creías.

**La hora es la de Colombia.** Las imágenes de contenedor vienen en UTC, así
que las unidades Quadlet fijan `TZ=America/Bogota` para el agente (que
dispara) y para el panel (que enseña la vista previa). Si se cambia de zona,
hay que cambiarla en los dos sitios o las horas mienten.

## Los disparos perdidos se pierden

Si el agente estaba apagado a la hora de una tarea, esa ejecución **no se
recupera** al arrancar. Es deliberado: una misión vieja sonando a deshoras —o
una llamada de madrugada porque la placa estuvo sin corriente— es peor que una
ejecución perdida. El planificador calcula siempre el próximo disparo
estrictamente en el futuro.

La misma regla vale para las misiones puntuales de la sección siguiente, con
un mecanismo distinto: al arrancar, `AlmacenMisiones.caducar_vencidas()` marca
`caducada` todo lo que quedó atrás, y durante la marcha `_disparar_puntuales`
hace lo propio con lo que se pasó de hora por más de dos ticks. Ese margen de
dos ticks **no es una ventana de gracia**, es la resolución del propio
planificador: mira el reloj cada treinta segundos, así que siempre ve las
misiones con algo de retraso, y sin ese margen no sonaría ninguna jamás.

Hay un caso que conviene conocer porque parece un fallo y no lo es: **una
llamada en curso bloquea el tick entero**, porque el planificador se queda
sondeándola hasta que muere. Si en mitad de una llamada de ocho minutos
alguien pide "llámame en cinco", esa misión vence sin que nadie la mire y se
anota `caducada`. La antelación mínima de dos minutos que exige
`programar_llamada` evita el caso trivial; el resto se ve en la bitácora, no
se pierde en silencio.

## Misiones puntuales: las que se inventa el agente

Además de las tareas de arriba, el planificador lleva un segundo calendario:
**misiones puntuales**, con un momento absoluto en vez de un cron, que suenan
una vez y se acabó. No las crea nadie desde el panel. Nacen de dos sitios:

- **De una conversación.** El agente tiene cuatro herramientas de agenda
  (`programar_llamada`, `editar_llamada_programada`,
  `cancelar_llamada_programada` y `llamadas_programadas`), así que cuando el
  paciente dice "ahora no puedo, llámame mañana a las cinco", la promesa queda
  apuntada en vez de perderse. Dentro de una llamada, el número por defecto es
  el de quien está al teléfono: el modelo no tiene que saberse ninguno.
- **De un reintento**, que se explica más abajo.

**El fichero lo escribe el agente, y solo él.** Vive en
`data/config/misiones_agente.json` y es la otra mitad de la doctrina de
`rutas.py`: `tareas.json` va panel → agente, y este va agente → panel. Por eso
el panel las **lista pero no las edita**, y su botón dice *Cancelar* y no
*Borrar*: lo que hace es dejar el id apuntado en un segundo fichero
(`misiones_canceladas.json`, panel → agente) que el planificador consulta por
mtime en su siguiente vuelta. Si el panel escribiera directamente en el fichero
del agente, pisaría lo que este acabara de apuntar en mitad de una
conversación.

Los ids llevan el prefijo `agenda-` porque comparten con las tareas la carpeta
de resultados; el formulario del panel rechaza los nombres que empiecen así,
para cerrar la colisión por los dos lados.

**Dónde NO están montadas estas herramientas**: en el pipeline del navegador ni
en el arnés de calidad. Programar una llamada es marcar un número arbitrario
desde la placa, aunque sea en diferido, y quien entra por el enlace de la
interfaz de llamada es un desconocido. Ver `docs/seguridad.md`.

## Reintentos

Cuando una llamada de misión no cuaja, el planificador agenda otro intento él
solo, como una misión puntual más. Dos ajustes lo gobiernan, ambos en la
sección *Tareas programadas* del panel:

| Ajuste | Por defecto | Qué hace |
|---|---|---|
| `TAREAS_REINTENTOS_MAX` | 2 | Veces que se marca **en total**. Con 2, si nadie contesta se intenta una vez más. |
| `TAREAS_REINTENTO_ESPERA_MIN` | 30 | Minutos entre un intento y el siguiente. |

Solo reintentan los desenlaces `sin_respuesta` y `error`. **Si contestaron y la
conversación quedó a medias no se vuelve a llamar**: eso es insistirle a alguien
que ya cogió el teléfono. Y con el puente caído tampoco, porque volver a
marcar contra un puente que no está no lo levanta.

Un reintento guarda sus respuestas y se anota en la bitácora bajo el id de la
**tarea original**, no bajo el suyo. Es lo que hace que la página *Resultados*
de esa tarea enseñe también lo que se consiguió al segundo intento; el id real
va aparte, en el campo `id_mision`.

## El número se congela al crear la tarea

Una tarea de llamada guarda el número, no el nombre: a la hora del disparo no
hay nadie delante con quien desambiguar un "llama a Luis". El formulario busca
en la agenda del móvil (a través del socket del puente) y copia el número
elegido. Si el contacto cambia de número, hay que volver a buscarlo en la
tarea.

## El procedimiento arma la puerta antes del primer turno

Una tarea de llamada puede llevar **de qué se operó el paciente**. El campo
sugiere los temas indexados con un `<datalist>` —HTML nativo, el panel no carga
JavaScript— pero admite texto libre, y eso es deliberado: hay que poder
programar la llamada de alguien operado de cataratas, que es justo el caso en
el que hace falta que Clara diga que no puede ayudar.

Con ese dato, el agente consulta **solo** los protocolos de esa cirugía; y si
el corpus no la cubre, no consulta ninguno en vez de contestar con los de otra
operación. La decisión está tomada antes de que suene el teléfono y el modelo
no puede hablarla para abrirla — a diferencia de una llamada entrante, donde la
cirugía la tiene que decir el paciente. En blanco, Clara la pregunta como
siempre. El mecanismo entero está en [`rag.md`](rag.md), sección *La puerta de
cobertura*.

Lo que se resuelva queda en el resumen y en el historial del número, así que la
siguiente llamada de ese paciente arranca ya con la puerta armada aunque nadie
vuelva a escribir nada.

## Cuestionarios y resultados

Con **guardar respuestas** activado, la misión le pide al modelo que al
terminar use la herramienta `guardar_respuestas`. Cada ejecución deja su
fichero en `data/tareas/resultados/<id-tarea>/`, y el planificador anota cada
disparo en `data/tareas/bitacora.jsonl` (`hablado`, `llamada_contestada`,
`sin_respuesta`, `sin_sala`, `error`, y para las puntuales también `caducada` y
`cancelada`). La página **Resultados** de cada tarea
enseña ambas cosas; el panel solo lee, quien escribe es el agente.

Si cuelgan a mitad de cuestionario, el resultado es el que sea: la bitácora
dirá `llamada_contestada` y habrá fichero de respuestas solo si el modelo
llegó a guardarlas.

## Límites conocidos (v1)

- El agente **no puede colgar** desde el pipeline de llamada: las
  herramientas de telefonía no están montadas ahí (es deliberado; ver
  `telefonia_llamada.py`). Se despide y espera a que cuelgue el otro lado.
- El saludo al descolgar es el general (`telefonia_saludo_llamada`), no uno
  por tarea.
- Las tareas son globales, no por perfil.
- Las misiones puntuales **no admiten cron**: son un momento y ya. Para algo
  que se repita, hay que crear una tarea desde el panel.
- El planificador ejecuta **una misión cada vez**. `MisionesLlamada` tiene un
  único hueco pendiente, y hoy basta justo por eso; si algún día se paralelizan
  las misiones, es lo primero que hay que rehacer.
- `bitacora.jsonl` no está acotada, y los reintentos la hacen crecer más
  deprisa.

## Dónde está cada cosa

| Pieza | Fichero |
|---|---|
| Parser cron (compartido panel/agente) | `packages/core/src/voice_agent_core/cron.py` |
| Modelos del contrato `tareas.json` | `packages/core/src/voice_agent_core/tareas.py` |
| Modelos de las misiones puntuales y `EncargoLlamada` | `packages/core/src/voice_agent_core/misiones.py` |
| Almacén de misiones (único escritor de su fichero) | `src/voice_agent/misiones_agente.py` |
| Las cuatro herramientas de agenda | `src/voice_agent/tools/misiones.py` |
| Planificador, misiones y correlación SCO | `src/voice_agent/tareas_programadas.py` |
| Prompt de llamada de misión | `src/voice_agent/telefonia_llamada.py` |
| Herramienta `guardar_respuestas` | `src/voice_agent/tools/tareas.py` |
| Panel: modelo, formulario, vistas | `packages/panel/src/voice_agent_panel/` |
| Búsqueda de agenda desde el panel | `packages/panel/src/voice_agent_panel/agenda.py` |
