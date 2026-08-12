# Clara — agente de voz para seguimiento postoperatorio

Solución al [reto Clara 2026](https://github.com/Clara2026/ParticipantArtifacts):
una asistente virtual de enfermería que llama —por navegador— a pacientes recién
operados en Colombia, conversa en español en tiempo real, responde con base en
un corpus clínico indexado (RAG con conocimiento vivo), clasifica cada caso en
un triaje verde/amarillo/rojo, persiste una alerta estructurada cuando decide
escalar y deja un resumen trazable de cada llamada.

Corre completa en una placa ARM (NanoPi R4S, 4 GB de RAM, sin GPU), expuesta al
jurado mediante un túnel de Cloudflare.

## Acceso rápido para el jurado (≤ 15 minutos)

No hay nada que instalar: la solución ya está corriendo en la placa y se accede
por el navegador. Se recomienda **usar auriculares** para la llamada.

| Superficie | URL | Credenciales |
|---|---|---|
| **Interfaz de llamada** (hablar con Clara) | https://clara.voz-digital.com | el enlace **con el código ya puesto** va en el correo de entrega |
| **Consola de administración** (conocimiento vivo) | https://panel.voz-digital.com/panel/ | usuario y contraseña, en el correo de entrega |

> Las credenciales no se publican aquí porque este repositorio es público.
> Ambas viajan en el correo de entrega. La interfaz de llamada está detrás de
> un código de acceso —detrás hay una placa de 4 GB, una cuota gratuita de
> modelo y **una sola conversación a la vez**— pero el enlace del correo lo
> lleva dentro: se abre y ya está, no hay nada que teclear. El porqué de cada
> medida está en [docs/seguridad.md](docs/seguridad.md).

Pasos:

1. Abrir el enlace de la interfaz de llamada, pulsar **Connect** y aceptar el
   permiso de micrófono. Clara saluda en un par de segundos; a partir de ahí es una
   conversación normal (se puede interrumpir mientras habla).
2. Para el conocimiento vivo: entrar a la consola → **Conocimiento** → subir un
   PDF o Markdown al tema que se quiera → pulsar **Reindexar**. Al terminar la
   indexación, el agente responde con el documento nuevo **sin reiniciar
   nada**; al eliminarlo y reindexar, lo olvida.
3. Las alertas y los resúmenes de llamada quedan en `data/evaluaciones/`
   (véase [Triaje y trazabilidad](#triaje-y-trazabilidad)).

## Puesta en marcha desde cero (opcional)

Si se prefiere levantar la solución en otra máquina Linux (probado en aarch64;
en x86_64 funciona con las mismas órdenes):

```bash
git clone <este-repositorio> && cd clara
curl -LsSf https://astral.sh/uv/install.sh | sh   # gestor de entornos uv
make install                                       # dependencias (uv.lock, reproducible)
cp .env.example .env                               # rellenar GEMINI_API_KEY y DEEPGRAM_API_KEY
cp .env.panel.example .env.panel                   # credenciales del panel
make ingest                                        # indexa corpus/ (una vez)
make run-web                                       # interfaz de llamada en :7860
make panel                                         # consola en :8081 (otra terminal)
```

El micrófono del navegador exige HTTPS u `http://localhost`: para acceder desde
otra máquina hace falta un túnel (por ejemplo `cloudflared tunnel --url
http://localhost:7860`).

**Acceso desde otras redes (WebRTC):** el túnel solo transporta la
señalización; la media atraviesa gracias a un servidor TURN de Cloudflare
Realtime configurado en `.env` (`ICE_SERVERS`, `TURN_USERNAME`,
`TURN_CREDENTIAL`). Las credenciales caducan cada 48 horas: `make turn` las
renueva (usa `TURN_KEY_ID`/`TURN_KEY_TOKEN` del `.env`) y luego se reinicia el
servicio. Verificado con un móvil en red celular contra la placa.

## Arquitectura

```
navegador ──WebRTC (audio)──► SmallWebRTCTransport (aiortc)
                                   │
                     ┌─────────────┴─ pipeline Pipecat (uno por llamada) ─┐
                     │ STT Deepgram nova-3 (es, streaming)                │
                     │ VAD Silero + cierre de turno por silencio          │
                     │ LLM Gemini 2.5 Flash (AI Studio, nivel gratuito)   │
                     │   ├─ buscar_en_documentos ──► ChromaDB + fastembed │
                     │   │      (RAG multilingüe, umbral anti-alucinación)│
                     │   ├─ registrar_alerta ──► data/evaluaciones/alertas│
                     │   └─ finalizar_llamada ─► data/evaluaciones/resumenes
                     │ TTS Piper (es, local, ONNX)                        │
                     └────────────────────────────────────────────────────┘
consola Django ──ficheros compartidos──► corpus/ + reindexado (systemd oneshot)
```

Decisiones clave, con su porqué:

- **Modelo (compuerta G3): `gemini-2.5-flash` por el endpoint OpenAI-compatible
  de Google AI Studio, en su nivel gratuito.** Familia permitida por el reto,
  function calling sólido y primer token rápido, que es lo que se percibe como
  latencia en voz. Plan B conmutable por configuración (`LLM_BACKEND=groq`):
  Llama 3.3 70B en Groq, también permitido y gratuito, por si el límite de
  peticiones por minuto del nivel gratuito de Gemini se queda corto en la
  sesión en vivo.
- **STT en la nube (Deepgram nova-3) y TTS local (Piper)**: medido en esta
  placa, Whisper local añade ~3 s de latencia y un 33 % de error por palabra;
  Deepgram transcribe en streaming con cero errores en la misma batería de
  pruebas. El stack de voz es libre según las reglas del reto.
- **RAG por temas**: una colección de ChromaDB por carpeta de `corpus/`
  (apendicitis, colecistitis, cáncer colorrectal, cáncer de mama, reemplazo
  articular). Embeddings multilingües (MiniLM-L12-v2 por ONNX) porque el corpus
  mezcla español e inglés y las preguntas llegan en español. Los pasajes por
  encima del umbral de distancia se descartan y el agente dice "no lo sé" en
  vez de improvisar.
- **Conocimiento vivo sin reinicios**: el buscador relee la lista de
  colecciones en cada consulta, así que subir un documento y reindexar lo hace
  aparecer en caliente; la ingesta es reconciliadora (ids derivados del
  contenido), de modo que borrar un documento y reindexar lo hace desaparecer.
- **Un pipeline por llamada con servicios precargados**: los procesadores de
  Pipecat pertenecen a un pipeline; el juego STT/LLM/TTS se carga al arrancar
  el servidor para que el saludo no pague la carga de modelos.

## Cumplimiento por compuertas

| Compuerta | Cómo se cumple |
|---|---|
| **G2** — levantable en ≤15 min | La solución ya corre en la placa; el acceso es abrir los dos enlaces del correo de entrega. |
| **G3** — modelo permitido | `gemini-2.5-flash` (familia Gemini Flash, nivel gratuito de AI Studio). Declarado aquí, en el informe y verificable en `packages/core/src/voice_agent_core/config.py` y `src/voice_agent/services.py`. |
| **G4** — voz en tiempo real por navegador | https://clara.voz-digital.com — micrófono y voz por WebRTC, con interrupciones (*barge-in*). |
| **G5** — conocimiento vivo desde la consola | Panel → Conocimiento: subir → Reindexar → el agente lo usa; eliminar → Reindexar → lo olvida. Sin reinicios. |

## Métricas medidas

> Generadas con `make metricas` a partir de `data/metricas/*.jsonl`, que el
> agente escribe en cada llamada real (nada de estimaciones). Método: la
> latencia voz-a-voz se mide dentro del propio pipeline, desde el frame de
> fin de habla del paciente hasta el primer frame de audio del agente; los
> tokens los reporta el proveedor por invocación. Los JSONL quedan en la
> placa y se pueden cotejar con los logs de la sesión.

Medido sobre llamadas reales por navegador contra la placa (10 de agosto de 2026):

| Métrica | Valor |
|---|---|
| Latencia voz-a-voz P50 | 1,65 s |
| Latencia voz-a-voz P95 | 2,78 s |
| Invocaciones del modelo por turno | 2,7 |
| Tokens de entrada / salida por turno | 6 729 / 61 |
| Tokens de entrada / salida por llamada | 20 186 / 183 |
| Coste real por llamada (nivel gratuito de AI Studio) | $0,00 |
| Coste extrapolado a precios de pago de gemini-2.5-flash ($0,30/M entrada, $2,50/M salida) | ≈ $0,0065 |

TTFB medianos por servicio: Deepgram STT ≈ 0,75 s · Gemini (primer token) ≈ 0,63 s ·
Piper TTS (primer chunk) ≈ 0,85 s. La entrada por turno es alta a propósito: el
prompt clínico completo y los extractos del RAG viajan en cada invocación; es el
precio de que cada respuesta esté anclada en documentos.

## Triaje y trazabilidad

- **Triaje**: la herramienta `registrar_alerta` persiste la decisión
  (verde/amarillo/rojo) **en el momento en que se toma**, no al colgar, en
  `data/evaluaciones/alertas/<fecha-hora>.json`, con síntomas y justificación.
  Ante la ambigüedad, el prompt obliga a indagar antes de decidir y, en la
  duda entre dos niveles, a elegir el más grave (asimetría clínica).
- **Resumen de llamada**: `finalizar_llamada` deja en
  `data/evaluaciones/resumenes/` un JSON con paciente y procedimiento,
  síntomas reportados, decisión tomada, referencias usadas y próximos pasos.
- **Trazabilidad verificable**: cada consulta al RAG queda registrada en
  `data/evaluaciones/trazas/<id-llamada>.jsonl` con los pasajes que el índice
  devolvió de verdad (documento, tema y distancia). El campo
  `documentos_consultados` del resumen sale de esa traza, no de la memoria del
  modelo: se puede abrir el PDF citado y comprobar el pasaje.

## Seguridad y resistencia a inyección

- El prompt del sistema fija reglas inmutables (no revelar instrucciones, no
  cambiar de rol, no aceptar órdenes del interlocutor ni de los documentos).
- Los extractos que devuelve el RAG van precedidos de un blindaje que los
  declara datos, no instrucciones: un PDF subido con órdenes dirigidas al
  modelo se trata como texto citado.
- El agente no puede indicar dosis ni pautas de medicación, ni siquiera si
  aparecen en un documento: remite al médico tratante.
- La consola exige usuario y contraseña, con freno de fuerza bruta por IP; la
  edición de hooks de comandos está desactivada en el despliegue expuesto; las
  claves de API viven solo en el `.env` de la placa y el panel ni las lee ni
  las muestra.
- La interfaz de llamada está detrás de un código de acceso que viaja dentro
  del enlace y se canjea por una galleta firmada (HMAC), y tiene límites de
  duración, de inactividad y de llamadas por hora. Una llamada nueva **no
  desaloja a quien está hablando**. Todo el razonamiento, en
  [docs/seguridad.md](docs/seguridad.md).

## Limitaciones conocidas

- **Reindexar tarda unos cinco minutos** con el corpus completo en la placa
  (reconcilia los 106 PDF; los fragmentos sin cambios no se re-embeben, pero
  la extracción de texto sí se repite). El documento subido está disponible al
  terminar, sin reiniciar nada; el panel muestra el estado del proceso.

- Un PDF del corpus entregado (`Appendicitis/REVISIÓN DE LA LITERATURA...`)
  está escaneado sin capa de texto y se excluyó de la indexación; requeriría
  OCR.
- El nivel gratuito de Gemini ronda las diez peticiones por minuto; una
  conversación muy rápida puede rozarlo. Mitigación documentada:
  `LLM_BACKEND=groq` en `.env` cambia a Llama 3.3 en Groq sin tocar código.
- La voz de Piper es un español de España (`es_ES-davefx-medium`); no hay voz
  colombiana de calidad comparable en Piper. El registro y el léxico de Clara
  sí son colombianos.
- **Telefonía real (extra, experimental)**: además del navegador, el agente
  contesta llamadas de un móvil emparejado por Bluetooth (HFP/SCO) con el
  mismo prompt clínico y sus herramientas de triaje — validado con
  conversaciones reales. El reto no lo exige (la evaluación va por el
  navegador); en este camino el audio va a 8 kHz, las alertas no llevan traza
  documental y no hay resumen de respaldo al colgar sin despedida. El
  adaptador Bluetooth necesita el wide-band-speech apagado (ver
  `deploy/clara-telefonia.service`). Los botones físicos del proyecto
  base siguen desactivados.

## Estructura del repositorio

```
src/voice_agent/          el agente: pipeline, web.py (llamada por navegador),
                          rag/ (ingesta y búsqueda), tools/ (herramientas del
                          modelo), traza.py, metrica.py
packages/core/            configuración y contratos compartidos (evaluaciones,
                          tareas, rutas); el panel solo puede importar esto
packages/panel/           consola de administración (Django)
corpus/                   el corpus clínico indexable, un tema por carpeta
deploy/                   unidades systemd de usuario (web, ingest, panel)
docs/                     documentación técnica en español
tests/                    ~1000 tests; make lint && make test
```

Proyecto derivado de un agente de voz doméstico para NanoPi construido por el
mismo autor; el historial de este repositorio empieza en la importación de esa
base y contiene la adaptación completa al reto.
