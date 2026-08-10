"""Registro de la aplicación en Django."""

from __future__ import annotations

from django.apps import AppConfig


class PanelConfig(AppConfig):
    """La única aplicación del proyecto."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "voice_agent_panel"
    verbose_name = "Panel del agente de voz"
