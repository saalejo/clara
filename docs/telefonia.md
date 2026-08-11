# Telefonía: la placa como manos libres del móvil

El agente puede anunciar quién llama, contestar, colgar, marcar y buscar en la
agenda del móvil. Este documento explica cómo, y sobre todo **por qué está
montado así**, que es la parte que no se deduce leyendo el código.

## La idea: quién hace de qué

El perfil Bluetooth que usa un manos libres de coche se llama **HFP**
(*Hands-Free Profile*) y define exactamente dos papeles:

| Papel | Quién | Qué hace |
|---|---|---|
| **AG** — *Audio Gateway* | El móvil | Tiene la línea, la SIM y la red |
| **HF** — *Hands-Free unit* | **La placa** | Manda órdenes y recibe avisos |

Es fácil confundirse aquí porque el móvil es el aparato "listo" y la placa el
accesorio, pero en HFP el que manda es el **HF**: el manos libres es quien dice
"contesta" y el móvil quien obedece. La placa hace de manos libres.

Por debajo, todo lo que parece telefonía son **comandos AT viajando por
RFCOMM**: `ATA` para contestar, `AT+CHUP` para colgar, `RING` y `+CLIP` cuando
entra una llamada. Nada de eso lo escribimos nosotros: lo hace oFono.

## El reparto del trabajo

```
móvil  ──HFP───▶ bluetoothd ──▶ ofonod        (bus del SISTEMA)
       ──PBAP──▶ obexd                        (bus de SESIÓN)
                        │
                        ▼  dbus-fast
                voice-agent-telefonia          ← NATIVO, systemd --user
                        │
                        ▼  HTTP + SSE sobre data/run/telefonia.sock
                   voice-agent                 ← dentro del contenedor
```

| Pieza | Papel |
|---|---|
| `bluetoothd` | El emparejamiento y el enlace. Guarda las claves en `/var/lib/bluetooth` |
| `ofonod` | Habla HFP y lo traduce a una API de D-Bus decente (`org.ofono.VoiceCallManager`) |
| `obexd` | Descarga la agenda por PBAP. Vive en el bus de **sesión**, no en el del sistema |
| `voice-agent-telefonia` | Los orquesta y lo publica todo por un socket unix |
| `voice-agent` | Siete herramientas que el modelo puede llamar |

## Por qué el puente es nativo y no un contenedor

Es la única unidad del proyecto que no corre en Podman, así que conviene
justificarlo. Tres razones, de más a menos decisiva:

1. **La autenticación de D-Bus.** El bus contrasta el uid que dice el cliente
   con `SO_PEERCRED`. En un contenedor rootless el proceso es uid 0 dentro pero
   1000 fuera, y el bus responde `REJECTED EXTERNAL`. El panel ya se peleó con
   esto y lo resolvió con `--userns=keep-id` (ver el docstring de
   `packages/panel/src/voice_agent_panel/control.py`), pero aquí harían falta
   **dos** buses —el del sistema para oFono y el de sesión para obexd— y encima
   el del sistema está gobernado por la política de oFono.
2. **Iterar.** Reconstruir la imagen del agente son diez minutos, y esto se
   escribe probando cosas contra un móvil de verdad.
3. **El audio de mañana.** El socket SCO de la fase 2 llega como descriptor de
   fichero por D-Bus. Meterlo en un contenedor es trabajo extra sin ganancia.

**Dónde están sus logs**, que tampoco es donde uno los busca: `journalctl
--user -u voice-agent-telefonia` sale **vacío**, igual que con el agente y el
panel. Esta Armbian no mantiene journal de usuario, así que todo acaba en el
del sistema; y allí el proceso aparece etiquetado como `uv`, no con el nombre
de la unidad, porque el `ExecStart` es `uv run`. Se filtra por unidad:

```bash
make telefonia-logs
# sudo journalctl _SYSTEMD_USER_UNIT=voice-agent-telefonia.service -f
```

## Por qué un socket unix bajo `data/`

Porque **el contenedor del agente ya monta `data/` en `/data`**. El socket en
`data/run/telefonia.sock` se ve dentro como `/data/run/telefonia.sock` sin
tocar una sola línea de `deploy/voice-agent.container`. Comprobado en la placa
antes de escribir el primer módulo.

Además un socket unix puede transportar un **descriptor de fichero** por
`SCM_RIGHTS`, que es lo que la fase 2 necesita. HTTP sobre loopback no puede, y
elegirlo hoy obligaría a rehacer el transporte, el cliente y sus tests mañana.

Y no hay trampa de uid: el agente corre **sin** `--userns=keep-id`, así que
dentro es uid 0, que en rootless mapea al 1000 del anfitrión; un socket de
`ember` se ve como de root ahí dentro y `connect()` funciona.

## Por qué dos librerías de D-Bus en el mismo repositorio

El panel usa `jeepney`; el puente usa `dbus-fast`. No es un descuido:

```console
$ grep -rn "enable_fds" .venv/lib/python3.13/site-packages/jeepney/io/
io/threading.py:107:def open_dbus_connection(bus='SESSION', enable_fds=False, …)
io/trio.py:69:    def __init__(self, socket, enable_fds=False)
```

`jeepney/io/asyncio.py` no aparece: **su backend de asyncio no puede recibir
descriptores de fichero**. La fase 2 necesita justamente eso, y además
necesita *exportar* un objeto D-Bus porque es oFono quien nos llama a nosotros.
El panel se queda con jeepney porque hace llamadas bloqueantes a systemd y
funciona; cambiarlo sería riesgo gratuito.

**No las unifiques** sin volver a ejecutar ese `grep`.

## Puesta en marcha

### 1. Paquetes y servicios

```bash
sudo apt-get install -y bluez bluez-obexd ofono bluez-tools
sudo systemctl enable --now bluetooth ofono
```

`obexd` no hay que habilitarlo: se activa por D-Bus en el bus de sesión.

### 2. Que la placa parezca un manos libres

En `/etc/bluetooth/main.conf` (el original queda en `main.conf.orig`):

```ini
[General]
Name = agente-de-voz
Class = 0x200408          # mayor 0x04 Audio/Vídeo + menor manos libres
DiscoverableTimeout = 0
PairableTimeout = 0
FastConnectable = true

[Policy]
AutoEnable = true
```

La clase importa: es lo que hace que Android nos trate como un kit de coche y
ofrezca la casilla de compartir contactos al emparejar.

Dos correcciones a la mitología de internet, comprobadas contra el
`src/main.conf` de BlueZ 5.82: **`Enable=Source,Sink,Media,Socket` no existe**
(es de BlueZ 4 y no hace nada) y **`Experimental = true` no hace falta** para
HFP ni para PBAP.

### 3. Dejar que el puente hable con oFono

La política que trae oFono deniega a todo el mundo salvo a root, y su cláusula
`at_console` **no salva**: con systemd, dbus la resuelve preguntándole a logind
si el uid tiene un asiento, y ni una sesión SSH ni un servicio de usuario con
*linger* lo tienen. Hace falta
`/etc/dbus-1/system.d/ofono-agente-de-voz.conf` con una `<policy user="ember">`
que permita `send_destination="org.ofono"`.

> **Trampa cara:** XML prohíbe dos guiones seguidos dentro de un comentario.
> Escribir ahí una línea de comando con opciones largas rompe el fichero, dbus
> lo **descarta entero** y el síntoma es exactamente el mismo `Access denied`
> de antes, sin ninguna pista. Solo aparece en el journal de dbus como
> `not well-formed (invalid token)`.

Comprobación decisiva, como usuario normal y sin `sudo`:

```bash
busctl --system call org.ofono / org.ofono.Manager GetModems
```

### 4. Emparejar el móvil

**Desde el móvil, no desde la placa**: el diálogo de Android es donde aparece la
casilla de acceso a contactos.

```bash
sudo bt-agent --capability=NoInputNoOutput &   # acepta sin humano delante
bluetoothctl power on
bluetoothctl pairable on
bluetoothctl discoverable on
#  --> en el móvil: emparejar con "agente-de-voz",
#      MARCANDO "permitir acceso a contactos y registro de llamadas"
bluetoothctl trust AA:BB:CC:DD:EE:FF
bluetoothctl discoverable off
```

`bt-agent` no es opcional en una placa sin pantalla. Android pide confirmar un
código por comparación numérica, y sin un agente que conteste el emparejamiento
muere a los 30 segundos con `Simple Pairing Complete: LMP Response Timeout`.
`bluetoothctl` registra un agente `DisplayYesNo` que espera a que **alguien
teclee**, y ahí no hay nadie.

En `bluetoothctl info` tienen que aparecer los dos UUID:

| UUID | Qué es | Para qué |
|---|---|---|
| `0000111f-…` | Handsfree Audio Gateway | oFono crea el módem al verlo |
| `0000112f-…` | Phonebook Access Server | obexd puede pedir la agenda |

Si falta `112f`, el móvil no está compartiendo contactos: revisa el interruptor
en los ajustes Bluetooth del propio teléfono.

### 5. El puente

```bash
make install-telefonia
systemctl --user enable --now voice-agent-telefonia
make telefonia-estado
```

## Cómo se enciende sola

No hay nada que activar. `bot.py` sondea `GET /salud` sobre el socket al
arrancar, con un segundo de paciencia:

* **contesta** → se construye el cliente, las siete herramientas se mezclan en
  el catálogo, se añade un párrafo al prompt del sistema y arranca la tarea de
  anuncios.
* **no contesta** → una línea en el log y el agente sigue exactamente como
  antes de que la telefonía existiera.

`TELEFONIA_MODO` permite forzarlo: `off` no sondea nunca, `on` activa las
herramientas aunque el puente no esté todavía (útil para arrancarlo después del
agente). El sondeo ocurre **solo al arrancar**; si levantas el puente después,
reinicia el agente.

Todo esto es aditivo por diseño: las herramientas de teléfono viven en
`HERRAMIENTAS_TELEFONIA`, **no** en `HERRAMIENTAS`, y `tests/test_telefonia_apagada.py`
vigila que sin puente el catálogo sea el de siempre.

## El anuncio de una llamada entrante

Es el único sitio donde algo entra en la conversación sin que nadie haya
hablado. Por cada llamada se encolan **dos frames**, y el orden importa:

```python
TTSSpeakFrame(text="Te llama Ana. ¿Respondo?", append_to_context=True)
LLMMessagesAppendFrame(messages=[{"role": "system", "content": "..."}])
```

El primero lleva **texto fijo**, no una respuesta del modelo: el anuncio es lo
único del sistema con una restricción dura de tiempo, porque un móvil suena unos
veinticinco segundos. Una ida y vuelta a OpenRouter se come un segundo largo y,
peor, el modelo podría decidir preguntar algo en vez de avisar — que es el bucle
de saludos que `bot.py` ya documenta.

El segundo es lo menos evidente y lo más valioso: **no lleva un `LLMRunFrame`
detrás**. Mete el aviso en el historial *sin disparar un turno*, así que el
agente no habla dos veces, pero cuando la persona diga "sí", el modelo ya sabe
qué significa y qué herramienta llamar.

Desde fuera del pipeline solo se encolan `DataFrame`s y siempre con
`queue_frames`; es la generalización de la lección de `hooks.py`.

## Autocontestar

El puente puede descolgar solo las llamadas entrantes. Se consulta y se cambia
por su API:

```bash
make telefonia-autocontestar            # consultar
make telefonia-autocontestar ON=1       # activar
make telefonia-autocontestar OFF=1      # desactivar
```

Por debajo son `GET /autocontestar` y `POST /autocontestar`, que acepta
`{"activo": true|false}` o `{"alternar": true}`. El `alternar` no es azúcar: lo
usa el mando físico, y sin él tendría que leer y luego escribir, con el panel
capaz de colarse entre las dos operaciones. Un solo método evita ese TOCTOU y le
ahorra un viaje por el socket.

Desde el mando físico es **VOL+ mantenido hasta el nivel 2**, ver
[`docs/botones.md`](botones.md).

**La preferencia se persiste**, en `<DATA_DIR>/telefonia/preferencias.json`.
Porque el interruptor se pone con un botón y se olvida: si no sobreviviera al
reinicio, un corte de luz dejaría el autocontestar apagado sin que nadie se
enterase, y de las dos formas de equivocarse la que sorprende es que deje de
contestar cuando lo habías dejado puesto.

**Es un fichero propio y no `config/settings.json`**, y eso es deliberado: ese
otro es el contrato **panel → agente** y lo escribe entero el exportador del
panel. Meter aquí un segundo escritor del mismo fichero es pedir una carrera —los
dos leen, los dos modifican su parte, el último en escribir se lleva por delante
lo del otro—. Con un fichero por dueño no hay nada que coordinar.

**Hay un margen de 2 segundos antes de descolgar**, y no es cero a propósito: da
tiempo a rechazar la llamada a mano, y deja que la máquina de estados de oFono se
asiente antes de mandarle otra orden. En esos segundos la persona puede haber
cogido el teléfono o el otro haber colgado, así que el estado se relee antes de
actuar; que la llamada ya no sea contestable es la carrera **normal**, no un
error.

### Autocontestar significa DESCOLGAR, no conversar

Esto hay que decirlo con todas las letras, porque el nombre promete más de lo que
hay: **el audio de la llamada sigue yendo por el móvil.** La fase 2 —el socket
SCO— no está empezada, así que descolgar no pone al agente en la línea.

Con el móvil en **altavoz** y cerca de la placa funciona igual, por
**acoplamiento acústico**: el micrófono de la sala oye a quien llama, y el altavoz
del agente le llega al micrófono del móvil. Es un montaje tosco pero real, y es
la única forma de que hoy funcione. **Sin el altavoz puesto, el agente saluda a
una habitación vacía.**

Verificado con una llamada real, de principio a fin:

```
02:55:01.035  llamada_entrante
02:55:03.039  Autocontestando la llamada de Mamá Nora    (2,004 s de margen)
02:55:03.071  Llamada contestada: voicecall01            (32 ms el Answer por D-Bus)
02:55:03.269  saludando en la llamada
02:56:56.510  llamada_terminada                          (1m53s de llamada)
```

### El id `actual` no estaba implementado

Los clientes de este puente usan el id literal `actual` para decir «la llamada de
ahora, la que sea». Era el contrato que `src/voice_agent/telefonia.py` daba por
supuesto —lo escribe a fuego— pero que **nadie traducía**: `Telefono._buscar`
buscaba por id exacto, `actual` no es el id de ninguna llamada y la respuesta era
un 404 **siempre**.

Se descubrió probando el mando físico con una llamada de verdad, y la consecuencia
era peor que un botón roto: **`contestar_llamada` del agente nunca pudo contestar
una llamada.** Pedirle en voz alta que cogiera el teléfono no podía funcionar.

Ya está arreglado en `api.py`, donde `actual` se traduce a `None` y `_buscar` lo
interpreta como «la única llamada viva». Merece la pena saber por qué los otros
dos verbos se salvaban, para no «arreglarlos» de más: **colgar** porque su
cliente sin id llama a `/llamadas/colgar-todas`, y **los tonos** porque su
manejador no mira el id de la ruta —`enviar_tonos` va al módem, no a una llamada
concreta—.

## La agenda

Se descarga por **PBAP** con `obexd`, al conectar el móvil y luego cada
`TELEFONIA_CONTACTOS_TTL_HORAS`. Medido en esta placa: **1179 contactos en unos
9 segundos**.

Esos 9 segundos dependen de un detalle: se piden solo los campos `N`, `FN` y
`TEL`. Sin ese filtro, el móvil manda también las **fotos** de los contactos,
que son el 95 % del peso.

### Las dos trampas de PBAP

**Una sesión muere con quien la creó.** obexd ata la vida de la sesión al dueño
del nombre de D-Bus que llamó a `CreateSession`. Con `busctl` desde el shell,
cada comando es una conexión distinta, así que la sesión se destruye antes del
comando siguiente y `Select` falla con *"Method doesn't exist"* — que suena a
versión equivocada de la interfaz y no lo es. En `obexd -d` se ve el motivo real:

```
session.c:owner_disconnected()
session.c:obc_session_shutdown()
```

Por eso **PBAP no se puede depurar a base de `busctl`**, y por eso la descarga
ocurre sobre la conexión persistente del puente.

**obexd se activa con retraso.** La primera llamada lo arranca, pero el nombre
`org.bluez.obex` aparece en el bus *antes* de que el objeto tenga registrada la
interfaz `Client1`, así que esa primera llamada falla con el mismo *"doesn't
exist"*. `DescargaPBAP` espera a que la interfaz esté de verdad.

**Y una tercera, en las vCard:** PBAP entrega vCard 2.1 por defecto, que no sabe
de UTF-8 y manda los acentos como `QUOTED-PRINTABLE`. Sin descodificarlo, media
agenda se llena de `Mar=C3=ADa` y el buscador deja de encontrar a la familia. No
falla nada visiblemente: simplemente no aparece nadie.

## Llamadas de WhatsApp (y de cualquier app)

**Sí funcionan para recibir, y no para llamar.** Comprobado con una llamada de
WhatsApp real contra este montaje.

Las apps de VoIP se integran en Android como `ConnectionService` autogestionado,
así que el sistema las trata como llamadas de verdad y **se las cuenta al manos
libres**. Por eso el agente anuncia una llamada de WhatsApp igual que una del
operador, y `contestar_llamada` y `colgar_llamada` funcionan con ellas.

Lo capturado, de principio a fin:

```
[telefonía] llamada_entrante     numero=10000000  estado=en_curso  entrante=True
telefonia_anuncios:_anunciar - [telefonía] anunciando llamada de ...
[telefonía] llamada_terminada
```

Dos límites que conviene tener claros:

**Llegan en `alerting` O en `dialing`, según le dé.** Medido en dos llamadas
reales contra el mismo TECNO POVA 5 Pro: la primera apareció en `alerting` y la
segunda en `dialing`. La segunda destapó un fallo, porque `dialing` era el único
estado que el puente daba por saliente: **la llamada no se anunció**. Quien
llamaba oyó el saludo del agente, pero en la habitación nadie se enteró.

Lo que separa una de app entrante de una saliente de verdad es el
**identificador**: la saliente lleva el número que se marcó, y la de app lleva el
relleno. Así que `dialing` **con relleno** se trata como entrante. Está en
`_parece_entrante`, con el caso que se equivoca a propósito escrito al lado.

Y para que un fallo así no vuelva a ser indiagnosticable, `CallAdded` ahora deja
en el log el `State` **en crudo** y la dirección que dedujo de él. Es un dato que
solo existe durante unas décimas.

**No hay número, y por tanto no hay nombre.** El identificador de llamada de HFP
solo entiende de números de teléfono, así que Android manda un relleno —aquí
`10000000`— y no el número de quien llama. Eso significa que **una llamada de
app no se puede resolver contra la agenda**: el agente dirá «te llama alguien
por una aplicación» y no «te llama Ana». No es un fallo que se pueda arreglar
desde la placa: el dato no viaja.

Ese relleno está en `RELLENOS_SIN_IDENTIFICAR`. Si algún día aparece otro valor
—depende de la versión de Android—, se añade ahí y el agente deja de leerlo en
voz alta.

**Se declaran contestadas SIN estarlo.** Aquí ponía que «llegan ya contestadas»,
a partir de los 121 ms medidos entre `llamada_entrante` y `llamada_contestada`.
Ese número es real, pero la conclusión era falsa, y costó tres llamadas más
descubrirlo.

Lo que pasa de verdad: el móvil declara la llamada `active` por HFP a los
~140 ms **mientras la aplicación sigue timbrando y nadie la ha cogido**.
Comprobado con dos llamadas seguidas que se dejaron sonar a propósito: las dos
duraron exactamente 20 segundos —el timbre agotándose—, el agente saludó en las
dos, y quien llamaba **no oyó nada**, porque no había ninguna llamada.

La sonda de RFCOMM (ver más abajo) cerró la duda que quedaba: **no es una
traducción de oFono, es el móvil**. Hablándole a pelo por AT, una llamada de
WhatsApp entrante y sin tocar emitió, en 900 ms: `+CIEV: 2,2` (marcando),
`+CIEV: 2,3` (sonando), `+CIEV: 1,1` (contestada) — con la aplicación todavía
timbrando. Y `AT+CLCC` la describía como `1,0,0,0,0,"10000000"`: dirección
**saliente**, estado **activa**, número de relleno. Ni un solo instante en
`incoming` (`dir=1,stat=4`). La misma tarde, una llamada del operador salió de
libro: `+CIEV: 2,1`, `RING`, `+CLIP` con número y nombre de la agenda, y
`+CLCC: 1,1,4,...` — entrante de verdad hasta que se descolgó en el móvil.

Y no es una rareza del TECNO. Un **realme 15T** emparejado a propósito para
comprobarlo hizo exactamente lo mismo, paso a paso y en los mismos ~900 ms,
hasta con el mismo relleno `10000000`. Dos fabricantes distintos con el mismo
guion es el guion de Android: las llamadas de `ConnectionService` autogestionado
se cuentan al manos libres como salientes ya contestadas, da igual el móvil.

Tres cosas se explican de golpe con eso:

* **El autocontestar no descuelga nunca una llamada de app.** A los 2 segundos
  de margen, oFono ya dice `active`, que no está en `ESTADOS_CONTESTABLES`, y
  `_autocontestar` se retira. No es un fallo de la preferencia.
* **El saludo se gastaba en el vacío.** Por eso `_saludar_en_llamada` ahora no
  hace nada si `Llamada.es_de_app`.
* **No hay forma de descolgarla desde la placa.** `Answer()` de oFono exige
  `incoming`, y este móvil entrega estas llamadas como `dialing` o `alerting`
  y luego `active`. Nunca `incoming`. Describe una llamada que ENTRA con los
  indicadores de una que SALE.

Así que con una llamada de app el botón de contestar **no tiene nada que hacer**
—ni el gesto, ni la herramienta—. Solo sirve para colgarlas. Lo único honesto
que puede hacer el agente es anunciarla.

### La tentación que hay que resistir

Al ver que estas llamadas aparecen en `alerting` o en `dialing` —estados que el
estándar reserva para las salientes— la reacción natural es meterlos en
`ESTADOS_CONTESTABLES` para poder descolgarlas. **No sirve de nada.** oFono
rechaza `Answer()` con cualquier estado que no sea `incoming`; está en su
`src/voicecall.c`:

```c
if (call->status != CALL_STATUS_INCOMING)
    return __ofono_error_failed(msg);
```

`HoldAndAnswer` exige a su vez una llamada en `waiting`, y `org.ofono.Handsfree`
solo expone `GetProperties`, `SetProperty` y `RequestPhoneNumber`. Mientras oFono
sea el dueño del canal RFCOMM no hay otra vía de mandar el `ATA`.

Quedaba la duda de si un `ATA` a pelo —sin oFono y su política— descolgaría.
La sonda la mató sin necesidad de mandarlo: `ATA` descuelga **la llamada en
`incoming`**, y este móvil no pone una llamada de app en `incoming` ni un
instante — a los 900 ms ya la declara `active` él solo. No hay estado al que
dirigir el `ATA`; mandarlo solo puede devolver `ERROR`. El muro no es la
política de oFono: es que el móvil describe una llamada que ENTRA con los
indicadores de una que SALE ya contestada.

**No se puede llamar por WhatsApp.** Marcar en HFP es el comando `ATD`, que
siempre abre una llamada **del operador**. No existe una orden de HFP que
signifique "llama a esta persona por WhatsApp", así que `llamar_a_contacto` y
`llamar_a_numero` gastan minutos de la línea y necesitan cobertura.

Hay una vía indirecta, no implementada: este móvil anuncia la capacidad
`voice-recognition` de HFP, y oFono la expone como la propiedad
`org.ofono.Handsfree.VoiceRecognition`. Ponerla a `true` despierta el asistente
del teléfono, que sí sabe llamar por WhatsApp. Sería el agente hablándole al
asistente del móvil: una cadena larga y frágil, pero es la única puerta que hay.

## Cuando no hay cobertura

Merece una nota porque explica síntomas desconcertantes. `org.ofono.NetworkRegistration`
dice si el móvil está registrado y con cuánta señal:

```bash
busctl --system call org.ofono /hfp/org/bluez/hci0/dev_AA_BB_... \
    org.ofono.NetworkRegistration GetProperties
```

En esta placa se ha visto pasar de `"Status": "unregistered"` con nombre vacío a
`"Status": "registered", "Name": "TIGO", "Strength": 40` en cuestión de minutos.
Con la señal así:

* Las llamadas **del operador** ni entran ni salen, y el puente no ve
  absolutamente nada —ni un evento— porque el móvil tampoco tiene nada que
  contar. Un silencio total del puente durante una llamada de prueba es, casi
  siempre, esto y no un fallo del código.
* Las llamadas **de WhatsApp** siguen funcionando, porque van por la red de
  datos o por wifi. En un sitio con mala cobertura, son las únicas que el agente
  va a ver.

Antes de dar por roto el puente, mira la cobertura.

## Buscar contactos dichos en voz alta

Los nombres propios son el peor caso posible para Whisper `tiny`: son justo las
palabras que un modelo pequeño no puede adivinar por contexto. Contra eso hay
tres capas en `normaliza.py`, de más fiable a menos: normalizar (sin tildes, sin
ruido), comparar por trozos ("ana pe" → "Ana Pérez") y una **clave fonética
española** que pliega las confusiones reales del castellano —b/v, seseo, yeísmo,
ge/jota, hache muda— y rescata "Varvara" → "Bárbara".

La regla de cuándo se puede actuar sin preguntar es deliberadamente estricta:

> el primero puntúa al menos 80 **y** le saca 15 puntos al segundo.

Porque el precio de los dos errores no es el mismo. Preguntar "¿Ana Pérez o Ana
Gómez?" cuando estaba claro cuesta una frase; llamar a la Ana equivocada cuesta
una llamada a alguien que no esperaba tu voz.

## Las herramientas, y por qué son esas siete

Ver `docs/herramientas.md`, sección *Herramientas que actúan sobre el mundo*.
En corto: el pestillo `confirmado`, la desambiguación la hace el puente y no el
modelo, y el modelo nunca ve la agenda entera.

## La sonda: hablarle al móvil a pelo

`packages/telefonia/src/voice_agent_telefonia/sonda.py` abre el RFCOMM contra
el AG del móvil sin pasar por oFono, hace el saludo mínimo de HFP hasta
`AT+CMER` —sin el cual no llega un solo `+CIEV`— más `AT+CLIP=1`, y pinta todo
el tráfico con marca de tiempo. Mientras detecta una llamada sondea `AT+CLCC`
cada segundo, que es el comando que no miente: da la dirección y el estado
reales de cada llamada. Por stdin admite órdenes a mano (`ATA`, `AT+CHUP`).

Es **intrusiva**: el móvil acepta una sola conexión HFP, así que hay que
apartar a oFono y devolverlo después:

```bash
sudo systemctl stop ofono && make telefonia-sonda; sudo systemctl start ofono
```

Necesita `TELEFONIA_BLUETOOTH_ADDRESS` con la MAC del móvil (el puente no la
necesita porque descubre el módem por D-Bus; la sonda no tiene a quién
preguntarle). El canal RFCOMM del AG **cambia de móvil a móvil** —el TECNO lo
tiene en el 3, un realme 15T en el 4— y se ajusta con `TELEFONIA_CANAL_HFP`.
Lo dice el SDP del propio móvil:

```bash
sdptool search --bdaddr <MAC> HFAG | grep -A2 RFCOMM
```

**La trampa: conectar demasiado pronto deja la sonda sorda.** Medido en la
primera ejecución real (2026-08-03): nada más parar oFono, el primer intento
devolvió `ECONNREFUSED`, y el segundo, cinco segundos después, fue peor — el
móvil **aceptó la conexión y no contestó ni al `AT+BRSF`**, sin conceder
créditos RFCOMM, y acabó tirando el enlace ACL entero. La sonda quedó
bloqueada en un `sendall` que nunca termina. Es residuo de la sesión HFP de
oFono que el móvil aún no ha soltado. Con **ocho segundos de respiro** entre
parar oFono y conectar, el mismo móvil hizo el saludo completo a la primera.
Dos consecuencias prácticas: darle ese respiro siempre, y envolver la sonda en
`timeout -k` si se lanza desatendida, porque colgada no muere sola.

Un dato que ahorra una hipótesis: el móvil habla AT con un socket RFCOMM
crudo aunque, con oFono parado, la placa no publique **ningún registro SDP de
manos libres**. El TECNO no lo comprueba.

Lo que la sonda dejó medido está arriba, en la sección de llamadas de app: la
llamada del operador sale de libro, y la de WhatsApp nace mintiendo de fábrica.

## El dongle

El puesto desde el 2026-08-03 es un **TP-Link UB500** (chip Realtek RTL8761BU),
justo el sustituto estándar que recomendaba la sección de abajo cuando el
anterior demostró no valer. Pasa las dos comprobaciones que decidían:

```bash
sudo hciconfig -a hci0 | grep "SCO MTU"      # 255:12 — doce búferes SCO
sudo btmgmt info | grep wide-band-speech      # aparece, y además activado
```

Es decir: CVSD posible y mSBC (16 kHz) sobre la mesa. Sigue valiendo la
advertencia de abajo: SCO capaz es condición **necesaria, no suficiente**.

Tres cosas aprendidas al ponerlo:

* **La trampa del firmware.** Recién pinchado, el dongle sale en `lsusb` pero
  `hci0` queda `DOWN` con dirección `00:00:00:00:00:00`. No está roto: el
  kernel pide `rtl_bt/rtl8761bu_fw.bin` y Armbian no lo trae — `dmesg` dice
  `firmware file rtl8761bu_fw not found`. El arreglo: bajar `rtl8761bu_fw.bin`
  y `rtl8761bu_config.bin` del repositorio linux-firmware de kernel.org a
  `/lib/firmware/rtl_bt/` y recargar `btusb`. Que el `_config.bin` pese 6
  bytes no es una descarga rota: los config de Realtek son así de pequeños.
* **Cambiar de dongle cambia la dirección del adaptador** (esta es
  `30:68:93:E4:03:59`), y los emparejamientos viven en
  `/var/lib/bluetooth/<dirección del adaptador>/`, así que se pierden todos:
  hubo que volver a emparejar el móvil. Para aceptar el emparejamiento sin
  teclado ni pantalla hace falta un agente en la placa; sirvió un
  `bluetoothctl` con `agent off` seguido de `agent NoInputNoOutput` (el
  `agent off` es obligatorio: `bluetoothctl` registra el suyo al arrancar y el
  segundo registro falla en silencio). En el mismo directorio vive el **alias
  del adaptador**, así que el dongle nuevo volvió a anunciarse como
  `nanopi-r4s`: el plugin de hostname de BlueZ pisa el `Name` de `main.conf`
  salvo que haya alias guardado. Se restaura con
  `bluetoothctl system-alias agente-de-voz`.
* **Ya no comparte hub con la tarjeta de sonido.** Al irse el pendrive del
  almacén de Podman quedó un puerto libre y el dongle va ahí, directo; la
  tarjeta de sonido cuelga de otro bus. La avería que documentaba esta sección
  —enchufar el dongle directo hacía desaparecer la tarjeta— era del reparto
  anterior de puertos. Si algún día vuelve a faltar un puerto,
  `cat /proc/asound/cards` sigue siendo el diagnóstico rápido.

### El anterior rechazaba el SCO. No era una duda: quedó medido

El dongle que hubo hasta el 2026-08-03 era un **clon de CSR sin marca**. Para
la fase 1 daba igual —emparejamiento, SDP y RFCOMM son tráfico ACL normal y
funcionaban, aunque **se reenumeraba solo de vez en cuando** (`usb 7-1.1: USB
disconnect` y a los dos segundos un `new full-speed USB device`; el bucle de
vigilancia del puente lo toleraba). Pero para el audio no había duda: **ese
dongle no iba a transportar una llamada.**

Empezando por lo que dice el kernel, que además identifica el chip de verdad:

```
Bluetooth: hci0: CSR: Unbranded CSR clone detected; adding workarounds and force-suspending once...
Bluetooth: hci0: Couldn't suspend the device for our Barrot 8041a02 receive-issue workaround
```

Es un **Barrot 8041a02**, un clon falso de CSR que el propio kernel intenta
parchear **y no consigue**. Los tres síntomas, de menos a más concluyente:

1. `hciconfig -a hci0` reporta `SCO MTU: 48:0` — **cero búferes** SCO.
2. `btmgmt info` **no lista `wide-band-speech`**, así que mSBC queda descartado de
   entrada.
3. Y lo definitivo: **rechazó el canal de audio en las tres llamadas de prueba**,
   con `Bluetooth: hci0: connection err: -111` (ECONNREFUSED) en el instante
   exacto en que cada llamada empezó a sonar — 01:59:23, 02:04:41 y 02:13:06.

**Cómo comprobar un dongle antes de creerle** — así se eligió el UB500, y así
se valida el siguiente si este muere. Estas dos órdenes deciden:

```bash
sudo hciconfig -a hci0 | grep "SCO MTU"          # el SEGUNDO número tiene que ser > 0
sudo btmgmt info | grep wide-band-speech          # tiene que aparecer
```

Y que quede claro para no crear una expectativa falsa: un dongle capaz de SCO es
condición **necesaria pero no suficiente**. Lo que faltaba encima es la fase 2.

## Fase 2: el audio de la llamada en la placa

La primera etapa ya está hecha y **medida con llamadas reales**: el puente
recibe el socket SCO de cada llamada y lo demuestra con un modo de eco. Está
en `packages/telefonia/src/voice_agent_telefonia/audio_sco.py`; se activa con
`TELEFONIA_AUDIO_MODO=eco` y por defecto va en `off` — y ese `off` no registra
nada, porque registrar el agente de audio **le quita el sonido de la llamada
al móvil**: quien registre carga con el audio.

Cómo funciona, con las dos trampas que costaron cuatro llamadas de prueba:

* `org.ofono.HandsfreeAudioManager` en `/` del servicio `org.ofono` ofrece
  `Register(o path, ay codecs)`. Nosotros exportamos un
  `org.ofono.HandsfreeAudioAgent` cuyo `NewConnection(o card, h fd, y codec)`
  recibe el socket SCO como descriptor. Códecs, de `src/hfp.h`: `CVSD = 0x01`,
  `MSBC = 0x02`. El puente registra **solo CVSD** de momento.
* **Trampa 1: el descriptor NO llega conectado — llega en «defer setup».** Es
  el agente quien completa la aceptación, con un `recv` sobre el fd (PulseAudio
  hace `recv(fd, NULL, 0, 0)` en su backend de oFono). Sin ese recv, nadie
  manda el `Accept Synchronous Connection Request` al controlador, el móvil
  espera 20 segundos exactos y btmon lo despide con `Connection Accept Timeout
  Exceeded (0x10)`. Los síntomas engañan: `ENOTCONN` al escribir y ni un byte
  al leer, con la llamada perfectamente viva.
* **Y desde Python, ese recv tiene que ser de longitud > 0** — un
  `sock.recv(0)` puede no llegar a emitir el syscall y la aceptación no
  ocurre. Lo que funciona: `sock.recv(1, MSG_PEEK)`, que fuerza el syscall sin
  consumir audio. La señal de que la conexión quedó de verdad: el
  `getsockopt(SOL_SCO)` pasa de reportar el MTU del controlador (255) al
  negociado de la conexión (48).
* **Trampa 2: la recepción no arranca hasta que el anfitrión transmite.** Es
  la razón por la que PulseAudio escribe silencio continuamente. El puente
  «ceba» el canal con paquetes de silencio de 48 B cada 3 ms hasta que llega
  el primer paquete del móvil; desde ahí, el reloj de la recepción marca el
  paso.
* El fd es un socket `AF_BLUETOOTH`/`BTPROTO_SCO` de tipo `SOCK_SEQPACKET`.
  Con CVSD transporta PCM crudo de 8 kHz mono de 16 bits y **hay que leer y
  escribir en bloques exactamente del tamaño del MTU** o el kernel descarta
  paquetes en silencio.

Cifras del eco medidas en la placa (2026-08-04, llamada real de 17 s): primer
paquete a los 0,04 s de confirmar el defer setup, paquetes de 48 B a ~330 por
segundo, 15.900–15.960 B/s sostenidos — la tasa teórica de 8 kHz × 2 B es
16.000. El canal se cierra solo al colgar y la tarea de eco muere con él.

### El contestador, funcionando

El 2026-08-04 de madrugada el agente mantuvo su **primera conversación
telefónica completa**: 104 segundos, varios turnos, saludo a los ~2 s de
descolgar y respuestas con TTFB por debajo del segundo y medio. El montaje:
`TELEFONIA_AUDIO_MODO=agente` en el puente, que entrega el descriptor por
`SCM_RIGHTS` (`run/telefonia-audio.sock`); en el agente, `ClienteAudioSCO`
lo recoge y `telefonia_llamada.py` monta un pipeline por llamada
(STT→LLM→TTS sobre el `TransporteSCO`), con los servicios **precargados
al arrancar** (`ServiciosDeLlamada`) y repuestos **al colgar**, nunca durante
la llamada, que compite por la CPU.

La deriva de reloj que este documento anunciaba como "la ingeniería de
verdad" se disolvió por diseño: **la salida va esclava del reloj de la
radio** — por cada paquete que entra sale exactamente uno, audio del TTS si
hay en cola y silencio si no, con contrapresión de medio segundo. El mismo
tique gobierna los dos sentidos y no queda nada que compensar.

Las cuatro trampas del montaje, cada una pagada con una llamada muda:

* **Los paquetes de 3 ms ahogan el pipeline.** 330 frames por segundo entre
  cinco procesadores de Python son >1.600 pases de frame por segundo en esta
  placa: la cola crece más rápido de lo que drena y el saludo llegó a pasarse
  14 s sepultado. La entrada agrupa ahora bloques de 20 ms (320 B), como el
  micrófono de la sala.
* **La salida necesita su `set_transport_ready`.** Sin él, el escritor del
  transporte de salida no arranca y el TTS sintetiza hacia el vacío. La
  entrada lo llamaba; la salida no lo heredó de ningún sitio.
* **Cada pipeline con su frecuencia.** Deepgram transcribe el flujo crudo:
  construido con los 16 kHz de la sala pero alimentado con los 8 kHz del SCO,
  la voz le llega a media velocidad y no entiende nada — con el VAD detectando
  turnos perfectamente, que es lo que despista. `build_stt`/`build_tts`
  aceptan la frecuencia y la llamada pide la del CVSD.
* **El saludo doble de la sala.** El saludo de fase 1 (`_saludar_en_llamada`)
  usa el mismo texto por el altavoz: se depuró como un cruce de audio entre
  pipelines que nunca existió. Con audio de llamada, el saludo de sala va
  vacío (la voz se desactiva; el aviso al modelo se mantiene).

Y una trampa de las PRUEBAS, que costó más que todas las del código: los
**procesos duplicados**. Dos puentes o dos agentes a la vez hacen ping-pong
por el socket de audio —cada conexión nueva expulsa a la anterior— y el
síntoma (EPIPE, consumidores fantasma, rechazos) parece cualquier cosa menos
lo que es. Antes de diagnosticar nada: `pgrep` y contar.

Lo que le queda al contestador, ya como pulido:

* **Cerrar el micrófono de la sala durante la llamada** — el mecanismo está
  en `src/voice_agent/audio_gate.py`; sería engancharlo a
  `EstadoLlamada.EN_CURSO`.
* **Honestidad en el prompt de llamada**: sin herramientas montadas, el
  modelo inventó con aplomo temperatura, IP y hasta una tarjeta WiFi que la
  placa no tiene. O se le montan las herramientas, o se le prohíbe fingir
  que las tiene.
* **mSBC** para ancho de banda, cuando el resto esté pulido.
* El modo por defecto sigue siendo `off`: encender `agente` en producción es
  una decisión pendiente, porque le quita el audio de las llamadas al móvil.

## El historial de pacientes: la memoria entre llamadas

Desde el 10-08 el agente recuerda a quien llama, por número de teléfono, en
una base SQLite del volumen de datos (`data/evaluaciones/historial.sqlite3`,
módulo `voice_agent_core.historial`). El flujo completo:

* **Al montar la llamada** (entrante o misión saliente) se registra la fila
  con el mismo `id_llamada` que llevan la traza, la alerta y el resumen — al
  principio y no al colgar, para que una llamada caída también cuente. Cada
  llamada telefónica estrena además su propia `TrazaLlamada`, lo que cerró el
  hueco de las «alertas sin traza».
* **La ficha del número entra en el prompt** (`PROMPT_HISTORIAL_PREVIO`):
  cuántas veces llamó y qué quedó de la última — con la orden de dar
  continuidad pero confirmar, porque el historial es del teléfono, no de la
  voz que contesta. Vale para entrantes Y para misiones: probado en vivo,
  Clara devolvió la llamada diciendo «hace un rato se cortó nuestra
  comunicación».
* **Las anotaciones llegan solas**: `registrar_alerta` fija el color del
  triaje en la fila en el instante de decidirse; `finalizar_llamada` completa
  procedimiento, decisión y próximos pasos; y si la llamada muere sin
  despedida, el **resumen de respaldo** (`voice_agent/respaldo.py`, ahora
  compartido con el pipeline web y enganchado también al desmontaje del SCO)
  anota lo que haya.
* Los números de relleno (WhatsApp `10000000`) y el número oculto **no abren
  ficha**: mezclarían pacientes distintos. Ver `numero_identificable`.
* El modelo puede pedir más con la herramienta `historial_paciente`, y el
  panel lo enseña en su página **Pacientes** (solo lectura).

Y una trampa nueva de las misiones, pagada con una llamada real: **el móvil
abre el SCO en el instante de marcar** —por él viaja el tono de llamada—, así
que la confirmación `EN_CURSO` tarda lo que tarde el humano en descolgar
(dieciséis segundos, medidos). La correlación de `MisionesLlamada` espera
mientras la llamada registrada siga sonando y solo se rinde si desaparece, si
otra llamada en curso reclama el audio o si la misión caduca; la versión de
reintentos fijos (1,5 s) atendió la misión como entrante y el vigilante la
colgó a los sesenta segundos en mitad de la conversación.
