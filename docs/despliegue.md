# Despliegue con Podman

El agente se empaqueta en un contenedor y se ejecuta con **Podman sin
privilegios de administrador** (*rootless*), gestionado por systemd a través de
una unidad **Quadlet**.

Son **dos imágenes y tres unidades**:

| Imagen | Tamaño | Construcción | Qué lleva |
|---|---|---|---|
| `voice-agent` | 1,69 GB | ~20 min | Pipecat, chromadb, fastembed, PyAudio compilado |
| `voice-agent-panel` | 280 MB | ~1,5 min | Django y poco más |

| Unidad | Imagen | Qué hace |
|---|---|---|
| `voice-agent.service` | agente | El agente, siempre en marcha |
| `voice-agent-panel.service` | panel | El panel web, en `127.0.0.1:8080` |
| `voice-agent-ingest.service` | agente | Reindexa el RAG; de un solo uso |

Están separadas para no pagar veinte minutos de construcción cada vez que se
toca una plantilla del panel. Ver [`panel.md`](panel.md).

## Preparación de la placa (una sola vez)

```bash
sudo apt install -y portaudio19-dev libportaudio2 python3-dev build-essential
sudo cp deploy/asound.conf /etc/asound.conf     # imprescindible, ver docs/audio.md
loginctl enable-linger $USER                    # el servicio sobrevive al cierre de sesión
sudo systemctl add-wants multi-user.target network-online.target   # ver más abajo
```

`enable-linger` es necesario para que un servicio de usuario siga corriendo sin
sesión abierta. Sin él, el agente se pararía al desconectar el SSH.

`add-wants network-online.target` no es opcional aunque lo parezca. Quadlet
inyecta `Wants=`/`After=podman-user-wait-network-online.service` en **toda**
unidad de contenedor, y ese ayudante es literalmente un
`until systemctl is-active network-online.target` con noventa segundos de
timeout. En esta placa el target estaba habilitado pero **nadie lo arrastraba**,
así que jamás se activaba y el ayudante agotaba el timeout en cada arranque.
Medido: `systemctl --user restart voice-agent` pasó de **101 s a 11,7 s**, y del
arranque del kernel al agente activo, de **211 s a 20 s**.

### El almacén de Podman vive en la propia tarjeta

En `~/.local/share/containers/storage`, el sitio por defecto de Podman en modo
rootless. Ocupa unos 5,6 GB con 85 imágenes.

**Hasta julio de 2026 vivía en un pendrive montado en `/mnt/almacen`**, porque la
microSD de 15 GB no daba ni para el pico transitorio de un `podman build`, que
supera los 8 GB. Con la tarjeta de 58 GB dejó de hacer falta: se migró el sistema
a una tarjeta mayor, el almacén volvió a la raíz, y desaparecieron a la vez el
override de `graphroot` en `storage.conf`, los tres `RequiresMountsFor=/mnt/almacen`
y la entrada del `fstab`.

**La trampa que dejó esa migración, y que conviene conocer si alguna vez se vuelve
a mover el almacén:** la base de datos de libpod (`db.sql`, dentro del propio
almacén) **graba la ruta absoluta con la que se creó**. Al moverla, Podman se
niega a arrancar con un error explícito —`database static dir ... does not match
our static dir ...`— y ninguna unidad levanta. Se arregla apartando ese fichero
para que se recree: solo contiene metadatos de contenedores y pods, no imágenes, y
como todas las unidades Quadlet corren con `--rm` no hay nada persistente que
perder.

```bash
systemctl --user stop voice-agent voice-agent-panel voice-agent-telefonia
mv ~/.local/share/containers/storage/db.sql{,.viejo}
podman images   # tienen que seguir estando las 85
```

## Construir la imagen

```bash
make build
```

Una sola etapa sobre `python:3.13-slim-trixie`, y **es deliberado**.

Lo natural sería construir en dos etapas: compilar en una imagen con
herramientas y copiar solo el entorno virtual a una imagen limpia, ahorrando
unos 300 MB de compilador y cabeceras. Se hizo así al principio y **no funciona
en esta placa**.

El entorno virtual pesa 1.1 GB. Pipecat arrastra `llvmlite` (168 MB), `scipy`
(101 MB, vía `pyloudnorm`), las bibliotecas de PyAV (54 MB) y el cliente de
Kubernetes (41 MB), además de lo que sí se usa: `ctranslate2`, `onnxruntime`,
`chromadb_rust_bindings` y `piper`. Copiar eso entre etapas obliga a Podman a
tenerlo tres veces a la vez —en la imagen de construcción, como blob en disco y
desempaquetado en la capa nueva—, con un pico transitorio de más de 4 GB.

En una microSD de 15 GB, con el sistema ocupando casi 5 y el proyecto otros 1.6
entre entorno virtual y modelos, ese pico no cabe. **Falló cuatro veces
seguidas**, siempre en el mismo `COPY --from=builder`, incluso arrancando con
9 GB libres:

```
unpacking failed: write /app/.venv/.../libscipy_openblas.so: no space left on device
```

En una sola etapa no hay copia: el entorno virtual se crea donde se queda. La
imagen final pasa de 1.31 a **1.67 GB**, que es un precio pequeño a cambio de que
la construcción no dependa de tener 4 GB de holgura transitoria. Construye a la
primera con 8 GB libres.

Se mantiene la instalación en **dos pasos** (`uv sync --no-install-project` con
solo los manifiestos, y después el código), que es lo que permite reutilizar la
capa cara mientras no cambien las dependencias.

En esta placa la primera construcción tarda del orden de veinte minutos, casi
todo compilando PyAudio y desempaquetando onnxruntime, ChromaDB y CTranslate2.

Reconstruir tras un cambio de código baja a unos **diez minutos**, y eso
depende de un detalle del `Containerfile` que conviene no romper: la
instalación va en **dos pasos**. Primero se copian solo `pyproject.toml` y
`uv.lock` y se ejecuta `uv sync --no-install-project`, que instala todo el árbol
de terceros sin necesitar el código; después se copia `src` y se sincroniza otra
vez, lo que ya solo instala el propio paquete. Así la capa cara depende
únicamente de los manifiestos.

Si se copiara `src` antes del primer `uv sync`, cambiar una línea de código
invalidaría esa capa y habría que recompilar PyAudio entero cada vez.

Diez minutos siguen pareciendo muchos para un cambio de una línea, y lo son: el
caché ahorra la resolución y la compilación de dependencias, pero **no** el
resto. Copiar el entorno virtual de 1.1 GB de la etapa de construcción a la de
ejecución y confirmar una imagen de 1.31 GB cuesta varios minutos en una
microSD, por mucho que las nueve capas anteriores se reutilicen. Para iterar
sobre el código, usa `make run` en nativo y deja el contenedor para desplegar.

Los modelos **no** van dentro de la imagen: se descargan a `data/models`, que es
un volumen. Eso mantiene la imagen pequeña y hace que reconstruirla no obligue a
volver a bajar cientos de megabytes.

## Ejecutar

```bash
make models            # una vez: descarga Whisper, la voz de Piper y los embeddings
make ingest            # una vez: indexa corpus/
make run-container
```

Lo que hace por debajo:

```bash
podman run --rm -it --name voice-agent \
    --device /dev/snd \
    --group-add keep-groups \
    --ipc=host \
    --env-file .env \
    -v ./data:/data:Z \
    -v ./corpus:/corpus:ro,Z \
    --memory 2g \
    localhost/voice-agent:latest
```

### Las dos opciones que no son obvias

**`--group-add keep-groups`.** Los nodos de `/dev/snd` son `root:audio` con
permisos `660`. En modo rootless, el GID del grupo `audio` del anfitrión no se
mapea dentro del espacio de nombres de usuario del contenedor, así que el
proceso no tendría permiso para abrirlos por mucho que le pases `--device`.
`keep-groups` hace que **crun** conserve los grupos suplementarios del usuario
del anfitrión, que ya pertenece a `audio`. Es específico de crun; con `runc` no
funciona.

**`--ipc=host`.** Los plugins `dmix` y `dsnoop` de ALSA se coordinan mediante
memoria compartida de System V. Con el espacio de IPC aislado, el mezclador del
contenedor y el del anfitrión no se ven y el segundo que abra la tarjeta recibe
*dispositivo ocupado*. Compartiendo el IPC puedes lanzar un `arecord` de
diagnóstico desde fuera mientras el agente conversa dentro.

### Sobre el usuario del contenedor

El proceso corre como `root` **dentro** del contenedor, lo que en rootless
significa que en el anfitrión es tu propio usuario sin privilegios. Es lo que
permite escribir en el volumen `data/` sin recurrir a `--userns=keep-id`. No hay
escalada de privilegios: fuera del contenedor no es root de nada.

## Instalarlo como servicio

```bash
make install-service
systemctl --user enable --now voice-agent
```

`make install-service` copia `deploy/voice-agent.container` a
`~/.config/containers/systemd/`, sustituyendo la ruta del proyecto, y recarga
systemd. **Quadlet** es la integración nativa de Podman 5 con systemd: se
escribe un fichero `.container` y systemd deriva el servicio automáticamente.
Sustituye a `podman generate systemd`, que está obsoleto, y evita depender de
podman-compose.

Operación habitual:

```bash
systemctl --user status voice-agent
systemctl --user restart voice-agent
make service-logs      # lo que escribe el agente
make service-events    # arranques, paradas y fallos de la unidad
```

### Dónde están los logs, que no es donde uno los busca

Hay dos flujos distintos y conviene no confundirlos:

| Qué quieres ver | Campo del journal | Comando |
|---|---|---|
| Todo, entrelazado | ambos | `make service-logs` |
| Lo que escribe el agente | `_SYSTEMD_USER_UNIT` | `journalctl _SYSTEMD_USER_UNIT=voice-agent.service -f` |
| Arranques, paradas y fallos | `USER_UNIT` | `make service-events` |

El error natural es probar `journalctl --user -u voice-agent -f` esperando ver
al agente hablar, y encontrarlo **completamente vacío**: "No journal files were
found". Hay dos motivos, y conviene conocer los dos.

Primero, systemd no ejecuta el programa: ejecuta un `podman run -d` que lo lanza
en segundo plano, así que la salida del agente nunca pasa por la unidad. Podman
la recoge con su driver de logs, que por defecto es `journald`.

Segundo, esta placa **no persiste el journal de usuario**: `journald.conf` está
en `Storage=auto` y no existe `/var/log/journal`, de modo que todo —incluidos
los eventos de las unidades de usuario— acaba en el journal del sistema. Por eso
los comandos de arriba no llevan `--user`.

Se leen **sin `sudo`** siempre que el usuario pertenezca al grupo `adm` o a
`systemd-journal`; si no, añade `sudo`.

Tampoco sirve `podman logs`: Quadlet añade `--rm`, así que el contenedor se
destruye al parar el servicio y se lleva sus logs por delante, justo cuando
querrías leerlos para saber por qué se cayó. Comprobado reiniciando el servicio:
el journal pasó de 99 a 121 líneas conservando el histórico, mientras
`podman logs` solo mostraba las 22 de la vida actual del contenedor.

La unidad usa `TimeoutStartSec=300` porque cargar Whisper, Piper y el modelo de
embeddings lleva unos 40 segundos en esta placa y el valor por defecto de
systemd daría el arranque por fallido.

## El puente de telefonía: la unidad que NO es un contenedor

Si la placa tiene un móvil emparejado, hay una tercera unidad, y es distinta de
las otras dos:

```bash
make install-telefonia
systemctl --user enable --now voice-agent-telefonia
```

`deploy/voice-agent-telefonia.service` es un servicio de usuario **nativo**, no
una unidad Quadlet. Corre `uv run` directamente sobre el código del proyecto, sin
imagen y sin Podman. El porqué está en [`telefonia.md`](telefonia.md); en corto,
la autenticación EXTERNAL de D-Bus contra dos buses distintos, los diez minutos
que cuesta reconstruir la imagen del agente, y el descriptor de fichero del audio
SCO de la fase 2.

Tres consecuencias prácticas:

**Sus logs tampoco están donde uno los busca.** `journalctl --user -u
voice-agent-telefonia` sale **vacío**: esta placa no mantiene journal de
usuario. Van al journal del **sistema**, y ahí el proceso aparece con la
etiqueta `uv` —no con el nombre de la unidad— porque el `ExecStart` es
`uv run`. Hay que filtrar por unidad:

```bash
make telefonia-logs
# sudo journalctl _SYSTEMD_USER_UNIT=voice-agent-telefonia.service -f
```

**Un cambio en `packages/telefonia` no necesita `make build`.** Basta reiniciar:

```bash
systemctl --user restart voice-agent-telefonia
```

Solo hace falta reconstruir la imagen cuando se toca `src/voice_agent` o el
`uv.lock`.

**No lleva `After=bluetooth.service`, y no es un olvido.** Es una unidad de
usuario y `bluetooth.service` es del sistema: el gestor de usuario no la conoce
y la dependencia se ignora **en silencio**. El puente arranca sin bus y se
engancha solo cuando aparece.

El panel puede arrancarlo, pararlo y reiniciarlo como a las otras dos: está en
`UNIDADES_PERMITIDAS`.

## El panel y la reindexación

```bash
cp .env.panel.example .env.panel && chmod 600 .env.panel   # rellena la clave y el usuario
make build-panel
make install-panel        # instala voice-agent-panel y voice-agent-ingest
systemctl --user start voice-agent-panel
ssh -L 8080:localhost:8080 nanopi          # y luego http://localhost:8080/panel/
```

Tres detalles de la unidad del panel que no son obvios:

**`--userns=keep-id`, y no es opcional.** El panel gobierna el servicio hablando
con systemd por su bus de sesión. La autenticación EXTERNAL de D-Bus manda el uid
que el cliente cree tener y el servidor lo contrasta con `SO_PEERCRED`; en un
contenedor rootless por defecto el proceso es uid 0 dentro pero el kernel reporta
1000 fuera, y el bus responde `REJECTED EXTERNAL`. Con `keep-id` coinciden.

Su precio: **el primer arranque tarda unos tres minutos**, porque Podman crea una
copia de las capas con los uid remapeados. De ahí el `TimeoutStartSec=300`; los
siguientes tardan unos seis segundos. Un `Found incomplete layer` en el log es la
copia a medias de un intento anterior limpiándose.

**`Environment=PANEL_HOST=0.0.0.0` va después del `EnvironmentFile`**, por la
misma razón que `DATA_DIR` en la unidad del agente: `EnvironmentFile` pisa el
`ENV` de la imagen. Sin esa línea, el `127.0.0.1` que `.env.panel` trae para la
ejecución nativa se cuela dentro del contenedor, uvicorn escucha en su propio
loopback, y el resultado es un servicio que systemd da por activo, sin nada raro
en los logs, cuyo puerto publicado no contesta. Quien restringe el acceso es
`PublishPort=127.0.0.1:8080`, no esa variable.

**No lleva `/dev/snd`, ni `keep-groups`, ni `--ipc=host`.** Dos contenedores
abriendo la tarjeta de sonido es exactamente el *dispositivo ocupado* de más
arriba.

La reindexación es una unidad de un solo uso con la imagen del **agente**, que es
la que tiene chromadb y fastembed:

```bash
# 7 s si no ha cambiado nada; con el corpus clínico entero por primera vez, minutos
systemctl --user start voice-agent-ingest
```

Al ser `Type=oneshot`, `start` **espera** a que termine. Quadlet añade
`--sdnotify=conmon` por defecto, que en un oneshot dejaría la unidad esperando
una notificación que nunca llega; por eso la unidad lleva `Notify=false`.

## Actualizar

```bash
make build
systemctl --user restart voice-agent
```

Si has cambiado el corpus:

```bash
make ingest-container       # reindexa usando la propia imagen
systemctl --user restart voice-agent
```

Si has cambiado `.env`, hace falta reiniciar: `EnvironmentFile` se lee al
arrancar el contenedor.

## Diagnóstico

**El contenedor no encuentra la tarjeta de sonido.**

```bash
make audio-check-container
```

Si falla y `make audio-check` (fuera del contenedor) funciona, el problema está
en el paso de dispositivos. Comprueba que tu usuario está en el grupo `audio`
(`id | grep audio`) y que el runtime es crun (`podman info | grep -i ocirun`).

**"Device or resource busy".** Hay otro proceso con la tarjeta abierta. Si el
servicio está corriendo, para primero: `systemctl --user stop voice-agent`. Si
no, `fuser -v /dev/snd/*` te dice quién la tiene.

**El servicio no arranca tras reiniciar la placa.** Comprueba el linger:

```bash
loginctl show-user $USER --property=Linger    # debe decir Linger=yes
```

**Se descargan los modelos en cada arranque.** El volumen `data/` no se está
montando, o `DATA_DIR` no apunta a `/data` dentro del contenedor.

**"no space left on device" al construir.** Es el fallo más probable en esta
placa, porque la tarjeta microSD son 15 GB y el proyecto ya consume una buena
parte: el entorno virtual nativo pesa 1.1 GB y los modelos descargados otros
500 MB.

El problema no es el tamaño de la imagen final, sino el **pico transitorio**.
La etapa de construcción produce una capa de unos 2.6 GB (compilador,
cabeceras y el entorno virtual completo) y Podman la escribe primero como blob
en `/var/tmp` y después la copia a su almacén: durante ese instante necesita el
doble, más de 5 GB libres.

Conviene tener **al menos 8 GB libres** antes de construir, y comprobarlo
en serio: los restos de un build anterior interrumpido no se ven en
`podman system df`. Para hacer sitio:

```bash
podman image prune -f                              # capas colgantes
podman rmi -f $(podman images -q -f dangling=true) # las que se resistan
sudo apt-get clean                                 # caché de paquetes
sudo journalctl --vacuum-size=50M                  # registros antiguos
uv cache prune                                     # caché de uv
df -h /
```

Ojo con `podman system prune -a`: borra también las imágenes que uses para otras
cosas en la placa.

### Interrumpir una construcción deja gigabytes tirados

Esto merece su propio apartado porque es especialmente traicionero: si matas un
`podman build` a media faena, **los ficheros temporales quedan huérfanos** y
nadie los recoge. Ni `podman image prune` ni `podman system df` los ven, así que
puedes estar mirando un `podman system df` que dice 265 MB mientras el disco está
al 99 %.

Esa basura cae en `$HOME/.cache/podman-build`, que es a donde `make build`
apunta `TMPDIR` justamente para que `clean-space` sepa dónde buscarla, y no en
`/var/tmp`. El ejemplo de abajo es de cuando no había ruta propia, y el problema
es idéntico.

En una sesión de pruebas, tras interrumpir cuatro construcciones, `/var/tmp`
había acumulado 6.9 GB:

```bash
sudo du -sh /var/tmp/* | sort -rh | head
#   1.7G  /var/tmp/buildah1366290818
#   1.1G  /var/tmp/container_images_storage2001335656
#   925M  /var/tmp/buildah-cache-1000     <- la caché de uv del RUN --mount
#   ...
```

Con ninguna construcción en marcha, se limpian sin riesgo:

```bash
sudo rm -rf /var/tmp/buildah* /var/tmp/container_images_storage*
```

No borres los directorios `systemd-private-*` que hay ahí: esos sí son del
sistema.

Nótese que `buildah-cache-1000` es la caché de uv que monta el `Containerfile`.
Es legítima y acelera reconstrucciones, pero ocupa cerca de un gigabyte: si
andas muy justo de espacio, bórrala también.

## Copias de seguridad

Lo único que no se puede reconstruir es `corpus/` y tu `.env`. El índice de
Chroma y los modelos se regeneran con `make ingest` y `make models`.

```bash
tar czf respaldo-agente.tar.gz corpus/ .env
```
