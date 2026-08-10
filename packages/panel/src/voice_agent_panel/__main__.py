"""Punto de entrada del panel: `python -m voice_agent_panel [subcomando]`.

Sin argumentos migra la base de datos, siembra el usuario administrador y sirve.
Hacer la migración al arrancar es lo correcto en una placa de un solo nodo: evita
tener que acordarse de un paso manual tras cada reconstrucción de la imagen, y no
hay concurrencia que lo complique.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "voice_agent_panel.settings")


def _preparar() -> None:
    """Arranca Django, aplica migraciones y siembra el usuario."""
    import django

    django.setup()

    from django.conf import settings
    from django.core.management import call_command

    settings.DATABASES["default"]["NAME"].parent.mkdir(parents=True, exist_ok=True)
    call_command("migrate", interactive=False, verbosity=1)
    # Los estáticos también se recolectan aquí, por el mismo motivo que la
    # migración: en la imagen lo hacía el build, pero en el despliegue nativo
    # no hay build y el panel arrancaba sin CSS (whitenoise devolvía 404).
    call_command("collectstatic", interactive=False, verbosity=0)
    _sembrar_admin()


def _sembrar_admin() -> None:
    """Crea o actualiza el usuario administrador desde el entorno.

    La contraseña vive en `.env.panel` y no en la base de datos como valor
    inicial: así cambiarla es editar un fichero y reiniciar, sin comandos.
    """
    from django.contrib.auth import get_user_model

    usuario = os.environ.get("PANEL_ADMIN_USER")
    clave = os.environ.get("PANEL_ADMIN_PASSWORD")
    if not usuario or not clave:
        return

    Usuario = get_user_model()
    cuenta, creada = Usuario.objects.get_or_create(
        username=usuario, defaults={"is_staff": True, "is_superuser": True}
    )
    cuenta.set_password(clave)
    cuenta.is_staff = True
    cuenta.is_superuser = True
    cuenta.save()
    print(f"Usuario '{usuario}' {'creado' if creada else 'actualizado'}.", file=sys.stderr)


def main() -> int:
    """Ejecuta el subcomando pedido, o arranca el servidor."""
    subcomando = sys.argv[1] if len(sys.argv) > 1 else "servir"

    if subcomando == "exportar":
        _preparar()
        from django.conf import settings

        from voice_agent_panel.exporter import ErrorDeExportacion, exportar

        try:
            despliegue = exportar(settings.DATA_DIR)
        except ErrorDeExportacion as e:
            print(f"No se ha exportado nada:\n{e}", file=sys.stderr)
            return 1
        print(f"Exportado a {settings.DATA_DIR}/config/ (despliegue #{despliegue.pk}).")
        return 0

    if subcomando in ("migrate", "createsuperuser", "collectstatic", "shell"):
        import django

        django.setup()
        from django.core.management import execute_from_command_line

        execute_from_command_line([sys.argv[0], *sys.argv[1:]])
        return 0

    _preparar()

    import uvicorn

    host = os.environ.get("PANEL_HOST", "127.0.0.1")
    puerto = int(os.environ.get("PANEL_PORT", "8080"))
    print(f"Panel escuchando en http://{host}:{puerto}/panel/", file=sys.stderr)
    uvicorn.run(
        "voice_agent_panel.asgi:application",
        host=host,
        port=puerto,
        # Un solo worker: la base de datos es SQLite y así no hay contención de
        # bloqueos. Para un panel de una placa es de sobra.
        workers=1,
        log_level=os.environ.get("PANEL_LOG_LEVEL", "info"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
