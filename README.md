# Voz Digital — agentes de voz a medida

Voz Digital diseña **agentes de voz conversacionales a la medida de cada
negocio**: agentes que atienden llamadas, agendan citas, hacen seguimiento a
clientes o responden preguntas frecuentes, en español y en tiempo real.

La mejor manera de entender el producto es hablar con él: **Clara**, nuestra
asesora comercial, es ella misma un agente de voz construido con esta
plataforma. Cuéntale tu negocio y ella levanta los requisitos del agente que
necesitas; el equipo revisa lo conversado y te contacta con una propuesta.

| Superficie | URL |
|---|---|
| **Portada** | https://voz-digital.com |
| **Hablar con Clara** | https://clara.voz-digital.com (con enlace de acceso) |
| **Consola de administración** | https://panel.voz-digital.com/panel/ (privada) |

Todo corre en hardware propio de bajo consumo (una placa ARM de 4 GB sin GPU):
la misma plataforma se despliega igual en la infraestructura del cliente o en
la nube.

## Qué hace Clara

- Conversa por voz desde el navegador, en español colombiano y en tiempo real,
  con interrupciones naturales (*barge-in*).
- Reconoce a quien vuelve: cada visitante queda registrado (con su
  consentimiento conversacional: la propia charla) y una segunda visita retoma
  lo pendiente en vez de empezar de cero.
- Deja un **brief comercial estructurado** por conversación —quién es, qué
  necesita, qué agente se le propondría, próximos pasos— junto con la
  transcripción, consultables en la consola (página **Prospectos**).

## La plataforma

El mismo motor soporta **perfiles** intercambiables desde la consola: cada
perfil es un agente distinto (prompt, personalidad, herramientas y ajustes
propios) sobre la misma infraestructura de voz. Clara comercial es el perfil
activo; el repositorio incluye además un perfil clínico completo —seguimiento
postoperatorio con RAG sobre un corpus de guías, triaje verde/amarillo/rojo y
alertas estructuradas— que sirve de referencia de lo que un agente a medida
puede llegar a hacer.

Capacidades disponibles para los agentes a medida:

- **Base de conocimiento viva (RAG)**: se sube un PDF o Markdown desde la
  consola, se reindexa y el agente responde con él sin reiniciar nada.
- **Herramientas**: el agente puede registrar datos estructurados, consultar
  historiales, programar llamadas o hablar con sistemas externos (MCP).
- **Telefonía real**: además del navegador, el agente puede contestar llamadas
  de un móvil emparejado por Bluetooth (HFP/SCO).
- **Memoria entre conversaciones**: historial por interlocutor, en el canal
  web (prospectos) y en el telefónico (pacientes/clientes).

## Arquitectura

```
navegador ──WebRTC (audio)──► SmallWebRTCTransport (aiortc)
                                   │
                     ┌─────────────┴─ pipeline Pipecat (uno por llamada) ─┐
                     │ STT Deepgram nova-3 (es, streaming)                │
                     │ VAD Silero + cierre de turno por silencio          │
                     │ LLM Gemini 2.5 Flash (herramientas por perfil)     │
                     │   ├─ identificar_prospecto ─► memoria comercial    │
                     │   ├─ guardar_brief ────────► briefs estructurados  │
                     │   └─ historial_prospecto ──► conversaciones previas│
                     │ TTS Piper (es, local, ONNX)                        │
                     └────────────────────────────────────────────────────┘
consola Django ──ficheros compartidos──► perfiles, prompts, conocimiento,
                                         prospectos, despliegues
```

Decisiones clave:

- **STT en la nube (Deepgram) y TTS local (Piper)**: medido en la placa,
  Whisper local añadía ~3 s de latencia y un 33 % de error por palabra;
  Deepgram transcribe en streaming con una fracción de eso.
- **Un pipeline por llamada con servicios precargados**: el juego STT/LLM/TTS
  se carga al arrancar el servidor para que el saludo no pague la carga de
  modelos. Latencia voz-a-voz medida en llamadas reales: P50 1,65 s, P95
  2,78 s.
- **El agente escribe, la consola lee**: prospectos, briefs, historiales y
  trazas viajan por ficheros compartidos (SQLite en modo WAL y JSON); ningún
  proceso pisa lo del otro.
- **RAG con umbral anti-alucinación**: los pasajes por encima del umbral de
  distancia se descartan y el agente dice "no lo sé" en vez de improvisar; en
  el perfil clínico, además, una puerta de cobertura en código impide citar
  protocolos de una cirugía no cubierta.

## Puesta en marcha desde cero

Probado en aarch64; en x86_64 funciona con las mismas órdenes:

```bash
git clone <este-repositorio> && cd clara
curl -LsSf https://astral.sh/uv/install.sh | sh   # gestor de entornos uv
make install                                       # dependencias (uv.lock, reproducible)
cp .env.example .env                               # rellenar GEMINI_API_KEY y DEEPGRAM_API_KEY
cp .env.panel.example .env.panel                   # credenciales de la consola
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
renueva y luego se reinicia el servicio.

## Seguridad

- La interfaz de llamada está detrás de un código de acceso que viaja dentro
  del enlace y se canjea por una galleta firmada (HMAC), con límites de
  duración, de inactividad y de llamadas por hora. Una llamada nueva **no
  desaloja a quien está hablando**.
- La consola exige usuario y contraseña, con freno de fuerza bruta por IP; las
  claves de API viven solo en el `.env` de la placa y la consola ni las lee ni
  las muestra.
- El prompt del sistema fija reglas inmutables (no revelar instrucciones, no
  cambiar de rol, no aceptar órdenes del interlocutor ni de los documentos), y
  los extractos del RAG se tratan como datos citados, nunca como
  instrucciones.
- El identificador de un visitante es una galleta opaca e inadivinable, sin
  datos personales dentro; lo que Clara anota es lo que la persona le contó.

El razonamiento completo de cada medida está en
[docs/seguridad.md](docs/seguridad.md).

## Estructura del repositorio

```
src/voice_agent/          el agente: pipeline, web.py (llamada por navegador),
                          rag/ (ingesta y búsqueda), tools/ (herramientas del
                          modelo), traza.py, metrica.py
packages/core/            configuración y contratos compartidos (prospectos,
                          historial, evaluaciones, tareas, rutas); la consola
                          solo puede importar esto
packages/panel/           consola de administración (Django)
corpus/                   la base de conocimiento indexable, un tema por carpeta
deploy/                   unidades systemd de usuario (web, ingest, panel)
docs/                     documentación técnica en español
tests/                    ~1500 tests; make lint && make test
```

El proyecto nació como un agente de voz doméstico para NanoPi del mismo autor,
creció como agente clínico de seguimiento postoperatorio (finalista del reto
Clara 2026) y hoy es la plataforma sobre la que Voz Digital construye
agentes a medida.

## Licencia

El código de este repositorio se publica bajo la licencia MIT (ver
[LICENSE](LICENSE)). Los PDF de `corpus/` son guías y artículos clínicos de
terceros, incluidos solo como material de demostración del RAG: cada uno
conserva la licencia de su editor y la MIT no se les aplica.
