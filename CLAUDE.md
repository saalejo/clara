# CLAUDE.md

Guía para Claude Code al trabajar en este repositorio.

## Dónde vive esto

**El proyecto corre en una NanoPi R4S, no en la máquina desde la que lo editas.**
Se accede con `ssh nanopi`; la ruta es `~/learning/voice-agent`. Las dependencias
son aarch64 y PyAudio se compila allí, así que **todo `uv run` y todo `make` van
en la placa**.

El `Makefile` ya antepone `~/.local/bin` al PATH: los objetivos funcionan por SSH
no interactivo sin exportar nada.

## Qué es

Agente de voz conversacional en español sobre **Pipecat 1.6**: micrófono →
Silero VAD → STT → LLM vía OpenRouter (con herramientas) → Piper → altavoz. RAG
sobre ChromaDB embebido. Se despliega con Podman rootless y unidades Quadlet.

Es un **workspace de uv con cinco paquetes** y un único `uv.lock`:

| Paquete | Qué es | Dependencias |
|---|---|---|
| `packages/core` → `voice_agent_core` | Configuración y modelos compartidos | pydantic-settings, loguru |
| `src/voice_agent` | El agente | Pipecat, chromadb, fastembed… |
| `packages/panel` → `voice_agent_panel` | El panel web | Django, jeepney, uvicorn |
| `packages/telefonia` → `voice_agent_telefonia` | El puente Bluetooth manos libres. **Corre nativo, no en un contenedor** | dbus-fast, starlette, uvicorn |
| `packages/botones` → `voice_agent_botones` | El mando físico de la tarjeta de sonido. **Corre nativo**, como el puente | httpx, loguru (nada de audio: `wave` de la stdlib y `aplay`) |

**El panel nunca importa `voice_agent`, solo `voice_agent_core`.** Es lo que
permite que su imagen pese 280 MB y se construya en minuto y medio, frente a
1,69 GB y veinte minutos la del agente. `tests/test_core_liviano.py` comprueba
que `voice_agent_core` no arrastre nada pesado; no lo rompas por comodidad.

## Comandos

```bash
make run            # arranca en local
make lint           # ruff + mypy (estricto, sin errores)
make test           # 1469 tests, sin red ni modelos
make audio-check    # graba, mide y dice si el VAD te oiría
make ask Q="..."    # consulta el RAG sin voz
make ingest         # reindexa corpus/
make build          # imagen del agente (~20 min, ver avisos)
make build-panel    # imagen del panel (~1,5 min)
make panel          # panel web en local, sin contenedor
```

`uv sync` a secas **no basta**: es un workspace, así que hace falta
`uv sync --all-packages` (lo hace `make install`) o el panel se queda sin Django.

`audio-check` y `audio-noise` **paran el servicio y lo restauran** solos.

## Trampas que cuestan horas

Todas están medidas y documentadas; no las redescubras.

| Trampa | Detalle |
|---|---|
| **El VAD no va en `TransportParams`** | En Pipecat 1.x va en `LLMUserAggregatorParams`. `TransportParams` es Pydantic e **ignora campos desconocidos en silencio**: el agente arranca y nunca detecta voz. Lo pilló mypy, no una prueba. |
| **La estrategia de fin de turno por defecto necesita PyTorch** | Hay que declarar `SpeechTimeoutUserTurnStopStrategy` explícitamente. Ver `services.py:build_turn_strategies`. |
| **La tarjeta USB no admite 16 kHz** | Solo 44.1/48 kHz, salida estéreo obligatoria. Por eso existe `deploy/asound.conf` y por eso hay que usar el dispositivo `default`, nunca `hw:0,0`. |
| **Los logs del servicio están en el journal del SISTEMA** | `journalctl --user -u voice-agent` sale vacío. Usa `make service-logs`. El panel no los lee de ahí: el agente escribe además a `data/logs/agente.log`, en el volumen compartido. |
| **`--env-file` y `EnvironmentFile` pisan el `ENV` de la imagen, y ha mordido dos veces** | Primero con `DATA_DIR=data` en `.env`, que machacaba el `/data` del contenedor; después con `PANEL_HOST`, cuyo `127.0.0.1` —pensado para `make panel`— se colaba en el contenedor y dejaba a uvicorn escuchando en su propio loopback: servicio activo, logs limpios, puerto publicado mudo. Las variables que deban ganar van **después** del `EnvironmentFile` en la unidad. |
| **El primer arranque del panel tarda tres minutos** | `--userns=keep-id` obliga a Podman a copiar las capas con los uid remapeados. La unidad da `TimeoutStartSec=300`; los siguientes tardan seis segundos. Un `Found incomplete layer` es la copia a medias del intento anterior. `keep-id` **no es opcional**: sin él, la autenticación EXTERNAL de D-Bus responde `REJECTED EXTERNAL` y el panel no puede gobernar el servicio. |
| **Un hook nunca puede descartar un frame de control** | Solo se transforman o vetan `TranscriptionFrame` y `LLMTextFrame`, que son `DataFrame`. Tragarse un `SystemFrame` o un `ControlFrame` no da error: cuelga el pipeline y lo único que se ve es un `timeout waiting for…`. Lo garantiza un `isinstance` en `hooks.py` y lo vigila `tests/test_hooks.py`. |
| **Los transportes de MCP y anyio se llevan mal con las tareas** | Cerrar los clientes desde un manejador de evento revienta con `Attempted to exit cancel scope in a different task`; por eso el cierre va en un `finally`. Y un servidor que falla puede salir por `CancelledError`, que es `BaseException` y se cuela bajo `except Exception`: la conexión corre en su propia tarea para que eso no tumbe el arranque. Ver el comentario largo de `mcp.py`. |
| **Todo arranque de un contenedor costaba 90 s de más** | Quadlet inyecta `Wants=`/`After=podman-user-wait-network-online.service` en **toda** unidad `.container`, y ese ayudante es un `until systemctl is-active network-online.target` con timeout de 90 s. En esta placa `network-online.target` tenía `WantedBy=` **vacío**: `systemd-networkd-wait-online` estaba habilitado, pero nadie arrastraba el target, así que nunca se activaba y el ayudante agotaba el timeout **en cada arranque**. Medido: `systemctl --user restart voice-agent` tardaba 101 s, de los cuales 90 eran espera pura. Arreglado con `sudo systemctl add-wants multi-user.target network-online.target` → 11,7 s. Si vuelve a aparecer, mira `systemctl is-active network-online.target` antes que nada. |
| **Mover el almacén de Podman rompe libpod** | La base de datos `db.sql`, dentro del propio almacén, **graba la ruta absoluta con la que se creó**. Al migrar el almacén de un pendrive a la tarjeta, Podman se negó a arrancar con `database static dir ... does not match our static dir ...` y ninguna unidad levantó. Se arregla apartando ese fichero para que se recree: son metadatos de contenedores y pods, no imágenes, y las unidades Quadlet corren con `--rm`. Hoy el almacén está en `~/.local/share/containers/storage`, el sitio por defecto, sin ningún override. |
| **Interrumpir un `podman build` deja GB de basura** | Invisibles para `podman system df`. Caen en `$HOME/.cache/podman-build`, que es a donde `make build` apunta `TMPDIR` para que `clean-space` sepa dónde buscar. Límpialo con `make clean-space`. |
| **El banco de muletillas va después de `build_tts`** | Es `build_tts` quien descarga la voz de Piper. Al revés, cambiar `TTS_VOICE` mata el arranque. |
| **Un `datetime` con zona horaria enmudece el planificador entero** | `ProgramadorTareas` compara siempre contra `datetime.now()`, que es **naive**, igual que `ExpresionCron` (ver su docstring: Colombia no tiene cambio de hora). Basta que a un `cuando` le entre un aware —lo que devuelve pydantic si alguien escribe un ISO con offset, y el modelo escribe lo que le sale— para que la comparación lance `TypeError: can't compare offset-naive and offset-aware datetimes` **dentro de `correr()`**, que se lo traga con `logger.exception` y sigue el bucle. Síntoma: las tareas dejan de sonar y no hay nada roto a la vista, solo una línea en el log. Por eso `MisionPuntual._naive_en_hora_local` normaliza en el validador y la herramienta no deja al modelo escribir directo en el modelo, sino que pasa por `interpretar_cuando`. |
| **`misiones_agente.json` lo escribe el agente; `tareas.json`, el panel** | Es la doctrina de un fichero, un escritor, y aquí va en las dos direcciones. El panel reescribe `tareas.json` **entero** en cada guardado, así que nada que apunte el agente sobreviviría ahí; y si el panel escribiera en el fichero del agente, pisaría lo que este acabara de agendar en mitad de una conversación. De ahí que el botón del panel diga *Cancelar* y no *Borrar*: deja el id en un tercer fichero (`misiones_canceladas.json`) que el planificador consulta por mtime. Corolario dentro del agente: el planificador **no relee** su propio fichero, le pregunta al `AlmacenMisiones` compartido — dos copias de la misma verdad en el mismo proceso son una carrera garantizada. |
| **El umbral de distancia NO distingue una cirugía de otra** | Mide si un fragmento se parece a la consulta; la cirugía del paciente no viaja en la consulta. Una pregunta postoperatoria genérica en español pega con texto postoperatorio genérico de **cualquier** documento clínico: medido en la placa, «cuidados de la herida cirugia de cataratas ojo» devolvía cinco pasajes de colecistitis y reemplazo articular a **0.457–0.460**, y «signos de alarma cirugia ocular vision borrosa» cinco de apendicitis a 0.445 — todos dentro de la nube que `docs/rag.md` daba por cubierta (≤ 0.41 cubiertas, ≥ 0.56 ajenas). Bajar el umbral se lleva por delante las buenas. Tampoco vale pedírselo al modelo por prosa: se intentó, y una línea de aviso no gana contra cinco bloques de texto clínico con pinta de autoridad. Por eso la cobertura se decide **en código** (`voice_agent_core.cobertura`) y, cuando la cirugía no está cubierta, `buscar_en_documentos` **ni llama al retriever** — sin extractos no hay de dónde sacar una respuesta que suene respaldada. Corolario: si una sonda de una cirugía cubierta sale vacía, revisa los alias, **no** el umbral. |
| **Añadir una columna al esquema del historial NO la añade en la placa** | `historial._ESQUEMA` es todo `CREATE TABLE IF NOT EXISTS`, así que sobre la base que ya existe no ejecuta nada. Hace falta declararla también en `_COLUMNAS_AÑADIDAS`, que `_migrar_columnas` aplica con `ALTER TABLE` guardado por `PRAGMA table_info`. Sin eso el síntoma es mudo: cada anotación falla con `no such column` y se la traga el `except (sqlite3.Error, OSError)` de la casa, así que el historial deja de anotar sin decir nada. |
| **ChromaDB reconstruye la función de embeddings por su cuenta** | Guarda su config en los metadatos de la colección y la rehace con `build_from_config`, así que el número de veces que se instancia no lo decide tu código. Sin la caché de `embeddings.cargar_modelo`, indexar 11 fragmentos cargaba el modelo **seis veces**: 43 s en vez de 16. Si añades otra función de embeddings, cachea el modelo, no el envoltorio. |
| **Reconciliar por identificador NO ahorra la lectura** | El id de un fragmento lleva un resumen de su contenido, así que saber si ya está indexado exige **haberlo troceado**, y trocearlo exige extraer el texto del PDF — que en esta placa es la mitad cara. Síntoma: reindexar 106 PDF sin haber cambiado nada tardaba **cerca de una hora** y el botón del panel era inusable en vivo. Lo que lo arregla es la **huella** por documento (contenido + `EMBEDDING_MODEL`/`CHUNK_SIZE`/`CHUNK_OVERLAP`), guardada en los metadatos de cada fragmento: si cuadra —y cuadra el número de fragmentos, que si no una pasada interrumpida se da por completa— el documento ni se abre. Medido tras adoptar el índice: 7 s. **Adoptar un índice viejo cuesta una pasada completa de lectura** (6 min medidos), porque sin leerlo no se sabe qué fragmento es cuál; esa pasada no recalcula embeddings, solo remienda metadatos con `update`. |
| **La función de embeddings ya no carga el modelo al construirse** | Lo carga en el primer uso, porque abrir una colección la construye y reindexar sin cambios abre todas sin vectorizar nada. Dos consecuencias: quien necesite el modelo caliente tiene que pedirlo (`Retriever.__init__` llama a `cargar_modelo`, y por eso el arranque del agente sigue pagando los diez segundos en vez de la primera pregunta del paciente), y en los tests **parchear `funcion_embeddings` ya no basta**: hay que parchear también `cargar_modelo`, o la batería se baja 120 MB de modelo. |
| **`StartUnit` por D-Bus no espera** | Vuelve en cuanto systemd encola el trabajo (medido: 0,01 s); quien espera a que la unidad acabe es `systemctl`, escuchando `JobRemoved`. Y un oneshot con `RemainAfterExit=no` se **descarga** al terminar bien, así que `GetUnit` falla con "not loaded" — que es el caso normal, no un error. |
| **La política D-Bus de oFono deniega a uid 1000, y `at_console` no salva** | `ofono.conf` trae un `<policy context="default"><deny send_destination="org.ofono"/>`. Su cláusula `at_console` no aplica: con systemd, dbus la resuelve preguntándole a logind si el uid tiene un **asiento**, y ni una sesión SSH ni un servicio de usuario con linger lo tienen. Hace falta el drop-in `/etc/dbus-1/system.d/ofono-agente-de-voz.conf`. Y **cuidado al comentarlo**: XML prohíbe dos guiones seguidos dentro de un comentario, así que escribir ahí una orden con opciones largas rompe el fichero, dbus lo descarta **entero** y el síntoma es el mismo `Access denied` de antes. Solo se ve en el journal de dbus como `not well-formed (invalid token)`. |
| **`jeepney` en asyncio no pasa descriptores de fichero; por eso hay DOS librerías de D-Bus** | `grep -rn enable_fds .venv/.../jeepney/io/` sale en `threading.py` y `trio.py` pero **no** en `asyncio.py`. El audio SCO de la fase 2 llega como descriptor y además hay que exportar un objeto D-Bus, así que el puente usa `dbus-fast`. El panel se queda con jeepney porque hace llamadas bloqueantes y funciona. **No las unifiques** sin repetir ese grep. |
| **Una sesión PBAP muere con quien la creó** | obexd ata la sesión al dueño del nombre de D-Bus que llamó a `CreateSession`. Con `busctl` cada comando es una conexión distinta, así que la sesión se destruye antes del siguiente y `Select` falla con *"Method doesn't exist"* — que parece un problema de versión de la interfaz y no lo es. **PBAP no se puede depurar con `busctl`.** Además obexd registra `Client1` un instante **después** de aparecer en el bus, así que la primera llamada falla igual; `DescargaPBAP` espera. |
| **Una unidad de usuario no puede ordenarse contra una del sistema** | `After=bluetooth.service` en `voice-agent-telefonia.service` se ignora **en silencio**: el gestor de usuario no conoce las unidades del sistema. Da falsa seguridad. El puente tolera que no haya bus y reengancha solo. |
| **Añadir un miembro al workspace obliga a tocar LOS DOS Containerfiles** | uv resuelve el workspace entero contra un único `uv.lock` y falla si le falta un miembro que el lock menciona, aunque esa imagen no lo instale. El error habla del workspace y **no menciona el paquete nuevo**. |
| **Los logs del puente tampoco están en el journal de usuario** | `journalctl --user -u voice-agent-telefonia` sale vacío igual que con el agente, pero por otro motivo: esta placa no mantiene journal de usuario. Van al del **sistema**, donde el proceso aparece etiquetado como `uv` —no con el nombre de la unidad— porque el `ExecStart` es `uv run`. Se filtra con `sudo journalctl _SYSTEMD_USER_UNIT=voice-agent-telefonia.service`, o `make telefonia-logs`. |
| **Las llamadas de WhatsApp llegan como `alerting` y sin número** | Las apps de VoIP se integran en Android como `ConnectionService` autogestionado, así que sus llamadas SÍ llegan al manos libres y se pueden contestar. Pero llegan en estado `alerting` —que el estándar de HFP reserva para las **salientes**— y con un identificador de relleno (`10000000`) en vez del número. Por eso `_ESTADOS_SALIENTES` solo contiene `dialing`, y por eso una llamada de app no se puede resolver contra la agenda: el dato no viaja. Marcar por WhatsApp es imposible: `ATD` siempre abre una llamada del operador. |
| **Sin cobertura, el puente no ve NADA** | Un silencio total durante una llamada de prueba suele ser falta de señal, no un fallo del código. `org.ofono.NetworkRegistration` lo dice: se ha visto pasar de `unregistered` a `registered / TIGO / Strength 40` en minutos. Las de WhatsApp siguen funcionando porque van por datos. Mira la cobertura antes de depurar. |
| **El dongle Bluetooth y la tarjeta de sonido comparten hub** | La placa tiene dos puertos USB y el pendrive del almacén de Podman ocupa el otro. Enchufar el dongle directamente **desconecta la tarjeta de sonido**. `cat /proc/asound/cards` lo dice enseguida. Desde el modo enchufar-y-listo ya no es silencioso: el agente lo anuncia («La tarjeta de sonido ha cambiado»), pasa a solo-teléfono y se recupera solo al reconectarla. |
| **`AddDevice=/dev/snd` congela los dispositivos del contenedor** | Podman copia los nodos que existen **al arrancar el contenedor**: una tarjeta USB conectada después crea sus nodos en el anfitrión y el contenedor no los ve nunca. Por eso la unidad monta `Volume=/dev/snd:/dev/snd`. La detección tampoco puede depender de PortAudio en caliente —enumera una vez—: el agente vigila `/proc/asound`, que no está sujeto a espacios de nombres, y rearma PortAudio en cada montaje. La tarjeta es así opcional y conectable en caliente: sin ella el agente queda en solo-teléfono, no muere. Simulable sin hardware: `sudo modprobe snd-dummy id=Device fake_buffer=0` (con `fake_buffer=0`, que sin él dmix/dsnoop no pueden hacer mmap). |
| **Tras reenumerarse el dongle, el móvil NO vuelve solo** | Android reintenta contra sus dispositivos recordados cuando quiere: medido, tres minutos tras un unbind/rebind del UB500 sin un solo intento del TECNO (bluetoothd sí reenciende el adaptador). Por eso el puente llama él a la puerta —`reconexion.py`, `Device1.Connect` sobre el emparejado+de confianza con perfil HFP AG, cada 30 s mientras no haya móvil— como cualquier kit de coche. Un dongle se puede simular sin tocarlo: `echo 7-1 \| sudo tee /sys/bus/usb/drivers/usb/unbind` (y `bind` para devolverlo). |
| **Emparejar sin agente de Bluetooth falla por timeout** | Android pide confirmar un código por comparación numérica. `bluetoothctl` registra un agente `DisplayYesNo` que espera a que alguien **teclee**, y en una placa sin pantalla no hay nadie: a los 30 s sale `Simple Pairing Complete: LMP Response Timeout (0x22)`. Hay que levantar `bt-agent --capability=NoInputNoOutput`. |
| **El corpus va `:ro` en el agente y la ingesta, y `rw` solo en el panel** | Es el único volumen que el panel escribe. Su `Environment=CORPUS_DIR` tiene que ir **después** del `EnvironmentFile` de la unidad, como `DATA_DIR` y `PANEL_HOST`. |
| **El botón del micrófono de la tarjeta no emite NADA** | Ni evento HID ni cambio en ningún control de ALSA: silencia dentro del códec y desde fuera es **indistinguible de un botón desconectado**. No hay otro device que buscar. Por eso el mute del micrófono está en el botón de **audio**, que es confuso y no tiene alternativa. |
| **`KEY_MUTE` no distingue mantener pulsado** | Manda el par pulsar/soltar **en el mismo microsegundo** aunque lo aprietes tres segundos. Medido tres veces de tres: `27.391s MUTE PULSA` / `27.391s MUTE suelta`. El rocker sí da duración real (`39.839s` → `42.111s` = 2272 ms). Es el comportamiento típico de la usage «Mute» de HID consumer. **Consecuencia: los niveles por duración solo pueden vivir en el rocker**, y `TECLAS_CON_NIVELES` es donde está escrito. |
| **El device de botones no declara `EV_REP`, y el HID se sondea cada 32 ms** | `EV=13` (SYN\|KEY\|MSC): el kernel **no autorrepite**, así que mantener VOL+ no sube el volumen solo. Y todas las duraciones medidas son múltiplos exactos de 32 ms (288, 1152, 1760, 1824, 4000, 6016), así que la granularidad real del detector es de 32 ms: **no afines umbrales por debajo de eso**. |
| **Con `EVIOCGRAB`, la unidad en marcha impide depurar los botones** | El demonio pide el device en **exclusiva** (hace falta: tiene handler `kbd` y sin el grab las teclas se inyectan también en la consola virtual). Con el servicio activo, cualquier otro lector ve **silencio**, y el síntoma —«la sonda no detecta nada»— parece una avería del hardware. `make botones-sonda` para la unidad y la restaura; para convivir, arranca con `BOTONES_ACAPARAR=0`. |
| **Encender las galletas seguras del panel sin `CSRF_TRUSTED_ORIGINS` lo rompe ENTERO** | En cuanto Django cree que la conexión es HTTPS —lo que hace `SECURE_PROXY_SSL_HEADER`, no las galletas— activa la comprobación estricta de Origin en **cada POST**: guardar ajustes, reiniciar el agente, subir un documento y reindexar empiezan a responder 403 mientras los GET siguen perfectos, así que el síntoma no apunta a las galletas por ningún lado. `settings.py` **falla al arrancar** si se pone `PANEL_TRAS_TLS=1` sin los orígenes: mejor un servicio que no levanta que uno que parece bien y se cae en la demostración. |
| **Una subclase de `LoginView` que redefina `dispatch` pierde `login_not_required`** | `LoginView` lo lleva como `@method_decorator(..., name="dispatch")` y `View.as_view()` copia los atributos de `dispatch` al callable. Redefinirlo sin volver a decorarlo deja el login detrás del `LoginRequiredMiddleware`: bucle de redirecciones y panel inaccesible. Por eso `VistaEntrar` sobreescribe `post`/`form_valid`/`form_invalid` y nunca `dispatch`; lo vigila `tests/test_panel_entrar.py`. |
| **El estado de aiortc no sirve para saber si una pestaña sigue ahí** | Tarda decenas de segundos en declarar muerta una conexión cuyo navegador ya se fue; por eso el «gana la última conexión» desalojaba a todo el mundo. La señal buena es `SmallWebRTCConnection.is_connected()`, que mira la hora del último ping y es `False` a los **tres segundos**. Sobre eso se decide si una oferta nueva espera, entra o recibe un 409 (`_esperar_hueco`). |
| **`CF-Connecting-IP` solo es de fiar mientras uvicorn escuche en loopback** | Es de donde sale la identidad del visitante para todos los límites por IP: cloudflared la reescribe siempre, así que por el túnel no se puede falsear. Pero con `--host 0.0.0.0`, cualquiera en la red local la inventa y se salta el freno. La unidad escucha en `127.0.0.1` por eso, no por casualidad. |
| **Una ruta de FastAPI que devuelve `dict \| Response` necesita `response_model=None`** | FastAPI intenta derivar un modelo de Pydantic del tipo de retorno y lanza `FastAPIError` **al construir la aplicación**, no al llamar a la ruta: el servicio no arranca. Pasó al añadirle a `/api/offer` los rechazos por ocupado y por cuota. |
| **`parado` NO es lo contrario de `activo`, y `failed` puede ser el ÉXITO** | Importa a cualquiera que toque `voice_agent_core.systemd`. Entre activo y parado hay un `deactivating` que no es ninguna de las dos cosas: darlo por parado anunciaba el final **cuarenta milisegundos** después de pedirlo. Y parar el agente acaba en `failed`, que es el desenlace **normal**: su unidad es `Type=notify` con `KillMode=mixed` y el agente no maneja SIGTERM, así que el contenedor sale con código distinto de cero. Medido: once segundos de parada limpia que terminan en `failed: exit-code`. Pitar error ahí sería mentir. |
| **Para el agente, el `active` de systemd NO significa listo** | Su unidad la genera Quadlet con `--sdnotify=conmon`, así que systemd la da por activa en cuanto arranca el **contenedor**, no cuando el proceso ha cargado Whisper, Piper y los embeddings. Medido: **24 segundos** entre el `active` y el `estado_arranque.json` que el agente escribe al montar el pipeline. Quien quiera avisar de «ya puedes hablarle» tiene que esperar ese testigo, no el estado de la unidad. |

## Convenciones

- **Identificadores, docstrings y comentarios en español.** `descubrir_documentos`,
  `ErrorDeControl`, `Pasaje`. La excepción son los campos de `Settings`, que están
  en inglés porque **son** las variables de entorno (`corpus_dir` → `CORPUS_DIR`),
  y los métodos que impone una librería (`build_from_config`, `list_collections`).
  Los nombres de herramientas van en español: forman parte del prompt.
- **Los docstrings de `tools/` son código.** Pipecat deriva de ellos el esquema
  JSON que ve el modelo. Reescribirlos cambia el comportamiento del agente;
  `tests/test_tools.py` lo vigila.
- **Las descripciones de los campos de `Settings` también son código.** El panel
  genera su formulario por introspección y las enseña tal cual; un test comprueba
  que ningún campo se quede fuera.
- Las herramientas reciben sus dependencias por `params.app_resources`, nunca
  por variables globales.
- Los tests no tocan red ni cargan modelos. Mantenlo así.

## Antes de dar algo por bueno

- `make lint && make test` en verde.
- Si tocas audio, **mide con voz real, no con tonos**: Silero está entrenado
  para ignorar tonos puros y una prueba con un seno de 440 Hz dio un falso
  negativo que costó una sesión entera.
- Las cifras de `docs/rendimiento.md` están medidas en la placa. Si cambias algo
  que las afecte, vuelve a medir en vez de estimar.

## Documentación

`docs/seguridad.md` explica qué protege la superficie expuesta (la puerta de
la interfaz de llamada, los límites de sesión, el panel) y por qué cada pieza
es como es. `docs/audio.md` es el más importante: el camino del audio es donde más tiempo se
pierde. Luego `arquitectura.md`, `rag.md`, `herramientas.md`, `panel.md`,
`telefonia.md`, `botones.md`, `despliegue.md` y `rendimiento.md`.
`docs/marco-legal.md` recoge la investigación del marco legal colombiano del
negocio (datos e IA, contacto comercial, sector salud, tributación) con su
checklist de cumplimiento; no es asesoría legal.
