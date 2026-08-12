"""Registro en el admin de Django.

El panel tiene su propia interfaz; el admin queda como red de seguridad para
mirar o arreglar algo a mano sin abrir la base de datos con sqlite3.
"""

from __future__ import annotations

from django.contrib import admin

from voice_agent_panel.models import (
    AjusteAgente,
    Despliegue,
    Herramienta,
    Hook,
    LanzamientoCalidad,
    Perfil,
    RevisionCalidad,
    ServidorMCP,
    VersionPrompt,
)

admin.site.register(
    [
        AjusteAgente,
        Despliegue,
        Herramienta,
        Hook,
        LanzamientoCalidad,
        Perfil,
        RevisionCalidad,
        ServidorMCP,
        VersionPrompt,
    ]
)
