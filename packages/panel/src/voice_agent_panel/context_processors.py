"""El perfil en edición, disponible en todas las plantillas.

"Activo" y "en edición" son cosas distintas a propósito: el activo es el que se
exporta al desplegar; el que está en edición es el que enseñan las páginas de
prompt, ajustes, herramientas, MCP y hooks. Así se puede dejar listo un perfil
antes de activarlo, sin usar la bandera de activo como cursor de edición.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest

from voice_agent_panel.models import Perfil

#: Clave de sesión donde se guarda el pk del perfil que se está editando.
CLAVE_SESION_PERFIL = "perfil_en_edicion"


def perfil_en_edicion(request: HttpRequest) -> Perfil:
    """El perfil que se está editando: el de la sesión, o el activo.

    Si el de la sesión ya no existe —lo borró otro navegador— se degrada al
    predeterminado en vez de fallar.
    """
    pk = request.session.get(CLAVE_SESION_PERFIL)
    if pk is not None:
        perfil = Perfil.objects.filter(pk=pk).first()
        if perfil is not None:
            return perfil
    return Perfil.predeterminado()


def perfiles(request: HttpRequest) -> dict[str, Any]:
    """Expone el perfil en edición y el activo a las plantillas."""
    if not getattr(request.user, "is_authenticated", False):
        return {}
    return {
        "perfil_en_edicion": perfil_en_edicion(request),
        "perfil_activo": Perfil.activo_o_none(),
    }
