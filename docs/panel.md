# Panel de control

Una web para configurar y gobernar el agente sin editar código ni entrar por
SSH: el prompt del sistema, el alma, los ajustes, qué herramientas ve el modelo,
qué servidores MCP se conectan, qué hooks se disparan, y el arranque y parada del
propio servicio.

```
ssh -L 8080:localhost:8080 nanopi
# y luego, en el navegador: http://localhost:8080/panel/
```

No se publica en la red a propósito. Quien entre en el panel puede ejecutar
comandos en la placa; ver [Seguridad](#seguridad).

---

## Cómo llega la configuración al agente

Panel y agente son **dos contenedores distintos** y no se hablan por red. Se
comunican dejando ficheros en el volumen de datos que los dos montan:

```
  panel  ── escribe ──►  <DATA_DIR>/config/settings.json        campos de Settings
                         <DATA_DIR>/config/runtime.json         perfil, prompt, alma, tools, mcp, hooks
  agente ── escribe ──►  <DATA_DIR>/config/estado_arranque.json  lo que cargó de verdad
                         <DATA_DIR>/logs/agente.log              el log en vivo
```

**Guardar en el panel no cambia nada por sí solo.** El ciclo completo es
`guardar → exportar → reiniciar`, y el botón *Desplegar y reiniciar* de la
portada lo hace de una vez. El agente lee su configuración una sola vez, al
arrancar.

Que no haya recarga en caliente es deliberado: cambiar el prompt a mitad de una
conversación obligaría a invalidar el historial y el banco de muletillas
pregrabadas en pleno turno. Reiniciar cuesta unos veinte segundos.

### El orden de prioridad, que es lo que hace que el panel sirva de algo

```
kwargs explícitos  >  instantánea del panel  >  entorno  >  .env  >  secrets
```

La instantánea va **por encima del entorno**, y no es un detalle de estilo: la
unidad de systemd inyecta el `.env` del proyecto como variables de entorno
reales dentro del contenedor. Colocarla por debajo dejaría el panel sin efecto
sobre las dos docenas de ajustes que ese fichero define — se guardaría, se
exportaría, se reiniciaría, y no cambiaría nada.

### Lo que el panel no puede tocar

```python
CAMPOS_PROTEGIDOS = {"data_dir", "corpus_dir", "openrouter_api_key", "deepgram_api_key"}
```

`data_dir` y `corpus_dir` son rutas *dentro del contenedor* y no coinciden con
las del anfitrión; dejarlas configurables resucitaría la trampa que ya costó una
sesión. Las dos claves de API no pasan por aquí en absoluto: viven en el `.env`
del agente, que el contenedor del panel **ni siquiera monta**.

---

## Las páginas

### Perfiles

Un perfil es **un juego completo de configuración**: su historial de prompt y
alma, sus ajustes, sus interruptores de herramientas y su selección de
servidores MCP y hooks. Solo uno está **activo**, y es el único que se exporta:
el agente no sabe qué es un perfil, en `runtime.json` solo viaja su nombre para
el banner de arranque.

Activar un perfil **no despliega**. Sigue mandando el ciclo de siempre
—`guardar → exportar → reiniciar`— y la portada avisa cuando el perfil activo no
es el del último despliegue.

Aparte del activo está el perfil **en edición**, que es el que enseñan las
páginas de prompt, ajustes, herramientas, MCP y hooks (se elige en Perfiles y se
guarda en la sesión). Son cosas distintas a propósito: así se puede dejar listo
un perfil entero antes de activarlo. Cada página lleva una franja que dice qué
perfil se está editando, con aviso si no es el activo.

Las definiciones de servidores MCP y hooks son **comunes a todos los perfiles**;
lo que es de cada perfil es cuáles usa. Las herramientas y los ajustes, en
cambio, se guardan por perfil. Duplicar copia todo; borrar arrastra lo suyo, y
ni el perfil activo ni el último se pueden borrar.

### Prompt y alma

El **prompt del sistema** son las reglas de funcionamiento: cómo hablar, cómo
usar las herramientas, qué hacer cuando no se sabe algo. El **alma** es la
personalidad de esta instalación concreta, y se añade tal cual al final del
prompt. Están separados para poder cambiar el carácter del agente sin tocar sus
reglas, y al revés.

Guardar **crea una versión nueva y la activa**. El historial no se reescribe
jamás: volver a una versión anterior también crea una versión, copiándola. Así
la secuencia de lo que estuvo puesto en cada momento se conserva entera, que es
justo lo que uno quiere consultar cuando el agente empieza a comportarse raro.

Las muletillas se editan como texto, con las categorías como encabezado:

```
consulta:
  Déjame consultarlo.
  Dame un segundo, que lo miro.
pensando:
  A ver...
```

### Ajustes

Los campos, sus rangos, sus opciones y sus textos de ayuda **se leen de la clase
`Settings` del agente**, no de una lista escrita a mano. El panel hereda así la
documentación de `config.py` —con sus cifras medidas en la placa— y no puede
quedarse desfasado. Un test comprueba que todo campo de `Settings` acaba en el
formulario o está explícitamente protegido.

Dejar un campo **en blanco significa «no lo fijes»**: se borra la fila y vuelve a
mandar el `.env`. No es lo mismo que escribir el valor por defecto.

### Herramientas

Lo que se enseña aquí es lo que el agente tenía cargado **en su último
arranque**, leído de `estado_arranque.json`, no lo que dice la base de datos del
panel. Cuando ambas cosas no coinciden —que es justo cuando uno abre esta
página— esa diferencia es el diagnóstico: falta desplegar.

Desactivar una herramienta hace que el modelo **ni siquiera sepa que existe**.
Se hace así, y no vetando la llamada más tarde, porque cuando el modelo ya ha
decidido llamarla es tarde: negársela entonces solo consigue confundirlo.

> **Aviso que da el panel:** si desactivas `buscar_en_documentos` pero el prompt
> sigue diciendo «tienes una herramienta para consultar tu base de
> conocimiento», el modelo afirmará tan tranquilo que la ha consultado. La
> portada lo detecta y lo avisa.

### Conocimiento

Los **temas** y los **documentos** del RAG. Un tema es una subcarpeta de
`corpus/` —y una colección de ChromaDB, ver [rag.md](rag.md)—; los documentos
sueltos en la raíz aparecen agrupados bajo *Sin tema*, que se puede llenar y
vaciar pero no borrar, porque no es una carpeta.

El ciclo aquí es **subir → reindexar**, hermano del *guardar → desplegar* del
resto del panel pero **independiente de él**. Y conviene saber por qué son
distintos: los documentos no pasan por `exporter.py`. No son configuración que se
serializa a JSON, son ficheros de verdad en un volumen que los dos contenedores
montan. Darle a *Desplegar* no hace que aparezcan; hace falta *Reindexar*.

Reindexar reconcilia: añade lo nuevo, olvida lo borrado y elimina las colecciones
de los temas que ya no existen. Lo que no ha cambiado **ni se abre** —se reconoce
por su huella—, así que subir un documento cuesta lo que cuesta ese documento y
no lo que cuesta el corpus entero, y una barra en la propia página cuenta por
dónde va. **El agente lo recoge en caliente**, sin
reiniciarse — el buscador relee la lista de colecciones en cada consulta, que en
un SQLite local con un puñado de filas no cuesta nada. Es la única parte de la
configuración que no necesita un reinicio, y es deliberado: reindexar no toca ni
el prompt ni el historial ni el banco de muletillas.

Cada tema lleva además un campo **Cómo lo llama la gente**: los nombres con los
que un paciente diría esa cirugía, separados por comas. Es lo que hace que
«me sacaron la vesícula» acabe en `colecistitis`, y lo que permite que un tema
nuevo sea reconocible sin tocar código. **No entra en el índice**, así que no
hace falta reindexar ni desplegar: se guarda en `data/config/alias_temas.json` y
el agente lo relee en cada consulta, incluso a mitad de una llamada. Por qué
importa —y por qué la puerta de cobertura no tiene interruptor— está en
[rag.md](rag.md#cómo-llama-la-gente-a-cada-cirugía).

Lo que el panel **no** puede hacer es borrar una colección de ChromaDB. No tiene
chromadb ni puede tenerlo (ver [Por qué dos imágenes](#por-qué-dos-imágenes)), así
que al borrar un tema desaparece la carpeta y la colección se va en la siguiente
reindexación. Por eso el aviso de "falta reindexar" también sale al borrar.

El nombre de un tema **se slugifica al crearlo**: "Guía de la Placa" se guarda
como `guia-de-la-placa`, y el panel dice con qué nombre ha quedado. Al buscar y
al borrar no se slugifica nada — si mañana cambiaran las reglas, un nombre
"arreglado" dejaría de encontrar la carpeta que creó el de ayer.

> **Cómo sabe el panel que el índice está viejo.** Guarda, con cada
> reindexación, la fecha del último cambio del corpus tomada justo *antes* de
> lanzarla, y la compara con la de ahora. Se miran las fechas de los ficheros y
> **también las de los directorios**: borrar un documento no cambia la fecha de
> ningún fichero —ya no está— pero sí la de su carpeta. Si nunca se ha
> reindexado desde el panel no se avisa de nada: es lo normal en una instalación
> montada con `make ingest`, y un aviso que sale siempre se aprende a ignorar.

### Servidores MCP

Cada servidor aporta sus herramientas al modelo, junto a las locales. Tres
transportes:

| Transporte | Qué es | ¿Se puede sondear desde el panel? |
|---|---|---|
| `stdio` | Se lanza como proceso hijo del agente | **No**: su comando vive en la imagen del agente, no en la del panel |
| `http` | HTTP con streaming, servidor remoto | Sí |
| `sse` | Server-Sent Events, el transporte remoto anterior | Sí |

Para los de tipo `stdio` el panel enseña lo que el agente descubrió al arrancar,
con su fecha.

La definición del servidor es común a todos los perfiles; la casilla
*Habilitado* de la lista dice si **el perfil en edición** lo usa.

**Un servidor roto no puede dejar muda la placa.** Es la propiedad más
importante de toda esta parte: se configuran escribiendo un comando o una URL a
mano en un navegador, así que equivocarse es lo normal. Lo que falle se anota,
se enseña en el panel, y el agente arranca con las herramientas que sí tenga.

Las variables `${VARIABLE}` en el entorno y las cabeceras **las resuelve el
agente** contra su propio entorno. Es la forma de darle una clave de API a un
servidor MCP sin que el panel llegue a verla: en la base de datos queda escrito
`${MI_CLAVE}`.

### Hooks

Reglas que se disparan en un punto concreto de la conversación.

| Evento | Cuándo |
|---|---|
| `transcripcion_lista` | Hay transcripción y **todavía no ha entrado en el historial**. El único punto donde reescribirla o descartarla surte efecto |
| `usuario_termino` | El usuario acaba de callar |
| `respuesta_texto` | Llega un fragmento de la respuesta del modelo |
| `llamada_herramienta` / `resultado_herramienta` | El modelo pidió una herramienta, o esta contestó |
| `error` | Algo falló dentro del pipeline |

Y tres acciones: **ejecutar un comando**, **reescribir** el texto con una
expresión regular, o **vetarlo** si casa.

Reescribir y vetar solo se ofrecen en los eventos que llevan texto. Descartar un
frame de control no da un error: **cuelga el pipeline entero**, y lo único que se
ve es un «timeout waiting for…» que no señala a ningún sitio. Por eso ni siquiera
aparece como opción.

Los comandos **no esperan por defecto**. La alternativa suma su duración a *cada*
turno de la conversación, que en un agente de voz se nota al instante; por eso
`bloqueante` hay que activarlo a mano y su tiempo máximo está topado en dos
segundos. El contexto del evento llega por la entrada estándar en JSON, y el
comando **no pasa por una shell**: sin comodines, sin tuberías y sin sorpresas de
comillas.

Un hook recién creado **no pertenece a ningún perfil**, así que nace desactivado:
uno recién escrito no debe estrenarse solo en mitad de una conversación. Se
activa marcándolo en la lista, que guarda la selección del perfil en edición.

> **Limitación que conviene conocer:** filtrar la respuesta del modelo trabaja
> sobre *fragmentos*, no sobre frases. El texto llega troceado en tokens, así que
> una regla que cruce el límite de un fragmento no casará nunca. Bufferizar la
> respuesta entera costaría toda la latencia de generación.

### Evaluaciones

Es el expediente clínico del agente, y desde el 12 de agosto es **una llamada
por fila**: filtrable, navegable y con una página de detalle por llamada.

La lista es una **fusión externa completa** entre los dos sitios donde el agente
deja lo que pasó, cruzados por `id_llamada`:

| | De dónde sale | Qué aporta |
|---|---|---|
| JSON | `data/evaluaciones/{alertas,resumenes}/*.json` | síntomas, justificación, decisión, referencias, documentos consultados, cobertura |
| SQLite | `data/evaluaciones/historial.sqlite3` | número, nombre, dirección de la llamada |

**No es un `SELECT` del historial con adornos, y esa es la parte que importa.**
`numero_identificable()` rechaza el número oculto y los rellenos de las llamadas
de app, así que una llamada de navegador o de WhatsApp **no tiene fila** y solo
existe como JSON; al revés, una que se cortó antes de que corriera ninguna
herramienta tiene fila y no tiene JSON. Listar desde un solo lado perdería, en
silencio, una mitad distinta cada vez. De ahí también el tercer valor del filtro
de dirección, **«sin ficha»**: sin él, las entrantes más las misiones no sumarían
el total y la página parecería rota.

Se filtra por seis ejes que se combinan en Y, todos en la URL (`GET`, que es el
único formulario de este tipo en el panel: un filtro tiene que sobrevivir a
recargar y a compartir el enlace):

* **fecha** (`desde`/`hasta`),
* **triaje** (verde/amarillo/rojo),
* **procedimiento** (la cirugía normalizada por la puerta de cobertura),
* **cobertura** (cubierta/no_cubierta/desconocida/ambigua),
* **dirección** (entrante/misión/sin ficha),
* **paciente** (por número).

Un filtro que no se entiende —un `nivel=azul` escrito a mano— **se ignora y se
avisa**: la página sale igual. Devolver un 400 dejaría al jurado delante de una
pantalla de error de Django.

El filtro de fecha es el único que **poda antes de abrir nada**: el nombre del
fichero es `%Y%m%d-%H%M%S.json`, así que sus ocho primeros caracteres deciden si
se abre, y abrir es la mitad cara en la placa. Por eso la vista sin acotar tiene
un tope (`TOPE_FICHEROS`), lo dice al pie —cuántos ficheros abrió— y empuja a
acotar en vez de paginar. **No hay paginación a propósito:** no existe una clave
común ordenada entre el directorio y el SQLite, así que la página 3 exigiría
rehacer el mismo escaneo y la misma fusión que la 1 y tirar dos tercios; paginar
no ahorraría trabajo, lo repetiría. Lo que hay es una escalera de tamaños de
página y un «Ver más».

Medido en la placa el 12 de agosto con los datos reales (15 llamadas, 18
ficheros de evaluación): **8 ms** la fusión y 92 ms la petición HTTP entera.
El tope está en 400 ficheros por carpeta, así que el peor caso sin acotar se
queda en el orden de las décimas de segundo.

La ventana de ficheros lleva **un día de holgura a cada lado**, y no es
decorativo: el nombre del fichero lleva el instante en que se escribió el
artefacto y el momento del expediente lleva el instante en que se montó la
llamada, así que una llamada de las 23:58 deja su resumen en el fichero del día
siguiente. Sin la holgura, filtrar por ese día perdería el resumen.

Al entrar en una fila está **todo lo de esa llamada junto**: sus alertas en
orden, el resumen completo (o el aviso de que se cortó antes de
`finalizar_llamada`, o el de que es un resumen de respaldo), la ficha del
paciente —o la explicación de por qué no la hay— y la **traza documental**, que
es la prueba de trazabilidad que pide la rúbrica: qué se le preguntó al RAG y qué
devolvió de verdad, con origen, tema y distancia. Una traza vacía también se
explica: cuando la cirugía no está cubierta, `buscar_en_documentos` ni llama al
retriever, y eso es un «no lo sé» bien hecho, no un fallo.

Todo esto vive en `voice_agent_core.expediente`, no en las vistas: el cruce es
lógica de dominio y se prueba sin Django (`tests/test_expediente.py`).

### Pacientes

El **padrón** de números que han llamado: número, nombre de la agenda, cuántas
llamadas y cómo fue la última. Sale de `data/evaluaciones/historial.sqlite3` y el
panel solo lee.

Ya no repite las tarjetas de llamada —eso era lo que hacía que las dos páginas
parecieran decir lo mismo—: cada ficha enlaza a **Evaluaciones ya filtrada por
ese número**. Las llamadas de navegador, de número oculto o de aplicación no
tienen ficha aquí, pero sí aparecen allí.

### Calidad

La sección Calidad ensaya a Clara contra ataques adversarios —inyección de
prompt, paciente hostil, banderas rojas, preguntas fuera del corpus— **por texto
y sin voz**. Es la respuesta a que la evaluación del reto es una sesión en vivo
donde los jueces atacan al agente: aquí esos mismos ataques se pueden ensayar a
solas antes de la demo y medir cómo responde.

Cómo funciona: un LLM interpreta el rol del escenario (su `persona`) y conversa
contra el **mismo** prompt de sistema, RAG y herramientas que atienden los
jueces; otro LLM juzga el resultado contra los criterios del escenario. Para los
escenarios de bandera roja hay además un **chequeo determinista**: si Clara no
registró la alerta del nivel exigido, es fallo automático —el falso negativo
clínico no se deja al criterio de otro modelo—. El veredicto del juez se puede
anular a mano desde el detalle de cada ejecución.

El catálogo de escenarios vive en el código (`voice_agent_core/calidad.py`,
cuatro categorías: seguridad, paciente difícil, riesgo clínico, robustez); la
matriz de la página lo pinta agrupado y guarda el historial de ejecuciones por
escenario, con la transcripción completa de cada una.

Como el reindexado, el panel **no ejecuta nada**: deja la solicitud en
`<DATA_DIR>/calidad/solicitud.json` y arranca por D-Bus la unidad oneshot
`clara-calidad.service`, que corre en la imagen del agente porque necesita
el LLM y el RAG. Los resultados son ficheros que el runner deja en
`<DATA_DIR>/calidad/resultados/`; el panel solo los lee. Cada ensayo corre en un
`data_dir` aislado (`calidad/sandbox/`) para que sus alertas y resúmenes de
prueba **no** aparezcan en las páginas de Evaluaciones ni Pacientes.

Mide el **cerebro, no el oído**: no cubre el ruido del micrófono, las muletillas
ni las interrupciones; prueba el prompt, el RAG y las herramientas, que es lo que
el jurado ataca con palabras. Desde SSH, `make calidad` (todo el catálogo) o
`make calidad ESC="inyeccion-olvida bandera-roja"` ejecuta sin pasar por el
panel. Ojo con la cuota del nivel gratuito de Gemini: el lote completo son ~14
conversaciones y comparte cuota con el agente en vivo, así que no conviene
lanzarlo minutos antes de la demo.

### Logs y despliegues

Los logs se leen de `<DATA_DIR>/logs/agente.log`, que el agente escribe en el
volumen compartido, y se siguen en vivo con eventos del servidor. No se puede
usar el journal —no es accesible desde dentro de un contenedor sin
privilegios— ni `podman logs`, porque Quadlet añade `--rm` y el contenedor se
destruye al parar, llevándose sus logs justo cuando querrías saber por qué se
cayó.

La página de despliegues guarda **las instantáneas tal y como se enviaron**. Es
lo que contesta la pregunta que siempre acaba apareciendo: «cambié algo y no pasó
nada».

---

## Puesta en marcha

```bash
cp .env.panel.example .env.panel
chmod 600 .env.panel
python -c 'import secrets; print(secrets.token_urlsafe(50))'   # -> PANEL_SECRET_KEY
# rellena también PANEL_ADMIN_USER y PANEL_ADMIN_PASSWORD

make build-panel      # ~1,5 min
make install-panel    # instala las unidades del panel y de la reindexación
systemctl --user start voice-agent-panel
```

El usuario administrador se crea o se actualiza en cada arranque a partir del
`.env.panel`, así que cambiar la contraseña es editar el fichero y reiniciar.

Para desarrollar sin contenedores, con recarga inmediata:

```bash
make panel        # migra, siembra el usuario y sirve en 127.0.0.1:8080
make panel-export # exporta la configuración sin reiniciar el agente
```

---

## Por qué dos imágenes

| | Agente | Panel |
|---|---|---|
| Tamaño | 1,69 GB | 280 MB |
| Construcción | ~20 min | ~1,5 min |

El panel depende de `voice-agent-core` —configuración, modelos y lectura de
`/proc`, sin una sola dependencia pesada— y **nunca** de `voice_agent`. Lo que lo
garantiza no es la disciplina: `uv sync --package` instala solo lo del paquete
indicado, así que Pipecat y chromadb ni se descargan, y un `RUN` al final de
`Containerfile.panel` falla la construcción si aparecieran. `test_core_liviano.py`
guarda la otra mitad del trato, comprobando que el paquete compartido no engorde.

El precio de haberlas juntado habría sido pagar veinte minutos de construcción
cada vez que se toca una plantilla.

---

## Por qué el control va por D-Bus y no por la API de Podman

La unidad del agente la genera Quadlet, y lo que ejecuta es
`podman run … --rm -d --sdnotify=conmon`, con un `ExecStop` y un `ExecStopPost`
que hacen `podman rm -f`. El proceso que systemd vigila es **conmon**, no el
agente.

Si el panel reiniciara el contenedor por la API de Podman, conmon moriría,
systemd daría el servicio por caído y su `ExecStopPost` **destruiría el
contenedor que Podman acaba de rearrancar**. Y aunque funcionase por casualidad,
el estado que enseñara el panel y el de `systemctl status` discreparían, que es
lo último que puede permitirse un panel de control.

Hablando con systemd por su bus de sesión, el dueño del ciclo de vida sigue
siendo systemd y no hay dos fuentes de verdad.

### `--userns=keep-id` no es opcional

La autenticación EXTERNAL de D-Bus manda el uid que el cliente cree tener y el
servidor lo contrasta con `SO_PEERCRED`. En un contenedor rootless por defecto el
proceso es uid 0 dentro pero el kernel reporta 1000 fuera, y el bus responde
`REJECTED EXTERNAL`. Medido en la placa:

| | uid dentro | Respuesta del bus |
|---|---|---|
| Nativo | 1000 | `OK <guid>` |
| Contenedor sin `keep-id` | 0 | `REJECTED EXTERNAL` |
| Contenedor con `keep-id` | 1000 | `OK <guid>` |

---

## Reindexar la base de conocimiento

El botón lanza `voice-agent-ingest.service`, una unidad de un solo uso que corre
con la **imagen del agente** —la que tiene chromadb y fastembed— y su propio tope
de memoria. Hacerlo dentro del panel obligaría a meter esas dependencias en su
imagen; hacerlo dentro del agente le pegaría un pico de 400 MB mientras conversa.

**La llamada no espera**, aunque la unidad sea `Type=oneshot`. Quien espera es
`systemctl`, que se queda escuchando la señal `JobRemoved`; el `StartUnit` del
bus vuelve en cuanto systemd encola el trabajo. Medido en la placa: **0,01 s**.
Por eso el panel registra la reindexación como *lanzada* y no como terminada, y
consulta el estado de la unidad para saber cómo acabó.

Ese estado se lee con una vuelta de tuerca que conviene conocer: al ser un
oneshot con `RemainAfterExit=no`, systemd **descarga** la unidad en cuanto
termina bien, y `GetUnit` responde `Unit ... not loaded`. Una que falló, en
cambio, sigue cargada. De modo que "no se puede consultar" significa "terminó
bien", `activating` significa "está reindexando" y `failed` significa lo que
parece.

La unidad ya **no lleva `--reset`**: desde que cada tema tiene su colección, la
ingesta normal reconcilia y reconstruir desde cero solo hace falta al cambiar el
modelo de embeddings.

### La barra de avance

La ingesta publica por dónde va en `<DATA_DIR>/ingesta/progreso.json` —el mismo
canal por fichero que usan el estado de arranque del agente y el progreso de los
lotes de Calidad— y la página lo consulta **una vez por segundo** mientras la
unidad está viva. Se ve la fase, el documento que está procesando, cuántos van de
cuántos y, sobre todo, **cuántos documentos no ha habido que tocar**: es la cifra
que explica que reindexar 107 PDF pueda terminar en siete segundos (ver la
huella, en [rag.md](rag.md)). Al terminar, la página se recarga sola, porque con
la reindexación cambian también el veredicto del índice y los avisos de la
portada.

Es sondeo corto y no un `EventSource` como el de la página de Logs, y la
diferencia no es de gusto: el log es un chorro sin final que hay que
*transmitir*, mientras que esto es un retrato pequeño que se relee. Con SSE, una
pestaña olvidada dejaría un hilo del panel ocupado toda la reindexación.

La respuesta lleva **dos** cosas: lo que dice el fichero y si la unidad sigue
viva según systemd. Hace falta lo segundo porque una ingesta que muera de golpe
—sin memoria, por ejemplo— deja el fichero congelado a mitad y sin nadie que lo
desmienta, y la barra se quedaría en el 40 % para siempre.

> Reindexar mientras el agente conversa es la combinación que más cerca está de
> quedarse sin memoria en esta placa: 2 GB del agente + 1 GB de la ingesta +
> 0,25 del panel + 1,5 del sistema, sobre 3,8 GB. El panel lo avisa.

---

## Seguridad

- **Todo cerrado por defecto.** `LoginRequiredMiddleware` cierra el panel entero;
  las excepciones (el login y `/healthz`) se marcan una a una, que es mucho más
  difícil de olvidar que acordarse de proteger cada vista nueva.
- **Solo loopback.** Se llega por túnel SSH. Exponerlo a la red local exige poner
  `PANEL_PERMITIR_LAN=1` a conciencia.
- **Toda acción es POST con CSRF.** Otra página no puede disparar un reinicio.
- **Los secretos no pasan por aquí**, y hay tres capas: están en
  `CAMPOS_PROTEGIDOS`, el contenedor del panel **no monta** el `.env` del agente,
  y el estado «clave configurada / no configurada» lo escribe el propio agente en
  `estado_arranque.json`, de modo que el panel lo enseña sin haberla visto nunca.
- **El corpus es el único sitio donde el panel escribe ficheros que no son
  suyos**, y con nombres que vienen de un formulario. Las defensas van en capas:
  el tema y el documento viajan en el cuerpo del POST y nunca en la URL; los
  nombres pasan por una lista blanca (`^[a-z0-9][a-z0-9._-]*$`, sin `..`, sin
  barras, sin ocultos); ninguna ruta se compone fuera de `corpus.resolver`, que
  además comprueba que la ruta resuelta no se salga del corpus —lo que también
  para los enlaces simbólicos—; solo se admiten las extensiones que la ingesta
  sabe leer; y borrar un tema exige que esté vacío, porque un `rmtree` disparado
  desde un navegador no es aceptable. El panel **no abre** los ficheros que
  guarda: escribe bytes y ya. Quien parsea un PDF hostil es la unidad de
  ingesta, en su propio contenedor y con su tope de memoria.
- **Meter texto en el corpus es meter texto en el contexto del modelo**, o sea
  inyección de prompt vía RAG. No es una escalada nueva —quien entra en el panel
  ya tiene ejecución de comandos por los hooks— pero conviene decirlo con esas
  palabras en vez de dejarlo implícito.
- **Los hooks de shell son la superficie real.** Quien entre en el panel consigue
  ejecución arbitraria **dentro del contenedor del agente** — no en el anfitrión:
  sin `/dev` más allá de `/dev/snd` y sin más sistema de ficheros que `/data` y
  `/corpus`. Es una escalada real pero contenida. Se puede cerrar del todo con
  `PANEL_HOOKS_COMANDO=0`.
- **El socket de D-Bus da poder sobre cualquier servicio del usuario**, no solo
  sobre el agente. `control.py` actúa únicamente sobre una lista blanca de dos
  unidades, y el nombre no se toma jamás de una petición. Es defensa en
  profundidad, no una frontera.

---

## Diagnóstico

**«Cambié algo y no pasó nada.»** Mira el banner del arranque del agente
(`make service-logs`): dice de qué fecha es la instantánea que aplicó, si hay
alma, qué herramientas están desactivadas y qué hooks y servidores MCP están
activos. Si la fecha es anterior a tu cambio, falta desplegar. La página de
despliegues enseña además lo que se envió exactamente.

**El panel arranca pero el puerto no contesta.** Casi seguro que `PANEL_HOST` se
coló desde `.env.panel`: `EnvironmentFile` pisa el `ENV` de la imagen, y uvicorn
acaba escuchando en el loopback *del contenedor*. La unidad lo fuerza a `0.0.0.0`
después de leer el fichero; si tocaste la unidad, comprueba que ese orden sigue.

**El primer arranque del panel tarda tres minutos y falla por timeout.**
`--userns=keep-id` hace que Podman cree una copia de las capas con los uid
remapeados. La unidad da `TimeoutStartSec=300` de margen; los arranques
siguientes tardan unos seis segundos. Un «Found incomplete layer» en el log es la
copia a medias del intento anterior limpiándose.

**Un servidor MCP no aparece.** Mira su estado en la página de MCP: es lo que
publicó el agente en su último arranque, con el error concreto. Un comando
inexistente da `FileNotFoundError`; un extremo HTTP caído suele dar un
`RuntimeError` de anyio o un timeout.

**El panel no ve el estado del agente.** Comprueba que ambos contenedores montan
el mismo `data/` y que `estado_arranque.json` existe. Si el agente nunca ha
arrancado con la versión nueva, no habrá nada que leer.

**«Subí un documento y el agente no lo conoce.»** Falta reindexar, y la portada
lo dice. Si ya reindexaste, mira el estado en la página de Conocimiento: si la
unidad de ingesta falló, el agente sigue con el índice anterior. Y comprueba que
el documento no esté a más de un nivel de profundidad — la página los lista
aparte, bajo *Fuera del índice*.

**La página de Conocimiento sale vacía y el corpus no lo está.** Al contenedor
del panel le falta el montaje de `corpus/`, que se añadió a la unidad junto con
esta página: `podman exec voice-agent-panel ls /corpus` lo confirma en un
segundo. Si la unidad es de antes, reinstálala con `make install-panel`.
