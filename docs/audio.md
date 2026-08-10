# Audio

Este es el documento más importante del proyecto. El camino del audio es donde
más tiempo se pierde si algo no está bien montado, porque los fallos se
manifiestan como errores opacos de PortAudio en lugar de mensajes útiles.

## El problema

La NanoPi R4S **no tiene hardware de audio**: es una placa pensada como router.
El sonido lo aporta un adaptador USB con chip **PCM2902 de Texas Instruments**,
que Linux ve como `USB PnP Sound Device` en `hw:CARD=Device,DEV=0`.

Sus capacidades reales, consultadas al núcleo:

```bash
arecord -D hw:0,0 --dump-hw-params -d 1 /dev/null
```

```
FORMAT:   S16_LE          <- y nada más
RATE:     [44100 48000]   <- NO soporta 16000 Hz
CHANNELS: 1               <- captura mono
```

```bash
aplay -D hw:0,0 --dump-hw-params /dev/zero
```

```
RATE:     [44100 48000]
CHANNELS: 2               <- la reproducción EXIGE estéreo
```

Y lo que el agente necesita:

| | Necesita | La tarjeta ofrece |
|---|---|---|
| Frecuencia | 16 000 Hz (Silero VAD solo admite 8 k o 16 k; Whisper trabaja a 16 k) | 44 100 o 48 000 |
| Canales de salida | 1 (Piper genera mono) | 2 obligatoriamente |

**No hay ninguna combinación válida.** Si abres `hw:0,0` directamente desde
PyAudio pidiendo 16 kHz mono, obtienes un `Invalid sample rate` sin más
explicación, y es fácil perder una tarde pensando que el problema está en
Pipecat.

## La solución: una capa `plug` de ALSA

alsa-lib incluye un plugin, `plug`, que hace conversión automática de
frecuencia, formato y número de canales. El fichero
[`deploy/asound.conf`](../deploy/asound.conf) define un dispositivo `default`
que lo usa:

```
pcm.!default {
    type asym                  # entrada y salida con configuraciones distintas
    playback.pcm "va_out"      # plug -> dmix   -> hw (48 kHz, estéreo)
    capture.pcm  "va_in"       # plug -> dsnoop -> hw (48 kHz, mono)
}
```

Con eso, el proceso puede pedir 16 kHz mono en ambos sentidos y alsa-lib se
encarga de remuestrear y de duplicar el canal.

Encima del `plug` se usan **`dmix`** (salida) y **`dsnoop`** (entrada). No son
imprescindibles para la conversión, pero permiten que varios procesos abran la
tarjeta a la vez. Sin ellos, la tarjeta admite un único cliente y no podrías
lanzar un `arecord` de diagnóstico mientras el agente está conversando: te
respondería *Device or resource busy*.

El fichero se instala en dos sitios, y el mismo contenido en ambos, para que el
comportamiento sea idéntico dentro y fuera del contenedor:

```bash
sudo cp deploy/asound.conf /etc/asound.conf   # anfitrión
# y el Containerfile lo copia a /etc/asound.conf en la imagen
```

## Comprobar que funciona

Antes de depurar nada más arriba, valida el camino de audio en crudo:

```bash
# Grabar 3 s a 16 kHz mono a través de la capa plug
arecord -D default -f S16_LE -r 16000 -c 1 -d 3 /tmp/prueba.wav

# Debe pesar exactamente 96044 bytes: 3 × 16000 × 2 + 44 de cabecera
ls -l /tmp/prueba.wav

# Reproducir (la capa plug lo convierte a 48 kHz estéreo)
aplay -D default /tmp/prueba.wav
```

Y después, la misma comprobación desde Python, con los mismos dispositivos y
parámetros que usará el agente:

```bash
make audio-check         # graba, mide y reproduce
make audio-noise         # localiza por fases un zumbido o pitido en la salida
make audio-noise-levels  # prueba si algún nivel del mezclador lo atenúa
```

Ambos **paran el servicio si estaba corriendo y lo restauran al terminar**,
incluso si cortas con Ctrl-C. Necesitan la tarjeta para ellos solos: aunque la
capa `dmix`/`dsnoop` permita varios clientes, con el agente vivo reaccionaría a
lo que digas durante la prueba, y en el diagnóstico de ruido la fase «ningún
flujo abierto» sería directamente falsa.

Ese comando lista lo que ve PortAudio, resuelve los dispositivos configurados,
graba, mide el nivel de la señal y lo reproduce. Avisa si el nivel es demasiado
bajo para que salte el VAD o si la señal satura.

## Ganancia del micrófono

De fábrica, la ganancia de captura de este adaptador viene a cero, con lo que
no se oye nada y el VAD nunca se dispara. Se ajusta así:

```bash
amixer -c Device sset 'Mic' 12 cap      # rango 0–16
amixer -c Device sset 'Speaker' 75% unmute
sudo alsactl store                       # persistir tras el reinicio
```

Sin `alsactl store` los niveles se pierden al reiniciar. Comprueba el resultado
con `make audio-check`: interesa que el pico al hablar quede en torno al 30–70 %
del fondo de escala. Si satura, baja la ganancia: el recorte degrada mucho la
transcripción.

Hay además un control de **control automático de ganancia** (`Auto Gain
Control`), activado por defecto. Ayuda con voces a distinta distancia pero
"bombea" el ruido de fondo en los silencios. Si notas que el VAD se dispara
solo, prueba a desactivarlo:

```bash
amixer -c Device sset 'Auto Gain Control' off
```

## Selección de dispositivo

Pipecat pide el dispositivo a PortAudio por **índice numérico**, y los índices
no son estables entre reinicios. Por eso el proyecto los configura por nombre:

```
AUDIO_INPUT_DEVICE=default
AUDIO_OUTPUT_DEVICE=default
```

y `src/voice_agent/audio_devices.py` los resuelve al arrancar. Si el nombre no
existe, el error lista todos los dispositivos disponibles en vez de fallar con
un índice inválido.

Deja `default` salvo que sepas lo que haces: es el único que pasa por la capa
`plug`.

## El ruido de ALSA y JACK

Al enumerar dispositivos, PortAudio escupe decenas de líneas como:

```
ALSA lib pcm.c:2664:(snd_pcm_open_noupdate) Unknown PCM cards.pcm.rear
Cannot connect to server socket err = No such file or directory
jack server is not running or cannot be started
```

No son errores del programa: alsa-lib prueba PCMs de su configuración por
defecto (`surround51`, `rear`, `hdmi`) que este adaptador no implementa, y
PortAudio sondea también un servidor JACK que aquí no existe ni existirá.

Como todo eso se escribe desde C directamente al descriptor 2 del proceso, no
se puede filtrar con `logging`. `src/voice_agent/logging.py` lo resuelve con dos
mecanismos complementarios:

- `silence_alsa_warnings()` registra vía `ctypes` un manejador de error vacío en
  `libasound`. Es la solución específica y permanente para ALSA.
- `suppressed_stderr()` redirige el descriptor 2 a `/dev/null`, pero **solo**
  mientras se construye el objeto `PyAudio`, que es la ventana en la que aparece
  el ruido de JACK. Fuera de ahí el `stderr` queda intacto, para que ningún
  error real del programa quede oculto.

## Eco: por qué existen dos perfiles

Si el altavoz y el micrófono están abiertos en la misma sala, el micrófono capta
la voz sintetizada del propio agente.

> **Cuidado al medir esto.** La primera comprobación de este proyecto reprodujo
> un tono puro de 440 Hz y midió el micrófono: no detectó nada y se concluyó que
> no había acoplamiento. Era una prueba mal diseñada. Silero VAD está *entrenado*
> para ignorar tonos, y un seno puro tampoco excita el camino del micrófono como
> lo hace la voz. Repetida con voz real de Piper, la misma configuración daba un
> pico del 37 % del fondo de escala y confianza de VAD de 0.956. **Mide siempre
> con voz, no con tonos.**

El VAD interpreta esa voz filtrada como que el usuario ha empezado a hablar,
interrumpe la reproducción a media frase y, si el reconocedor llega a
transcribir algo, el agente acaba respondiéndose a sí mismo.

Ocurre también **con auriculares** si filtran algo, que es el caso del adaptador
barato de esta placa: no hace falta un altavoz abierto para sufrirlo.

La solución de libro es la **cancelación de eco acústico** (AEC). Pipecat no
incluye ninguna libre: sus filtros de audio son integraciones comerciales
(Krisp, Koala) o supresión de ruido (`rnnoise`), que no es lo mismo que
cancelación de eco.

Por eso el proyecto ofrece dos perfiles, en `AUDIO_PROFILE`:

| | `headset` | `speaker` |
|---|---|---|
| Situación | Aislamiento acústico real | Altavoz abierto, o auriculares que filtran |
| Interrupciones | Sí, puedes hablar encima | No, semidúplex |
| Mecanismo | Ninguno | Compuerta de micrófono + `AlwaysUserMuteStrategy` |
| Confianza del VAD | 0.70 | 0.85 |
| Volumen mínimo | 0.6 | 0.7 |
| Inicio / parada del VAD | 0.20 s / 0.20 s | 0.35 s / 0.60 s |

Si el agente se corta solo, cambia a `speaker`. Es el síntoma inequívoco.

### Por qué `AlwaysUserMuteStrategy` no basta

`AlwaysUserMuteStrategy` es la estrategia de Pipecat para ignorar al usuario
mientras el bot habla, y es lo primero que uno prueba. **No es suficiente**, y
conviene entender por qué: actúa en el agregador de contexto, que está *después*
del reconocedor de voz y después de que el VAD haya emitido sus eventos de
turno. Para cuando la estrategia decide ignorar al usuario, la interrupción ya
se ha propagado y ha cortado la reproducción.

Medido en una sesión real con el perfil `speaker` activo: nueve interrupciones
en veintitrés segundos, todas con el mismo patrón —`Bot started speaking`, y
exactamente 0.6 s después `User started speaking`—, sin una sola transcripción.

Tampoco sirve bajar la ganancia del micrófono. Silero decide por la **forma**
del habla, no por su intensidad: bajando la ganancia de 12/16 a 3/16 el pico
capturado cayó a la cuarta parte y la confianza del VAD se mantuvo en 0.92.

| Ganancia del micro | Pico capturado | Confianza VAD |
|---|---|---|
| 12/16 | 1040 (3.2 %) | 0.970 |
| 8/16 | 480 (1.5 %) | 0.920 |
| 3/16 | 231 (0.7 %) | 0.922 |

Subir `min_volume` sí influye —la condición real de Pipecat es
`confianza >= umbral **Y** volumen >= min_volume`— pero es un equilibrio
inestable: el volumen depende de la distancia al micrófono, de cuánto suba el
usuario el altavoz y de lo larga que sea la frase, porque el volumen se suaviza
exponencialmente y crece cuanto más habla el agente. Con ganancia 8/16 y
`min_volume=0.7`, medido en frío daba cero falsos positivos y en una sesión real
seguían saliendo cuatro en cuarenta segundos.

### La solución: cortar el audio en el origen

`src/voice_agent/audio_gate.py` instala un `BaseAudioFilter` en el transporte de
entrada, que se ejecuta **antes del VAD y antes de propagar nada aguas abajo**.
Mientras el agente habla, devuelve silencio: el VAD nunca llega a ver la voz del
agente, así que no hay nada que interrumpir ni que transcribir.

Un procesador colocado tras la salida de audio (`BotSpeechGateController`) abre
y cierra la compuerta al ver los frames `BotStartedSpeaking` y
`BotStoppedSpeaking`. La **cola de guarda** de medio segundo tras callar no es
un adorno: cuando Pipecat emite `BotStoppedSpeakingFrame` el audio sigue sonando,
porque quedan hasta 85 ms en el búfer de ALSA más la reverberación de la sala.

Resultado medido: de nueve interrupciones en veintitrés segundos a **cero en
sesenta**, descartando 230 bloques de audio mientras el agente hablaba.

Se ajusta con `MIC_GATE_HANGOVER_SECS`: súbelo si el agente aún se oye a sí
mismo al terminar de hablar, bájalo si tarda demasiado en volver a escucharte.

## Zumbido mientras el agente corre

Síntoma: se oye un pitido continuo por los auriculares mientras el servicio está
activo, y desaparece al pararlo.

**No lo genera el software.** Capturando por un bucle ALSA lo que el agente
envía a la tarjeta durante el silencio, todas las muestras son cero: silencio
digital perfecto.

Lo provoca **tener abierta la captura**. Con `make audio-noise`, que abre los
flujos por fases, en esta placa el zumbido aparece en la fase de «solo
micrófono» y en la de «los dos», pero **no** en la de «solo altavoz». Es
diafonía del PCM2902: el conversor de entrada, al activarse, inyecta ruido en el
amplificador de auriculares a través de la alimentación y la masa que comparten
dentro del mismo chip.

Desde el software no hay arreglo: el agente necesita el micrófono abierto
permanentemente, que es justo la condición que lo causa.

Tampoco lo hay desde el mezclador, y esto está **comprobado, no supuesto**. Con
`make audio-noise-levels`, que mantiene la captura abierta y va variando los
niveles, en este adaptador el zumbido **no se atenúa en ninguna fase, ni
siquiera con la salida silenciada por completo**. Que sobreviva al silenciado
significa que la interferencia se inyecta *después* del control de volumen: no
queda ningún punto donde intervenir.

Merece la pena entender por qué ni siquiera un "modo pulsar para hablar"
ayudaría de forma útil: el zumbido molesta precisamente **durante el silencio**,
que es cuando el agente está escuchando. Cerrar el micrófono mientras el agente
habla —lo único que se puede predecir— lo silenciaría justo en el momento en que
nadie lo nota.

Las salidas son, por tanto, físicas:

- Mover el adaptador a un puerto **USB 2.0** en lugar del 3.0, que es
  notablemente más ruidoso.
- Alejarlo de la placa con un alargador corto.
- Alimentarlo desde un **hub con fuente propia**, que rompe el camino de masa
  compartido.
- Separar entrada y salida en **dos adaptadores distintos**: micrófono en uno,
  auriculares en otro. Es la solución definitiva, porque elimina la causa de
  raíz —ya no hay un conversor de entrada compartiendo chip con el amplificador
  de salida— y es la recomendada si el zumbido molesta. El proyecto ya lo admite
  **sin tocar código**: `AUDIO_INPUT_DEVICE` y `AUDIO_OUTPUT_DEVICE` son
  independientes y se resuelven por nombre.

  Con dos adaptadores, `make audio-list` mostrará ambos y basta con poner en
  `.env` el nombre de cada uno, por ejemplo::

      AUDIO_INPUT_DEVICE=USB PnP Sound Device
      AUDIO_OUTPUT_DEVICE=USB Audio CODEC

  Ojo: cada adaptador tendrá sus propias limitaciones de frecuencia y canales,
  así que habrá que revisar `deploy/asound.conf`, que hoy asume una sola tarjeta
  para ambas direcciones.

### Si quieres interrumpir de verdad con altavoz

Necesitas cancelación de eco acústico real, y eso significa hardware con AEC
integrado —un altavoz de conferencia USB, por ejemplo— o una de las
integraciones comerciales de Pipecat (Krisp, Koala).

## Audio dentro del contenedor

Dos requisitos, ambos poco evidentes:

**1. Acceso a `/dev/snd` en modo rootless.** Los nodos son `root:audio` con
permisos `660`. En rootless, el GID del grupo `audio` del anfitrión no se mapea
dentro del espacio de nombres de usuario del contenedor, así que el proceso no
tendría permiso. La solución:

```
--device /dev/snd --group-add keep-groups
```

`keep-groups` hace que crun conserve los grupos suplementarios del usuario del
anfitrión, que ya pertenece a `audio`. Requiere que el runtime sea **crun**
(esta placa lo usa; con `runc` no funciona).

**2. IPC compartido.** `dmix` y `dsnoop` se coordinan mediante memoria
compartida de System V. Si el contenedor tiene su propio espacio de nombres de
IPC, su mezclador y el del anfitrión no se ven, y el segundo que intente abrir
la tarjeta recibirá *dispositivo ocupado*. Con `--ipc=host` comparten el mismo
espacio y puedes diagnosticar desde fuera mientras el agente conversa dentro.

Comprobación desde el contenedor:

```bash
make audio-check-container
```
