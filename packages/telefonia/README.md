# voice-agent-telefonia

El **puente de telefonía**: convierte el móvil emparejado por Bluetooth en algo
que el agente de voz puede usar. Contesta, cuelga, marca y busca en la agenda.

La placa hace de **unidad manos libres** (HFP-HF, el papel del manos libres del
coche) y el móvil de **pasarela de audio** (HFP-AG). Quien habla HFP de verdad
es oFono; quien descarga la agenda es obexd; este paquete solo los orquesta y
publica el resultado por un socket unix.

```
móvil  ──HFP──▶ bluetoothd ──▶ ofonod   (bus del SISTEMA)
       ──PBAP─▶ obexd                   (bus de SESIÓN)
                     │
                     ▼  dbus-fast
             voice_agent_telefonia       ← este paquete, NATIVO
                     │
                     ▼  HTTP + SSE sobre <DATA_DIR>/run/telefonia.sock
                voice_agent              ← dentro del contenedor
```

## Las tres decisiones que explican el paquete

### 1. Por qué es un paquete aparte y no parte del agente

Por lo mismo que existe `packages/panel`: cada `Containerfile` hace
`uv sync --package <x>`, así que lo que no está en la lista de dependencias de
un paquete **no puede** entrar en su imagen. Aquí el requisito va en los dos
sentidos:

- `dbus-fast`, `starlette` y `uvicorn` no deben entrar en la imagen del agente.
- `pipecat`, `chromadb` y `fastembed` no deben entrar aquí: el puente corre
  **nativo** en la placa, y arrastrar 1,1 GB de dependencias para hablar con
  D-Bus sería absurdo.

Lo garantiza `tests/test_telefonia_liviana.py`, no la buena voluntad.

### 2. Por qué corre nativo y no en un contenedor

Tres razones, en orden de peso:

1. **Autenticación de D-Bus.** El bus contrasta el uid que dice el cliente con
   `SO_PEERCRED`. En un contenedor rootless el proceso es uid 0 dentro pero 1000
   fuera, y el bus responde `REJECTED EXTERNAL`. El panel lo resuelve con
   `--userns=keep-id` (ver `packages/panel/src/voice_agent_panel/control.py`),
   pero aquí habría que hacerlo con **dos** buses, el del sistema y el de
   sesión, y además el del sistema está gobernado por la política de oFono.
2. **Iterar.** Reconstruir la imagen del agente son diez minutos. El puente se
   escribe a base de probar cosas contra un móvil real.
3. **El audio de la fase 2.** El descriptor del socket SCO lo entrega oFono por
   D-Bus; meterlo en un contenedor es trabajo extra sin ninguna ganancia.

Efecto lateral que conviene saber: **aquí `journalctl --user -u
voice-agent-telefonia` sí funciona**, al revés que con las unidades Quadlet del
agente y el panel, porque systemd ejecuta el proceso directamente en vez de un
`podman run -d` vigilado por conmon.

### 3. Por qué `dbus-fast` y no `jeepney`, que ya está en el proyecto

Medido, no supuesto:

```console
$ grep -rn "enable_fds" .venv/lib/python3.13/site-packages/jeepney/io/
io/threading.py:107:def open_dbus_connection(bus='SESSION', enable_fds=False, …)
io/trio.py:69:    def __init__(self, socket, enable_fds=False)
```

`jeepney/io/asyncio.py` **no aparece**: su backend de asyncio no puede recibir
descriptores de fichero. La fase 2 necesita exactamente eso —
`org.ofono.HandsfreeAudioAgent.NewConnection(o card, h fd, y codec)` entrega el
socket SCO como descriptor — y además necesita **exportar** un objeto D-Bus,
porque es oFono quien nos llama a nosotros.

El panel se queda con jeepney: hace llamadas bloqueantes a systemd y funciona.
El proyecto acaba con dos librerías de D-Bus, en dos procesos y dos imágenes
distintas, cada una elegida por su caso. **No las unifiques** sin volver a
comprobar el `grep` de arriba.

## Módulos

| Módulo | Contenido |
|---|---|
| `normaliza.py` | Normalización de nombres y números, y la clave fonética española |
| `vcard.py` | Analizador de vCard 2.1/3.0, incluido `QUOTED-PRINTABLE` |
| `contactos.py` | Descarga de la agenda por PBAP, caché en disco y búsqueda |
| `modem.py` | Descubrimiento y seguimiento del módem HFP de oFono |
| `llamadas.py` | Control de llamadas y traducción de los estados de oFono |
| `eventos.py` | Bus de eventos interno, difundido a los suscriptores del SSE |
| `bus.py` | Envoltorio fino sobre dbus-fast, con reintentos |
| `api.py` | La aplicación Starlette que se sirve por el socket unix |
| `servicio.py` | Ciclo de vida: conectar, reconectar, apagar limpio |

## Trampas que ya costaron tiempo

**Una sesión de PBAP muere con quien la creó.** obexd ata la vida de la sesión
al dueño del nombre de D-Bus que llamó a `CreateSession`. Con `busctl` desde el
shell la sesión se destruye en cuanto el comando termina, y el síntoma es que
`Select` falla con *"Method doesn't exist"* — que suena a versión equivocada de
la interfaz y no lo es. En el log de `obexd -d` se ve claramente:

```
session.c:owner_disconnected()
session.c:obc_session_shutdown()
```

Por eso la agenda se descarga desde una conexión **persistente**, la del propio
puente, y por eso no se puede depurar PBAP a base de `busctl`.

**La política de D-Bus de oFono deniega a uid 1000.** Hace falta el fichero
`/etc/dbus-1/system.d/ofono-agente-de-voz.conf`. Y cuidado al editarlo: XML
prohíbe dos guiones seguidos dentro de un comentario, así que escribir ahí una
línea de comando con opciones largas rompe el fichero, dbus lo descarta entero,
y el síntoma es el mismo `Access denied` de antes sin ninguna pista.

**No hay que poner `Online=true` en el módem.** El driver `hfp` de oFono no
tiene `set_online`: `Powered` pasa a `true` solo en cuanto se establece el
enlace HFP. Las guías que dicen lo contrario hablan de módems GSM de verdad.
