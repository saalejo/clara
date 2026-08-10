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

## El número se congela al crear la tarea

Una tarea de llamada guarda el número, no el nombre: a la hora del disparo no
hay nadie delante con quien desambiguar un "llama a Luis". El formulario busca
en la agenda del móvil (a través del socket del puente) y copia el número
elegido. Si el contacto cambia de número, hay que volver a buscarlo en la
tarea.

## Cuestionarios y resultados

Con **guardar respuestas** activado, la misión le pide al modelo que al
terminar use la herramienta `guardar_respuestas`. Cada ejecución deja su
fichero en `data/tareas/resultados/<id-tarea>/`, y el planificador anota cada
disparo en `data/tareas/bitacora.jsonl` (`hablado`, `llamada_contestada`,
`sin_respuesta`, `sin_sala`, `error`). La página **Resultados** de cada tarea
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

## Dónde está cada cosa

| Pieza | Fichero |
|---|---|
| Parser cron (compartido panel/agente) | `packages/core/src/voice_agent_core/cron.py` |
| Modelos del contrato `tareas.json` | `packages/core/src/voice_agent_core/tareas.py` |
| Planificador, misiones y correlación SCO | `src/voice_agent/tareas_programadas.py` |
| Prompt de llamada de misión | `src/voice_agent/telefonia_llamada.py` |
| Herramienta `guardar_respuestas` | `src/voice_agent/tools/tareas.py` |
| Panel: modelo, formulario, vistas | `packages/panel/src/voice_agent_panel/` |
| Búsqueda de agenda desde el panel | `packages/panel/src/voice_agent_panel/agenda.py` |
