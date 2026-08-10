"""Punto de entrada ASGI del panel."""

from __future__ import annotations

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "voice_agent_panel.settings")

application = get_asgi_application()
