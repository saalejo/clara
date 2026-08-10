# voice-agent-botones

El **mando físico** del sistema: convierte los botones de la tarjeta de sonido
USB en el control del agente. Silenciar el micrófono, ajustar el volumen,
arrancar y parar servicios, contestar llamadas — sin túnel SSH y sin abrir el
panel.

```
tarjeta USB (PCM2902) ──HID──▶ /dev/input/event0
                                     │
                                     ▼  struct + add_reader
                          voice_agent_botones          ← este paquete, NATIVO
                                     │
                ┌────────────────────┼────────────────────┐
                ▼                    ▼                    ▼
        amixer / aplay      systemd (D-Bus sesión)   telefonia.sock
      micrófono, volumen    arrancar/parar/reiniciar  contestar/colgar
            pitidos                                   autocontestar
```

## El hardware, medido

La tarjeta es un **TI PCM2902** que se presenta como *C-Media USB PnP Sound
Device*. Además de las dos interfaces de audio expone una interfaz **HID**, y
por ahí llegan los botones:

```
/dev/input/by-id/usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-event-if03
```

Tiene cuatro botones físicos, y **no todos sirven**:

| Botón | Qué emite | Utilizable |
|---|---|---|
| Audio/altavoz | `KEY_MUTE` (113) | Sí, **pero sin mantenido** (ver abajo) |
| Rocker arriba | `KEY_VOLUMEUP` (115) | Sí, con mantenido |
| Rocker abajo | `KEY_VOLUMEDOWN` (114) | Sí, con mantenido |
| **Micrófono** | **nada** | **No.** Es hardware puro |

### Las tres trampas del hardware

**1. El botón de micrófono no existe para el software.** No emite evento HID ni
mueve ningún control de ALSA. Silencia (o no) dentro del códec, y desde fuera es
indistinguible de un botón desconectado. No se puede usar como señal, y por eso
el mute del micrófono está en el botón de audio y no en el que lleva el dibujo
del micrófono. Es confuso y no hay alternativa.

**2. `KEY_MUTE` no distingue mantener pulsado.** Medido: manda el par
pulsar/soltar **en el mismo microsegundo**, aunque lo aprietes tres segundos.

```
27.391s  MUTE   PULSA
27.391s  MUTE   suelta      <- pegados, tres veces de tres
39.839s  VOL-   PULSA
42.111s  VOL-   suelta      <- 2272 ms de duración real
```

Es el comportamiento habitual de la usage «Mute» de HID consumer: se manda como
un pulso, no como un estado. **Consecuencia de diseño: los niveles por duración
solo pueden vivir en el rocker de volumen.** MUTE es un botón de una sola
acción — y sale ganando, porque no paga latencia de detección de nada.

**3. El device no declara `EV_REP`** (`EV=13` = SYN|KEY|MSC), así que el kernel
no autorrepite: mantener VOL+ no sube el volumen solo. Si algún día hiciera
falta, habría que sintetizarlo con un temporizador.

Y una cuarta que no es del hardware sino del sistema: el device tiene handler
`kbd`, así que las teclas **también se inyectan en la consola virtual**. Por eso
se hace `EVIOCGRAB` por defecto. El precio está en la sección de diagnóstico.

## Por qué corre nativo y no en un contenedor

Dos razones, ambas insalvables:

1. **No vería los botones.** `/dev/input` no está en ninguna unidad de
   contenedor del proyecto, y montarlo traería el problema de que los enlaces
   estables de `/dev/input/by-id/` los crea udev en el anfitrión y no existen
   dentro.
2. **No podría arrancar ni parar el agente.** Gobernar el systemd del usuario
   exige hablar con el bus de sesión, y ahí la autenticación EXTERNAL contrasta
   el uid con `SO_PEERCRED`: en un contenedor rootless el proceso es uid 0 dentro
   pero 1000 fuera, y el bus responde `REJECTED EXTERNAL`. El panel lo resuelve
   con `--userns=keep-id`; un demonio nativo no tiene el problema.

Y una tercera, práctica: el código del contenedor está congelado y cualquier
cambio cuesta un `make build` de veinte minutos. Nativo se itera al instante.

De paso, ser nativo es lo que permite que **el agente no necesite ni una línea
de cambio**: el micrófono y el volumen se controlan en el mezclador de ALSA, no
dentro del pipeline de Pipecat.

## Por qué el micrófono se silencia en ALSA y no en el agente

`src/voice_agent/audio_gate.py` tiene una compuerta de micrófono que parecería el
sitio natural. No lo es, por tres razones:

1. **Haría falta un canal de control que el proyecto no tiene.** El agente lee su
   configuración una sola vez al arrancar, por decisión razonada, y no expone
   socket, señal ni endpoint. Habría que inventarlo.
2. **No funcionaría con el agente parado**, que es justo cuando más falta hace un
   botón físico.
3. **Cuesta un `make build` cada iteración.**

Y además resuelven cosas distintas: la compuerta silencia el micro mientras habla
el propio agente —dentro del pipeline y por milisegundos, para que no se
interrumpa a sí mismo— mientras que esto es un interruptor que se queda puesto
hasta que alguien lo quite. La compuerta **sí** está activa en esta placa, porque
el perfil es `speaker`; solo desaparece con `AUDIO_PROFILE=headset`, donde
interrumpir es lo que se quiere permitir.

En el mezclador, en cambio, es una orden y está medido que funciona: con el
agente en marcha y la captura abierta por `dsnoop`, `amixer -c Device sset Mic
nocap` lleva la grabación de `rms=92.2 / pico=403` a **`rms=0 / pico=0`**,
silencio digital absoluto.

Hay precedente en casa: `src/voice_agent/audio_devices.py` ya invoca `amixer`
por subprocess en su código de diagnóstico.

## Los pitidos

No hay pantalla, así que el único feedback posible es sonoro. Se generan con
`wave` y `math` de la biblioteca estándar la primera vez que hacen falta, en
`<DATA_DIR>/pitidos/`, y se reproducen con `aplay -D default`. No hay binarios
en el repositorio.

Tres detalles que no son cosméticos:

- **Rampa de 5 ms a la entrada y a la salida** de cada tono. Sin ella la onda se
  corta a mitad de ciclo y se oye un chasquido.
- **Amplitud máxima 0,25** del fondo de escala, para no saturar el `dmix` que
  comparte la tarjeta con el agente.
- **48 kHz estéreo**, que es el formato nativo de la tarjeta, para que la capa
  `plug` de `deploy/asound.conf` no tenga que convertir nada.

Y una propiedad afortunada: `docs/audio.md` documenta que **Silero está entrenado
para ignorar tonos puros** — el hallazgo que en su día invalidó una medición de
eco. Aquí juega a favor: los pitidos no disparan el VAD del agente.

## Diagnóstico

```
make botones           arranca en primer plano, con log a la consola
make botones-sonda     imprime los gestos detectados sin ejecutar nada
make botones-pitidos   regenera y reproduce el catálogo, para elegir de oído
make botones-logs      sigue el log del servicio
make install-botones   instala la unidad de systemd
```

**`make botones-sonda` tiene que parar el servicio primero**, y no es un
descuido: con `EVIOCGRAB` puesto, la unidad en marcha tiene el device en
exclusiva y cualquier otro lector ve silencio. El síntoma —«la sonda no detecta
nada»— parece una avería del hardware. Se puede desactivar con
`BOTONES_ACAPARAR=0`.

Los logs **no están en el journal del usuario**: esta Armbian no lo mantiene.
Van al del sistema, y como la unidad arranca con `uv run`, el proceso aparece
etiquetado como `uv`:

```
sudo journalctl _SYSTEMD_USER_UNIT=voice-agent-botones.service -f
```

## Permisos

Ninguno que haya que configurar. `/dev/input/event0` es `crw-rw---- root:input`
y el usuario ya pertenece al grupo `input`. **No hacen falta reglas de udev ni
`sudo`.**
