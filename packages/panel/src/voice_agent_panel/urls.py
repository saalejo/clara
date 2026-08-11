"""Rutas del panel.

Todo cuelga de `/panel/` salvo `/healthz`, y las acciones que cambian algo son
POST: el `LoginRequiredMiddleware` cierra el acceso y el CSRF de Django impide
que otra página dispare un reinicio del agente por ti.
"""

from __future__ import annotations

from django.contrib.auth import views as auth_views
from django.urls import path
from django.views.generic import RedirectView

from voice_agent_panel import views

urlpatterns = [
    path("", RedirectView.as_view(url="/panel/", permanent=False)),
    path("healthz", views.healthz, name="healthz"),
    path("panel/", views.panel, name="panel"),
    path(
        "panel/entrar/",
        auth_views.LoginView.as_view(template_name="panel/entrar.html"),
        name="login",
    ),
    path("panel/salir/", auth_views.LogoutView.as_view(), name="logout"),
    path("panel/perfiles/", views.perfiles_lista, name="perfiles"),
    path("panel/perfiles/<int:pk>/editar/", views.perfil_editar, name="perfil_editar"),
    path("panel/perfiles/<int:pk>/activar/", views.perfil_activar, name="perfil_activar"),
    path(
        "panel/perfiles/<int:pk>/seleccionar/", views.perfil_seleccionar, name="perfil_seleccionar"
    ),
    path("panel/perfiles/<int:pk>/duplicar/", views.perfil_duplicar, name="perfil_duplicar"),
    path("panel/perfiles/<int:pk>/borrar/", views.perfil_borrar, name="perfil_borrar"),
    path("panel/prompt/", views.prompt, name="prompt"),
    path("panel/prompt/<int:pk>/volver/", views.volver_a_version, name="volver_a_version"),
    path("panel/ajustes/", views.ajustes, name="ajustes"),
    path("panel/herramientas/", views.herramientas, name="herramientas"),
    # El nombre del tema y el del documento viajan en el cuerpo del POST, nunca
    # en la ruta: así ningún convertidor de URL puede acabar admitiendo barras.
    path("panel/conocimiento/", views.conocimiento, name="conocimiento"),
    path("panel/conocimiento/temas/nuevo/", views.tema_crear, name="tema_crear"),
    path("panel/conocimiento/temas/borrar/", views.tema_borrar, name="tema_borrar"),
    path(
        "panel/conocimiento/documentos/borrar/",
        views.documento_borrar,
        name="documento_borrar",
    ),
    path("panel/mcp/", views.mcp, name="mcp"),
    path("panel/mcp/nuevo/", views.mcp_editar, name="mcp_nuevo"),
    path("panel/mcp/<int:pk>/", views.mcp_editar, name="mcp_editar"),
    path("panel/mcp/<int:pk>/borrar/", views.mcp_borrar, name="mcp_borrar"),
    path("panel/mcp/<int:pk>/sondear/", views.mcp_sondear, name="mcp_sondear"),
    path("panel/tareas/", views.tareas, name="tareas"),
    path("panel/tareas/nueva/", views.tarea_editar, name="tarea_nueva"),
    path("panel/tareas/<int:pk>/", views.tarea_editar, name="tarea_editar"),
    path("panel/tareas/<int:pk>/borrar/", views.tarea_borrar, name="tarea_borrar"),
    path("panel/tareas/<int:pk>/resultados/", views.tarea_resultados, name="tarea_resultados"),
    path("panel/evaluaciones/", views.evaluaciones, name="evaluaciones"),
    path("panel/pacientes/", views.pacientes, name="pacientes"),
    path("panel/hooks/", views.hooks, name="hooks"),
    path("panel/hooks/nuevo/", views.hook_editar, name="hook_nuevo"),
    path("panel/hooks/<int:pk>/", views.hook_editar, name="hook_editar"),
    path("panel/hooks/<int:pk>/borrar/", views.hook_borrar, name="hook_borrar"),
    path("panel/desplegar/", views.desplegar, name="desplegar"),
    path("panel/servicio/<str:accion>/", views.servicio, name="servicio"),
    path("panel/logs/", views.logs, name="logs"),
    path("panel/logs/stream/", views.logs_stream, name="logs_stream"),
    path("panel/despliegues/", views.despliegues, name="despliegues"),
    path("panel/despliegues/<int:pk>/", views.despliegue_detalle, name="despliegue_detalle"),
]
