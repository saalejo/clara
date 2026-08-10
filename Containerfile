# Imagen del agente de voz para aarch64 (NanoPi R4S / RK3399).
#
# UNA SOLA ETAPA, Y ES DELIBERADO
# --------------------------------
# Lo natural aquí sería una construcción en dos etapas: compilar en una imagen
# con herramientas y copiar solo el entorno virtual a una imagen limpia. Ahorra
# unos 300 MB de compilador y cabeceras en la imagen final.
#
# Cuando se escribió esto, en esta placa **no funcionaba**. El entorno virtual
# pesa 1.1 GB —Pipecat arrastra llvmlite (168 MB), scipy (101 MB vía pyloudnorm),
# las bibliotecas de PyAV (54 MB) y el cliente de Kubernetes (41 MB), además de
# lo que sí se usa— y
# copiarlo entre etapas obliga a Podman a tenerlo tres veces a la vez: en la
# imagen de construcción, como blob en disco, y desempaquetado en la capa nueva.
# El pico transitorio supera los 4 GB y la microSD de 15 GB se queda sin
# espacio al confirmar la capa, con un `no space left on device` en mitad del
# COPY. Falló cuatro veces seguidas, incluso arrancando con 9 GB libres.
#
# En una sola etapa no hay copia: el entorno virtual se crea donde se va a
# quedar. La imagen final crece unos 300 MB, que es un precio pequeño a cambio
# de que la construcción no dependa de tener 4 GB de holgura transitoria.
#
# NOTA: desde que el almacén de Podman vive en un pendrive de 115 GB, esa
# restricción de espacio ya no existe. La etapa única se mantiene porque sigue
# siendo más simple y el ahorro no compensa reintroducir la complejidad, pero
# el motivo original ya no aplica: si algún día interesa adelgazar la imagen,
# esto se puede reconsiderar sin miedo.

FROM docker.io/library/python:3.13-slim-trixie

# uv se copia desde su imagen oficial: más rápido y reproducible que instalarlo
# con pip.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# `portaudio19-dev` y el compilador son necesarios porque **PyAudio no publica
# ruedas precompiladas** y hay que compilar su extensión en C. Se quedan en la
# imagen: es el coste de la etapa única. El resto de dependencias pesadas
# —onnxruntime, ChromaDB, CTranslate2— sí tienen rueda para aarch64.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        portaudio19-dev \
        libasound2t64 \
        alsa-utils \
        ca-certificates \
        libsbc1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# `UV_LINK_MODE=copy` es obligatorio: la caché de uv se monta como caché de
# construcción y desaparece al acabar la orden, así que el entorno virtual debe
# contener copias reales y no enlaces duros a ficheros que ya no existirán.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Instalación en dos pasos para que la capa cara dependa solo de los
# manifiestos.
#
# Esto es un workspace de uv con tres paquetes, así que hacen falta los
# manifiestos de **todos** aunque aquí solo se instale el del agente: uv resuelve
# el workspace entero contra un único uv.lock y se queja si falta un miembro que
# el fichero de bloqueo menciona. Los del panel pesan unos pocos kilobytes.
#
# `--package voice-agent` es lo que mantiene fuera a Django: se instalan solo las
# dependencias de este paquete, no las de sus hermanos.
#
# La caché de uv se monta como caché de construcción en lugar de dejarla caer
# dentro de la capa; sin eso la capa contendría dos copias de todo, los archivos
# descargados y el entorno virtual.
COPY pyproject.toml uv.lock ./
COPY packages/core/pyproject.toml packages/core/README.md ./packages/core/
COPY packages/panel/pyproject.toml packages/panel/README.md ./packages/panel/
# El puente de telefonía y el demonio de botones corren nativos y NO se instalan
# en esta imagen, pero sus manifiestos tienen que estar: uv resuelve el workspace
# entero contra un único uv.lock y falla si le falta un miembro que el lock
# menciona. El error habla del workspace y no menciona qué paquete falta, así que
# es caro de diagnosticar.
COPY packages/telefonia/pyproject.toml packages/telefonia/README.md ./packages/telefonia/
COPY packages/botones/pyproject.toml packages/botones/README.md ./packages/botones/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package voice-agent --no-install-workspace

COPY README.md ./
COPY src ./src
COPY packages/core/src ./packages/core/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package voice-agent

# La capa ALSA que adapta la tarjeta USB (44.1/48 kHz, salida estéreo) a lo que
# necesita el pipeline (16 kHz mono). Sin esto, PyAudio no puede abrir el
# dispositivo dentro del contenedor. Ver deploy/asound.conf y docs/audio.md.
COPY deploy/asound.conf /etc/asound.conf

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    CORPUS_DIR=/corpus

# Se declara el volumen para que quede documentado en la propia imagen: sin
# montarlo, cada arranque volvería a descargar cientos de megabytes de modelos.
VOLUME ["/data"]

CMD ["python", "-m", "voice_agent"]
