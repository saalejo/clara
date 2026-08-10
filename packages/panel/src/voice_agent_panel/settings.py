"""Configuración de Django para el panel.

Se lee del entorno, que en producción viene de `.env.panel` —un fichero
distinto del `.env` del agente, y eso es a propósito: el contenedor del panel no
monta el del agente, así que **no puede** filtrar las claves del LLM ni de
Deepgram ni por accidente ni por un fallo de plantilla.

El panel ejecuta comandos en la placa a través de los hooks y arranca y para el
servicio, así que no hay ninguna vista anónima salvo el login y la comprobación
de salud, y por defecto solo escucha en loopback.
"""

from __future__ import annotations

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from voice_agent_core.rutas import SUBDIR_LOGS

BASE_DIR = Path(__file__).resolve().parent

#: Raíz de datos compartida con el agente. La misma variable que usa él, y por
#: el mismo motivo: dentro del contenedor es /data y fuera es ./data.
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))

#: Carpeta del corpus del RAG, la misma que montan el agente y la unidad de
#: ingesta. El panel la monta en **lectura y escritura**, y esa asimetría es
#: deliberada: es el único sitio donde el panel crea y borra ficheros que no son
#: suyos. Igual que DATA_DIR, es una ruta *dentro del contenedor* y la fija la
#: unidad de systemd, no `.env.panel`.
CORPUS_DIR = Path(os.environ.get("CORPUS_DIR", "corpus"))

SECRET_KEY = os.environ.get("PANEL_SECRET_KEY", "")
if not SECRET_KEY:
    raise ImproperlyConfigured(
        "Falta PANEL_SECRET_KEY. Genera una con:\n"
        "    python -c 'import secrets; print(secrets.token_urlsafe(50))'\n"
        "y ponla en el fichero .env.panel."
    )

DEBUG = os.environ.get("PANEL_DEBUG", "0") == "1"

# Exponer a la red local es una decisión consciente y hay que declararla: el
# panel da ejecución de comandos a quien entre. Por defecto, solo loopback y
# túnel SSH.
PERMITIR_LAN = os.environ.get("PANEL_PERMITIR_LAN", "0") == "1"
ALLOWED_HOSTS = ["*"] if PERMITIR_LAN or DEBUG else ["localhost", "127.0.0.1", "[::1]"]

#: Interruptor general de los hooks que ejecutan comandos. Apagarlo impide
#: crearlos desde el panel; los que ya existan siguen en la configuración.
HOOKS_COMANDO_PERMITIDOS = os.environ.get("PANEL_HOOKS_COMANDO", "1") == "1"

#: Unidades de systemd que el panel puede gobernar. La lista es fija a
#: propósito: el socket de D-Bus da poder sobre *cualquier* servicio del
#: usuario, y el nombre de la unidad no puede venir nunca de una petición.
UNIDAD_AGENTE = os.environ.get("PANEL_UNIDAD_AGENTE", "voice-agent.service")
UNIDAD_INGESTA = os.environ.get("PANEL_UNIDAD_INGESTA", "voice-agent-ingest.service")
# El puente de telefonía. No es una unidad Quadlet sino un servicio de usuario
# nativo, pero para el panel da igual: lo gobierna por D-Bus contra systemd
# exactamente igual que a las otras dos, sin una línea de código nueva.
UNIDAD_TELEFONIA = os.environ.get("PANEL_UNIDAD_TELEFONIA", "voice-agent-telefonia.service")
# El mando físico de la tarjeta de sonido. Está aquí para poder pararlo desde el
# navegador si se vuelve loco: es el único proceso del sistema que acapara el
# device de los botones, así que sin esta salida habría que entrar por SSH.
UNIDAD_BOTONES = os.environ.get("PANEL_UNIDAD_BOTONES", "voice-agent-botones.service")
UNIDADES_PERMITIDAS = frozenset({UNIDAD_AGENTE, UNIDAD_INGESTA, UNIDAD_TELEFONIA, UNIDAD_BOTONES})

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "voice_agent_panel.apps.PanelConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Cierra el panel entero por defecto. Las excepciones se marcan una a una
    # con @login_not_required, que es mucho más difícil de olvidar que
    # acordarse de poner @login_required en cada vista nueva.
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "voice_agent_panel.urls"
WSGI_APPLICATION = None
ASGI_APPLICATION = "voice_agent_panel.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "voice_agent_panel.context_processors.perfiles",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        # En el volumen compartido, no dentro de la imagen: así sobrevive a cada
        # reconstrucción. Es un fichero aparte del de Chroma, que es del agente.
        "NAME": DATA_DIR / "panel" / "panel.sqlite3",
        "OPTIONS": {"timeout": 20},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

# Django 6 cambiará el esquema por defecto de URLField a https; declararlo
# ahora evita el aviso y deja el comportamiento explícito.
FORMS_URLFIELD_ASSUME_HTTPS = True

LANGUAGE_CODE = "es"
TIME_ZONE = os.environ.get("TZ", "America/Bogota")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = Path(os.environ.get("PANEL_STATIC_ROOT", BASE_DIR / "staticfiles"))

# El almacenamiento con manifiesto añade un hash al nombre de cada fichero, lo
# que permite cachearlos para siempre. A cambio exige haber pasado
# `collectstatic`: sin el manifiesto, cualquier `{% static %}` revienta con un
# "Missing staticfiles manifest entry" que no dice nada de lo que falta de
# verdad. En la imagen se genera al construir; en desarrollo y en los tests no
# existe, así que se elige según esté o no.
_HAY_MANIFIESTO = (STATIC_ROOT / "staticfiles.json").is_file()
# Sin manifiesto (desarrollo y tests) whitenoise sirve buscando los ficheros en
# vivo, en vez de avisar de que STATIC_ROOT no existe.
WHITENOISE_AUTOREFRESH = not _HAY_MANIFIESTO
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "whitenoise.storage.CompressedManifestStaticFilesStorage"
            if _HAY_MANIFIESTO
            else "django.contrib.staticfiles.storage.StaticFilesStorage"
        )
    },
}

LOGIN_URL = "/panel/entrar/"
LOGIN_REDIRECT_URL = "/panel/"
LOGOUT_REDIRECT_URL = "/panel/entrar/"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
SESSION_COOKIE_AGE = 8 * 60 * 60
CSRF_COOKIE_SAMESITE = "Strict"
# Va por HTTP plano sobre loopback y un túnel SSH: exigir cookies seguras
# impediría entrar. Si algún día esto se pone detrás de TLS, enciéndelas.
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("PANEL_CSRF_TRUSTED_ORIGINS", "").split(",") if o]

X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True

RUTA_LOG_AGENTE = DATA_DIR / SUBDIR_LOGS / "agente.log"
