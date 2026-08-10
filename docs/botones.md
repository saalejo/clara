# El mando físico: los botones de la tarjeta de sonido

La tarjeta de sonido USB tiene cuatro botones que hasta ahora no hacían nada.
Este documento explica en qué se han convertido —silenciar el micrófono, mover el
volumen, contestar el teléfono, arrancar y parar el agente— y, sobre todo, **por
qué el reparto es el que es**, que lo decidió el hardware y no el gusto.

La idea de fondo: para lo más frecuente no debería hacer falta un túnel SSH ni
abrir el panel. Y hay un caso en que ninguno de los dos sirve —el agente
parado—, porque el panel gobierna el servicio pero el mando funciona **con el
agente muerto**, que es justo cuando más falta hace.

```
tarjeta USB (PCM2902) ──HID──▶ /dev/input/event0
                                     │
                                     ▼  struct + add_reader
                     voice-agent-botones          ← NATIVO, systemd --user
                                     │
                ┌────────────────────┼────────────────────┐
                ▼                    ▼                    ▼
        amixer / aplay      systemd (bus de sesión)   telefonia.sock
      micrófono, volumen    arrancar/parar/reiniciar  contestar/colgar
            pitidos                                   autocontestar
```

Corre nativo por las mismas razones que el puente de telefonía, y una más: un
contenedor **no vería los botones** —`/dev/input` no está montado en ninguna
unidad del proyecto, y los enlaces estables de `/dev/input/by-id/` los crea udev
en el anfitrión— y **no podría gobernar systemd**, porque la autenticación
EXTERNAL del bus de sesión contrasta el uid con `SO_PEERCRED` y en rootless el
proceso es uid 0 dentro pero 1000 fuera. El detalle está en
[`packages/botones/README.md`](../packages/botones/README.md).

## El hardware, medido

La tarjeta es un **TI PCM2902** que se presenta como *C-Media USB PnP Sound
Device*. Además de las dos interfaces de audio expone una interfaz **HID**, y por
ahí llegan los botones:

```
/dev/input/event0
/dev/input/by-id/usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-event-if03
```

Se usa siempre el enlace de `by-id/`. La numeración de los `eventN` depende del
orden de enumeración del USB, así que basta reconectar el hub para que el demonio
acabe leyendo el botón de recovery de la placa: un fallo mudo y desconcertante.

Cuatro botones, y **solo tres sirven**:

| Botón | Emite | Utilizable |
|---|---|---|
| Audio/altavoz | `KEY_MUTE` (113) | Sí, **pero sin mantenido** |
| Rocker arriba | `KEY_VOLUMEUP` (115) | Sí, con mantenido |
| Rocker abajo | `KEY_VOLUMEDOWN` (114) | Sí, con mantenido |
| **Micrófono** | **nada** | **No.** Es hardware puro |

### El botón de micrófono no existe para el software

No emite evento HID ni mueve ningún control de ALSA: silencia (o no) dentro del
códec, y desde fuera es **indistinguible de un botón desconectado**. No sirve
como señal de nada.

De ahí la consecuencia más incómoda de todo el diseño: **el mute del micrófono
está en el botón de audio, no en el que lleva el dibujo del micrófono.** Es
confuso y no hay alternativa.

### `KEY_MUTE` no distingue mantener pulsado

Manda el par pulsar/soltar **en el mismo microsegundo**, aunque lo aprietes tres
segundos. Medido en la placa, tres veces de tres:

```
27.391s  MUTE   PULSA
27.391s  MUTE   suelta      <- pegados
39.839s  VOL-   PULSA
42.111s  VOL-   suelta      <- 2272 ms de duración real
```

Es el comportamiento habitual de la usage «Mute» de HID consumer: se manda como
un pulso, no como un estado. El rocker sí informa de la duración real.

**Consecuencia de diseño: los tres niveles por duración solo pueden vivir en el
rocker.** MUTE es un botón de una sola acción — y sale ganando, porque al no
tener que contar clics en ninguna tecla, el clic de MUTE no paga latencia de
detección de doble clic. Y es la acción más frecuente, la única en la que la
latencia se nota: se silencia el micrófono porque alguien acaba de entrar en la
habitación *ahora*.

### El kernel no autorrepite

El device **no declara `EV_REP`** (`EV=13` = SYN|KEY|MSC), así que mantener VOL+
no sube el volumen solo. No es un fallo: es lo que permite que el mantenido
signifique otra cosa. Si algún día hiciera falta la autorrepetición, habría que
sintetizarla con un temporizador.

### La granularidad real es de 32 ms

El USB HID de esta tarjeta **se sondea cada 32 ms**. Todas las duraciones
medidas son múltiplos exactos de 32 —288, 1152, 1760, 1824, 4000, 6016 ms—, así
que el detector no puede distinguir nada más fino y **no tiene sentido afinar los
umbrales por debajo de 32 ms**.

### Las teclas se cuelan además en la consola

El device tiene handler `kbd`, así que lo que pulses llega **también a la consola
virtual** del anfitrión. Por eso el demonio hace `EVIOCGRAB` y pide el device en
exclusiva. Tiene un precio, y está en la sección de diagnóstico.

### Permisos: ninguno que configurar

`/dev/input/event0` es `crw-rw---- root:input` y el usuario `ember` ya pertenece
al grupo `input` (gid 995). **No hacen falta reglas de udev ni `sudo`.**

## El mapa de gestos

Tres niveles por duración, que **solo existen en el rocker**:

| Nivel | Duración | Aviso |
|---|---|---|
| 1 | hasta 700 ms | ninguno |
| 2 | hasta 2500 ms | un pip agudo al cruzar |
| 3 | por encima | un pip más agudo al cruzar |

El pip suena **mientras sigues pulsando**, no al soltar. Es lo que enseña la
interfaz sola: oyes que has entrado en el nivel 2 y decides si sigues apretando
hasta el 3 o sueltas ahí.

### Modo NORMAL (sin llamada, o sin puente de telefonía)

| Gesto | Qué hace |
|---|---|
| MUTE clic | Alternar el micrófono |
| VOL+ nivel 1 | Volumen +6 % |
| VOL− nivel 1 | Volumen −6 % |
| VOL+ nivel 2 | Autocontestar sí/no |
| VOL− nivel 2 | Modo «solo tarjeta de sonido»: parar o arrancar el puente |
| VOL+ nivel 3 | Arrancar o parar el agente |
| VOL− nivel 3 | Reiniciar el agente |

### Modo LLAMADA ENTRANTE (el teléfono está sonando)

| Gesto | Qué hace |
|---|---|
| MUTE clic | **Contestar** |
| VOL± nivel 1 | Volumen, como siempre |
| VOL+ nivel 2 | Rechazar |

### Modo LLAMADA EN CURSO

| Gesto | Qué hace |
|---|---|
| MUTE clic | Alternar el micrófono |
| VOL± nivel 1 | Volumen, como siempre |
| VOL+ nivel 2 | Colgar |

El modo lo decide el demonio escuchando el canal de eventos del puente. Sin
puente, siempre es NORMAL. **Durante una llamada, los gestos que tocan servicios
no existen**: el nivel 3 no está en el mapa a propósito.

Un gesto que no significa nada en el modo actual **suena a error**, no a
silencio: el silencio se confundiría con un botón que no funciona.

### Las tres reglas del mapa

Están en el docstring de
[`acciones.py`](../packages/botones/src/voice_agent_botones/acciones.py), que es
donde vive la tabla, y se repiten aquí porque son lo único que hay que memorizar:

1. **MUTE significa siempre «la acción obvia de este momento»**: silenciar el
   micrófono, o contestar si el teléfono está sonando. En una interfaz sin
   pantalla eso vale más que la simetría.
2. **El volumen es volumen en los tres modos.** El nivel 1 del rocker no cambia
   de significado nunca.
3. **Durante una llamada, los gestos que tocan servicios no existen.** Parar el
   agente mientras hablas por teléfono no es algo que nadie quiera de verdad, y
   el modo lo decide un estado invisible.

## Lo destructivo pide confirmación

Parar el agente, reiniciarlo y apagar la telefonía **no se ejecutan al soltar el
botón**. Suena un pitido de pregunta y hay que dar **un clic de MUTE en diez
segundos**. Cualquier gesto del rocker cancela — y cancela **sin ejecutar lo que
ese gesto pedía**, porque quien cancela no quiere además otra cosa.

**Arrancar no pide confirmación.** La asimetría es deliberada: arrancar es
inocuo, parar y reiniciar interrumpen una conversación en curso. Tratarlos igual
sería simetría mal entendida.

Se confirma con MUTE y no repitiendo el gesto porque repetir un mantenido de 2,5
segundos es incómodo, y porque MUTE ya es el botón de «sí, eso».

**Los diez segundos están medidos, y la primera cifra fue otra.** Con cuatro
segundos caducaron **cinco confirmaciones seguidas**: entre mantener el rocker
cinco segundos, soltarlo, oír el pitido y encontrar el otro botón, cuatro no dan.
Y el efecto secundario es peor que la caducidad: **el clic que llega tarde no se
pierde, cae como «silenciar micrófono»**, así que quien creía estar confirmando
un reinicio se queda con el agente vivo y el micrófono mudo.

## Los pitidos

No hay pantalla, así que el único feedback posible es sonoro. Y hay una razón por
la que no lo dice el agente con su voz: **el momento en que más falta hace el
feedback es cuando el agente está parado** —justo al pulsar el botón que lo
arranca—, y entonces no hay quien hable. Un pitido funciona siempre.

| Pitido | Cuándo | Cómo suena |
|---|---|---|
| `si` | Hecho, o algo que se ha encendido | agudo y corto |
| `no` | Algo que se ha apagado (micrófono, unidad parada) | grave, algo más largo |
| `nivel2` | Has cruzado la frontera del nivel 2, con el botón pulsado | pip muy corto |
| `nivel3` | Has cruzado la frontera del nivel 3 | pip muy corto, más agudo |
| `pregunta` | Un verbo destructivo espera confirmación | sube |
| `cancelado` | Confirmación cancelada o caducada, o dos teclas a la vez | baja |
| `error` | No se ha podido hacer, o el gesto no significa nada aquí | dos pulsos graves |
| `listo` | La unidad está arriba **de verdad** | arpegio |
| `tope` | El volumen ya estaba en el límite | dos pulsos iguales |

El idioma es consistente: **agudo es encendido, grave es apagado.** Importa en el
micrófono, porque un micrófono silenciado y olvidado hace que el agente parezca
averiado.

`tope` no es un adorno: sin él, una pulsación que no cambia nada parece un botón
roto.

### Cómo se generan

Con `wave` y `math` de la biblioteca estándar, la primera vez que hacen falta, en
`<DATA_DIR>/pitidos/`, y se reproducen con `aplay -D default`. No hay binarios en
el repositorio ni dependencias de audio. Tres detalles que no son cosméticos:

- **Rampa de 5 ms** a la entrada y a la salida de cada tono. Sin ella la onda se
  corta a mitad de ciclo y se oye un chasquido más fuerte que el propio pitido.
- **Amplitud máxima 0,25** del fondo de escala. La tarjeta se comparte con el
  agente por el `dmix` de [`deploy/asound.conf`](../deploy/asound.conf), que suma
  los flujos: al 100 % un pitido durante una frase del agente saturaría.
- **48 kHz estéreo**, el formato nativo de la tarjeta (ver
  [`docs/audio.md`](audio.md): la reproducción exige estéreo y solo admite
  44,1/48 kHz), para que la capa `plug` no tenga que convertir nada.

El nombre de cada fichero lleva un resumen de los parámetros de sus tonos, así
que cambiar una frecuencia genera un WAV nuevo en el siguiente arranque en vez de
reutilizar el viejo.

### Y una propiedad afortunada

[`docs/audio.md`](audio.md) documenta que **Silero está entrenado para ignorar
tonos puros** —el hallazgo que en su día invalidó una medición de eco hecha con
un seno de 440 Hz—. Aquí juega a favor: **los pitidos no disparan el VAD**, así
que se pueden emitir mientras el agente conversa sin provocar una interrupción
falsa.

### Dos pitidos por cada orden a un servicio

`StartUnit` vuelve en centésimas de segundo: devuelve el trabajo encolado, no el
resultado. Y para el agente **ni siquiera el `active` de systemd significa
listo**, porque su unidad es Quadlet con `--sdnotify=conmon` y se da por activa
en cuanto arranca el contenedor, no cuando el proceso ha cargado Whisper, Piper y
el modelo de embeddings. Medido en la placa: **24 segundos** entre el `active` de
systemd y el `estado_arranque.json` que el agente escribe al montar el pipeline.

Así que suena un `si` inmediato de «recibido» y un `listo` cuando la unidad está
arriba de verdad. Entre uno y otro la unidad queda **en vuelo**, y otra orden
sobre ella se rechaza con un `error`: es lo que evita encolar tres reinicios por
nerviosismo.

## Dos honestidades

Las dos son limitaciones reales, no matices, y conviene tenerlas claras antes de
usar el mando delante de otra persona.

### El micrófono que se silencia es el de la sala, no el de la llamada

El silencio se hace en el mezclador de ALSA, con `amixer -c Device sset Mic
nocap`. Está medido, con el agente en marcha y la captura ya abierta por
`dsnoop`: la grabación pasa de `rms=92.2 / pico=403` a **`rms=0 / pico=0`**,
silencio digital absoluto.

Pero eso silencia **el micrófono de la placa**. Durante una llamada, el audio va
por el móvil: **quien está al otro lado te sigue oyendo**. Lo que consigues
pulsando MUTE en una llamada es que el agente no te oiga, no que el otro no te
oiga.

Se hace en ALSA y no en el agente por tres razones: el agente lee su
configuración una sola vez al arrancar y no expone ningún canal de control;
tampoco funcionaría con el agente parado, que es cuando más falta hace; y cada
iteración costaría un `make build`. Ojo con no confundirlo con la compuerta de
`src/voice_agent/audio_gate.py`, que **sí** está activa en esta placa: aquella
silencia el micro mientras habla el propio agente, por milisegundos y dentro del
pipeline; esto es un interruptor que se queda puesto.

### Contestar y autocontestar significan DESCOLGAR, no conversar

El botón de contestar descuelga la llamada. **No pone al agente a hablar con
quien llama**, porque el audio de la llamada sigue yendo por el móvil: la fase 2
—el audio SCO— no está escrita.

Con el móvil en **altavoz** y cerca de la placa funciona igual, por acoplamiento
acústico: el micrófono de la sala oye a quien llama y el altavoz del agente le
llega al micrófono del móvil. Sin el altavoz puesto, el agente saluda a una
habitación vacía. El detalle, con la traza de una llamada real, está en
[`docs/telefonia.md`](telefonia.md).

## Puesta en marcha

```bash
make install-botones
systemctl --user enable --now voice-agent-botones
```

No hace falta nada más: ni reglas de udev, ni permisos, ni ordenar la unidad
contra el audio. Si la tarjeta todavía no ha enumerado, el demonio espera y lo
dice una sola vez. El pitido de `listo` al arrancar es la señal de que el mando
está atendiendo.

La configuración es opcional y vive en `.env.botones`
([plantilla comentada](../.env.botones.example)): umbrales de duración, paso de
volumen, controles del mezclador y qué unidades se pueden gobernar. Son ajustes
de hardware y de ergonomía, se tocan una vez al instalar, y por eso **no** están
en el panel.

## Diagnóstico

```
make botones           arranca el mando en primer plano, con log a la consola
make botones-sonda     imprime los gestos detectados sin ejecutar nada
make botones-pitidos   regenera y reproduce el catálogo, para afinarlo de oído
make botones-logs      sigue el log del servicio
make install-botones   instala la unidad de usuario
```

`make botones-sonda` es la herramienta de verdad: imprime cada gesto con su
duración real y avisa de los cruces de frontera. Sirve para comprobar el
cableado, para ajustar los umbrales y para descubrir qué emite un botón nuevo.

> **La trampa:** `make botones-sonda` **tiene que parar el servicio**, y no es un
> descuido. Con `EVIOCGRAB` puesto, la unidad en marcha tiene el device en
> **exclusiva** y cualquier otro lector ve silencio. El síntoma —«la sonda no
> detecta nada»— parece exactamente una avería del hardware. El objetivo para la
> unidad y la restaura al terminar, incluso si cortas con Ctrl-C. Si prefieres
> convivir con el servicio, arráncalo con `BOTONES_ACAPARAR=0`.

Los logs **no están en el journal de usuario**: esta Armbian no lo mantiene. Van
al del sistema, y como el `ExecStart` es `uv run`, el proceso aparece etiquetado
como `uv` y no con el nombre de la unidad:

```bash
make botones-logs
# sudo journalctl _SYSTEMD_USER_UNIT=voice-agent-botones.service -f
```

## Lo que el mando tolera sin morir

Un mando físico que se muere porque alguien ha tocado el hub USB no sirve de
nada, así que todo lo que puede fallar —el mezclador, systemd, el device, el
puente— **falla hacia un pitido de error**, nunca hacia una excepción.

- **La tarjeta desaparece del hub** (pasa: comparte hub con el dongle Bluetooth,
  ver [`docs/telefonia.md`](telefonia.md)). El lector reabre el device solo,
  vuelve a pedir el `EVIOCGRAB`, olvida el gesto a medias —el instante de inicio
  ya no significa nada— y **reaplica el silencio del micrófono**, que la tarjeta
  ha perdido al volver con los valores por defecto del driver.
- **El puente de telefonía no está.** Es el estado normal la mitad del tiempo,
  porque el modo «solo tarjeta de sonido» lo para a propósito. Se reintenta con
  retroceso exponencial y **solo se registra al cambiar de estado**: el mando
  está pensado para quedarse encendido meses, y un mensaje cada treinta segundos
  llenaría el journal.
- **Dos teclas a la vez.** Con tres botones juntos los acordes accidentales
  existen. Se anulan los dos gestos y suena `cancelado`: ejecutar lo que no era
  es peor que no hacer nada.
