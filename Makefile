# Atajos del proyecto. Todo pasa por `uv`, que gestiona el entorno virtual y
# las dependencias a partir de pyproject.toml + uv.lock.
#
# Ejecuta `make` sin argumentos para ver la lista de objetivos.

.DEFAULT_GOAL := help
SHELL := /bin/bash

# `uv` se instala en ~/.local/bin, que el PATH solo recoge en shells
# interactivos a través de .bashrc. Un `ssh placa 'make algo'` abre un shell NO
# interactivo, así que sin esta línea todos los objetivos fallan con
# "uv: command not found" — que es un mensaje bastante desorientador cuando uv
# está perfectamente instalado y funciona al entrar por SSH a mano.
export PATH := $(HOME)/.local/bin:$(PATH)

IMAGE   := localhost/voice-agent:latest
IMAGE_PANEL := localhost/voice-agent-panel:latest
DATA    := $(CURDIR)/data
CORPUS  := $(CURDIR)/corpus

.PHONY: help
help:  ## Muestra esta ayuda
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- Desarrollo --------------------------------------------------------------

.PHONY: install
install:  ## Instala las dependencias de los tres paquetes en .venv
	# `--all-packages` es necesario: sin él, uv solo instala las del paquete raíz
	# y el panel se queda sin Django. Cada Containerfile, en cambio, usa
	# `--package <x>` para instalar únicamente lo suyo.
	uv sync --all-packages

.PHONY: lint
lint:  ## Comprueba estilo y tipos (ruff + mypy)
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

.PHONY: format
format:  ## Formatea el código y arregla lo que ruff pueda arreglar solo
	uv run ruff check --fix .
	uv run ruff format .

# La configuración de Django del panel exige PANEL_SECRET_KEY, y en producción
# eso tiene que seguir siendo un error de arranque: un panel sin clave secreta no
# debe levantarse. Aquí se le da una de mentira.
#
# Va en el Makefile y no en un conftest.py porque pytest-django lee la
# configuración de Django dentro de `pytest_load_initial_conftests`, o sea
# mientras se cargan los conftests: cualquier variable puesta ahí puede llegar
# tarde. DATA_DIR apunta a un temporal para que importar la configuración no
# toque el directorio de datos real de la placa; los tests que escriben usan
# `tmp_path`.
ENTORNO_TESTS = PANEL_SECRET_KEY=clave-solo-para-los-tests DATA_DIR=$(shell mktemp -d)

.PHONY: test
test:  ## Ejecuta la batería de tests
	$(ENTORNO_TESTS) uv run pytest

# --- Modelos y base de conocimiento ------------------------------------------

.PHONY: models
models:  ## Descarga por adelantado Whisper, la voz de Piper y los embeddings
	uv run python -m voice_agent.models

.PHONY: ingest
ingest:  ## Indexa corpus/ en ChromaDB
	uv run python -m voice_agent.rag.ingest

.PHONY: reingest
reingest:  ## Reconstruye el índice desde cero
	uv run python -m voice_agent.rag.ingest --reset

.PHONY: ask
ask:  ## Consulta el RAG por CLI sin voz.  Uso: make ask Q="tu pregunta"
	uv run python -m voice_agent.rag.retriever "$(Q)"

.PHONY: calidad
calidad:  ## Ensaya escenarios de calidad adversarios.  Uso: make calidad [ESC="id1 id2"]
	uv run python -m voice_agent.calidad $(if $(ESC),$(foreach e,$(ESC),--escenario $(e)),--todos)

# --- Audio --------------------------------------------------------------------

# Las pruebas de audio necesitan la tarjeta para ellas solas. La capa dmix/dsnoop
# permite que varios procesos la abran a la vez, pero eso no basta aquí: si el
# agente sigue vivo, reacciona a lo que digas durante la prueba, y en el
# diagnóstico de ruido la fase "ningún flujo abierto" sería directamente falsa.
#
# En lugar de obligar a pararlo y arrancarlo a mano —y a acordarse de arrancarlo,
# que es lo que de verdad se olvida— estos objetivos lo paran solo si estaba
# corriendo y lo restauran al terminar. El `trap` cubre también la salida por
# Ctrl-C o por error, para no dejarse el agente parado sin darse cuenta.
define con_servicio_parado
	estaba=$$(systemctl --user is-active voice-agent 2>/dev/null || true); \
	if [ "$$estaba" = "active" ]; then \
		echo "  (parando el servicio; se restaurará al terminar)"; \
		systemctl --user stop voice-agent; \
	fi; \
	trap 'if [ "$$estaba" = "active" ]; then \
		echo "  (restaurando el servicio)"; \
		systemctl --user start voice-agent; \
	fi' EXIT INT TERM; \
	$(1)
endef

.PHONY: audio-check
audio-check:  ## Lista los dispositivos de audio y hace una prueba de grabación/reproducción
	@$(call con_servicio_parado,uv run python -m voice_agent.audio_devices --check)

.PHONY: audio-noise
audio-noise:  ## Localiza por fases qué provoca un zumbido o pitido en los auriculares
	@$(call con_servicio_parado,uv run python -m voice_agent.audio_devices --diagnose-noise)

.PHONY: audio-noise-levels
audio-noise-levels:  ## Prueba si bajar volumen o ganancia atenúa el zumbido del adaptador
	@$(call con_servicio_parado,uv run python -m voice_agent.audio_devices --diagnose-noise-levels)

.PHONY: audio-list
audio-list:  ## Solo lista los dispositivos que ve PortAudio
	uv run python -m voice_agent.audio_devices

# --- Ejecución ----------------------------------------------------------------

.PHONY: run
run:  ## Arranca el agente en local (fuera del contenedor)
	uv run python -m voice_agent

# `--factory` porque `crear_app` es una factoría: importar el módulo no carga
# configuración ni modelos; eso pasa en el lifespan, al arrancar de verdad.
# 0.0.0.0 aquí, para poder probar desde el móvil en la LAN; el micrófono del
# navegador solo funciona vía HTTPS o localhost, así que el acceso real va por
# el túnel. OJO: la unidad de systemd escucha en 127.0.0.1 y esa diferencia es
# deliberada, no un descuido — ver docs/seguridad.md. No las unifiques.
.PHONY: run-web
run-web:  ## Arranca la interfaz de llamada por navegador (puerto 7860)
	uv run uvicorn --factory voice_agent.web:crear_app --host 0.0.0.0 --port 7860

.PHONY: metricas
metricas:  ## Agrega las métricas medidas (latencia, tokens, coste) para el README
	uv run python scripts/metricas.py

.PHONY: turn
turn:  ## Renueva las credenciales TURN de Cloudflare (caducan cada 48 h)
	uv run python scripts/renovar_turn.py

# --- Panel de control ---------------------------------------------------------

# El panel necesita su propio fichero de entorno, distinto del del agente. El
# `set -a` exporta todo lo que se defina en él sin tener que repetir `export`.
CON_ENV_PANEL = set -a; . ./.env.panel; set +a;

.PHONY: panel
panel:  ## Arranca el panel en local (migra y siembra el usuario al arrancar)
	@test -f .env.panel || { echo "Falta .env.panel (copia .env.panel.example)"; exit 1; }
	@$(CON_ENV_PANEL) uv run python -m voice_agent_panel

.PHONY: panel-migrate
panel-migrate:  ## Aplica las migraciones del panel
	@$(CON_ENV_PANEL) uv run python -m voice_agent_panel migrate

.PHONY: panel-user
panel-user:  ## Crea un usuario del panel de forma interactiva
	@$(CON_ENV_PANEL) uv run python -m voice_agent_panel createsuperuser

.PHONY: panel-export
panel-export:  ## Exporta la configuración del panel a data/config/ sin reiniciar
	@$(CON_ENV_PANEL) uv run python -m voice_agent_panel exportar

.PHONY: panel-makemigrations
panel-makemigrations:  ## Regenera las migraciones tras cambiar los modelos
	@$(CON_ENV_PANEL) DJANGO_SETTINGS_MODULE=voice_agent_panel.settings \
		uv run python -m django makemigrations voice_agent_panel

# --- Contenedor ----------------------------------------------------------------

# La caché de construcción la gobierna TMPDIR, que por defecto apunta a /var/tmp.
# Se le da una ruta propia por dos motivos que siguen valiendo aunque ya haya
# sitio de sobra: un `podman build` interrumpido deja gigabytes de basura que
# `podman system df` NO ve, y `clean-space` necesita saber dónde buscarla.
#
# Antes esto apuntaba a /mnt/almacen/tmp, en un pendrive, porque la microSD de
# 15 GB no daba ni para el pico transitorio del build. Con la tarjeta de 58 GB el
# pendrive ya no hace falta.
BUILD_TMP := $(HOME)/.cache/podman-build

.PHONY: build
build:  ## Construye la imagen del contenedor con Podman
	@mkdir -p $(BUILD_TMP)
	TMPDIR=$(BUILD_TMP) podman build -t $(IMAGE) -f Containerfile .

.PHONY: build-panel
build-panel:  ## Construye la imagen del panel (~2 min, frente a los ~10 del agente)
	@mkdir -p $(BUILD_TMP)
	TMPDIR=$(BUILD_TMP) podman build -t $(IMAGE_PANEL) -f Containerfile.panel .

.PHONY: clean-space
clean-space:  ## Borra imágenes colgadas y restos de construcciones interrumpidas
	podman image prune -f
	rm -rf $(BUILD_TMP)/buildah* $(BUILD_TMP)/container_images_storage*
	@df -h /

.PHONY: run-container
run-container:  ## Ejecuta el agente en un contenedor con acceso a la tarjeta de sonido
	podman run --rm -it --name voice-agent \
		--device /dev/snd \
		--group-add keep-groups \
		--ipc=host \
		--env-file .env \
		-e DATA_DIR=/data -e CORPUS_DIR=/corpus \
		-v $(DATA):/data:Z \
		-v $(CORPUS):/corpus:ro,Z \
		--memory 2g \
		$(IMAGE)

.PHONY: audio-check-container
audio-check-container:  ## Comprueba el audio desde dentro del contenedor
	podman run --rm -it \
		--device /dev/snd --group-add keep-groups --ipc=host \
		--env-file .env \
		-e DATA_DIR=/data -e CORPUS_DIR=/corpus \
		-v $(DATA):/data:Z \
		$(IMAGE) python -m voice_agent.audio_devices --check

.PHONY: ingest-container
ingest-container:  ## Reindexa el corpus usando la imagen del contenedor
	podman run --rm \
		--env-file .env \
		-e DATA_DIR=/data -e CORPUS_DIR=/corpus \
		-v $(DATA):/data:Z \
		-v $(CORPUS):/corpus:ro,Z \
		$(IMAGE) python -m voice_agent.rag.ingest --reset

# --- Telefonía (puente Bluetooth manos libres) --------------------------------

# El puente corre NATIVO, no en un contenedor. El porqué está en
# packages/telefonia/README.md y en deploy/voice-agent-telefonia.service.

.PHONY: telefonia
telefonia:  ## Arranca el puente de telefonía en primer plano (para iterar)
	DATA_DIR=$(DATA) uv run --package voice-agent-telefonia python -m voice_agent_telefonia

.PHONY: install-telefonia
install-telefonia:  ## Instala la unidad de usuario del puente de telefonía
	mkdir -p $(HOME)/.config/systemd/user
	sed -e 's|@@PROJECT_DIR@@|$(CURDIR)|g' deploy/voice-agent-telefonia.service \
		> $(HOME)/.config/systemd/user/voice-agent-telefonia.service
	systemctl --user daemon-reload
	@echo "Instalada. Arranca con: systemctl --user enable --now voice-agent-telefonia"

# COMPROBADO EN LA PLACA: `journalctl --user -u voice-agent-telefonia` sale
# VACÍO, igual que con las unidades del agente y el panel. En esta Armbian no
# hay journal de usuario, así que todo acaba en el del sistema —y ahí el
# proceso aparece con la etiqueta `uv`, no con el nombre de la unidad, porque
# el ExecStart es `uv run`. Se filtra por unidad con `_SYSTEMD_USER_UNIT`.
.PHONY: telefonia-logs
telefonia-logs:  ## Sigue los logs del puente de telefonía
	sudo journalctl _SYSTEMD_USER_UNIT=voice-agent-telefonia.service -f

SOCK := $(DATA)/run/telefonia.sock

.PHONY: telefonia-estado
telefonia-estado:  ## Consulta el estado del teléfono por el socket unix
	@curl -s --unix-socket $(SOCK) http://telefonia/estado | python3 -m json.tool

.PHONY: telefonia-contactos
telefonia-contactos:  ## Fuerza la descarga de la agenda del móvil por PBAP
	@curl -s -XPOST --unix-socket $(SOCK) http://telefonia/contactos/sincronizar | python3 -m json.tool

.PHONY: telefonia-buscar
telefonia-buscar:  ## Busca en la agenda.  Uso: make telefonia-buscar Q="ana"
	@curl -s --unix-socket $(SOCK) "http://telefonia/contactos?nombre=$(Q)" | python3 -m json.tool

.PHONY: telefonia-autocontestar
telefonia-autocontestar:  ## Consulta o alterna el autocontestar.  Uso: make telefonia-autocontestar [ON=1|OFF=1]
	@if [ -n "$(ON)" ]; then \
		curl -s -XPOST --unix-socket $(SOCK) -d '{"activo": true}' http://telefonia/autocontestar | python3 -m json.tool; \
	elif [ -n "$(OFF)" ]; then \
		curl -s -XPOST --unix-socket $(SOCK) -d '{"activo": false}' http://telefonia/autocontestar | python3 -m json.tool; \
	else \
		curl -s --unix-socket $(SOCK) http://telefonia/autocontestar | python3 -m json.tool; \
	fi

.PHONY: telefonia-eventos
telefonia-eventos:  ## Escucha el canal de eventos (Ctrl+C para salir)
	@curl -sN --unix-socket $(SOCK) http://telefonia/eventos

# INTRUSIVA: el móvil acepta una sola conexión HFP, así que hay que apartar a
# oFono antes y devolverlo después. No lo hace el target: pararlo es del sistema
# y con sudo, y dejarlo parado por una interrupción sería peor que teclearlo.
#
#   sudo systemctl stop ofono && make telefonia-sonda; sudo systemctl start ofono
#
.PHONY: telefonia-sonda
telefonia-sonda:  ## Habla HFP a pelo con el móvil, sin oFono (requiere ofono parado)
	@grep -q TELEFONIA_BLUETOOTH_ADDRESS .env.telefonia 2>/dev/null \
		|| echo "Aviso: define TELEFONIA_BLUETOOTH_ADDRESS o expórtala antes."
	uv run --package voice-agent-telefonia python -m voice_agent_telefonia.sonda

# --- Botones (mando físico de la tarjeta de sonido) ---------------------------

# El demonio corre NATIVO, como el puente. El porqué está en
# packages/botones/README.md: un contenedor no ve /dev/input y no puede gobernar
# el systemd del usuario.

# Variante de `con_servicio_parado` parametrizada por unidad. Hace falta porque la
# sonda de botones choca con el propio demonio de botones, no con el agente: el
# servicio tiene el device en exclusiva por EVIOCGRAB y la sonda vería silencio.
# El síntoma —"la sonda no detecta nada"— parece una avería del hardware.
define con_unidad_parada
	estaba=$$(systemctl --user is-active $(1) 2>/dev/null || true); \
	if [ "$$estaba" = "active" ]; then \
		echo "  (parando $(1); se restaurará al terminar)"; \
		systemctl --user stop $(1); \
	fi; \
	trap 'if [ "$$estaba" = "active" ]; then \
		echo "  (restaurando $(1))"; \
		systemctl --user start $(1); \
	fi' EXIT INT TERM; \
	$(2)
endef

.PHONY: botones
botones:  ## Arranca el mando físico en primer plano (para iterar)
	DATA_DIR=$(DATA) uv run --package voice-agent-botones python -m voice_agent_botones

.PHONY: botones-sonda
botones-sonda:  ## Imprime los gestos de los botones sin ejecutar nada
	@$(call con_unidad_parada,voice-agent-botones,DATA_DIR=$(DATA) uv run --package voice-agent-botones python -m voice_agent_botones --sonda)

.PHONY: botones-pitidos
botones-pitidos:  ## Genera y reproduce el catálogo de pitidos, para afinarlo de oído
	DATA_DIR=$(DATA) uv run --package voice-agent-botones python -m voice_agent_botones --pitidos

.PHONY: install-botones
install-botones:  ## Instala la unidad de usuario del mando físico
	mkdir -p $(HOME)/.config/systemd/user
	sed -e 's|@@PROJECT_DIR@@|$(CURDIR)|g' deploy/voice-agent-botones.service \
		> $(HOME)/.config/systemd/user/voice-agent-botones.service
	systemctl --user daemon-reload
	@echo "Instalada. Arranca con: systemctl --user enable --now voice-agent-botones"

# Mismo motivo que en telefonia-logs: en esta Armbian no hay journal de usuario y
# el proceso aparece etiquetado como `uv`, porque el ExecStart es `uv run`.
.PHONY: botones-logs
botones-logs:  ## Sigue los logs del mando físico
	sudo journalctl _SYSTEMD_USER_UNIT=voice-agent-botones.service -f

# --- Servicio systemd (Quadlet) -----------------------------------------------

.PHONY: install-service
install-service:  ## Instala la unidad Quadlet en systemd --user
	mkdir -p $(HOME)/.config/containers/systemd
	sed -e 's|@@PROJECT_DIR@@|$(CURDIR)|g' deploy/voice-agent.container \
		> $(HOME)/.config/containers/systemd/voice-agent.container
	systemctl --user daemon-reload
	@echo "Instalada. Arranca con: systemctl --user start voice-agent"

.PHONY: install-panel
install-panel:  ## Instala las unidades del panel y de la reindexación
	@test -f .env.panel || { echo "Falta .env.panel (copia .env.panel.example)"; exit 1; }
	mkdir -p $(HOME)/.config/containers/systemd
	for u in voice-agent-panel voice-agent-ingest; do \
		sed -e 's|@@PROJECT_DIR@@|$(CURDIR)|g' deploy/$$u.container \
			> $(HOME)/.config/containers/systemd/$$u.container; \
	done
	systemctl --user daemon-reload
	@echo "Instaladas. Arranca el panel con: systemctl --user start voice-agent-panel"
	@echo "Y llega con:  ssh -L 8080:localhost:8080 nanopi   ->  http://localhost:8080/panel/"

# Los logs NO están donde uno los buscaría, y hay dos flujos distintos:
#
#   - Lo que escribe el agente. systemd no ejecuta el programa, ejecuta un
#     `podman run -d` que lo lanza en segundo plano, así que su salida nunca
#     pasa por la unidad. Podman la recoge con su driver journald y la deja en
#     el journal del SISTEMA, etiquetada con `_SYSTEMD_USER_UNIT`.
#   - Los eventos de la unidad (arrancó, se paró, falló), que emite el gestor de
#     systemd del usuario y quedan bajo `USER_UNIT`.
#
# Esta placa no persiste journal de usuario (Storage=auto sin /var/log/journal),
# así que `journalctl --user -u voice-agent` sale vacío y despista. Se leen sin
# sudo porque el usuario pertenece al grupo `adm`.
#
# Tampoco vale `podman logs`: Quadlet añade `--rm`, de modo que el contenedor se
# destruye al parar el servicio y se lleva sus logs justo cuando querrías saber
# por qué se cayó. El journal sí los conserva.
UNIT_LOGS := USER_UNIT=voice-agent.service + _SYSTEMD_USER_UNIT=voice-agent.service
PANEL_LOGS := USER_UNIT=voice-agent-panel.service + _SYSTEMD_USER_UNIT=voice-agent-panel.service

.PHONY: service-logs
service-logs:  ## Sigue todo: eventos de la unidad y salida del agente, entrelazados
	journalctl $(UNIT_LOGS) -f

.PHONY: service-events
service-events:  ## Solo los eventos de systemd: arranques, paradas y fallos
	journalctl USER_UNIT=voice-agent.service -n 30 --no-pager

.PHONY: panel-logs
panel-logs:  ## Sigue los logs del panel
	journalctl $(PANEL_LOGS) -f
