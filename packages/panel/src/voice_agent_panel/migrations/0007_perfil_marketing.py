"""El perfil Marketing: Clara pasa a ser la asesora comercial de Voz Digital.

La siembra crea el perfil "Marketing" y lo deja ACTIVO, con su propio prompt
(las constantes `*_MARKETING` de `voice_agent_core.prompts`) y las herramientas
clínicas apagadas; el perfil "Por defecto" —el clínico— queda intacto pero
inactivo, con las herramientas comerciales apagadas para que siga siendo
exactamente el agente de siempre si se reactiva. Ausencia de fila
`Herramienta` significa "habilitada", de ahí que haya que apagar en las dos
direcciones.

Activar no despliega: tras migrar sigue haciendo falta el ciclo
guardar → exportar → reiniciar del panel para que el agente lo cargue.
"""

from django.db import migrations

HERRAMIENTAS_CLINICAS = (
    "buscar_en_documentos",
    "registrar_alerta",
    "finalizar_llamada",
    "guardar_respuestas",
    "historial_paciente",
)

HERRAMIENTAS_COMERCIALES = (
    "identificar_prospecto",
    "guardar_brief",
    "historial_prospecto",
)


def _sembrar_perfil_marketing(apps, schema_editor):
    # Las constantes viven en core, como las que siembra la vista del prompt:
    # lo que se enseña es exactamente lo que corre.
    from voice_agent_core.prompts import (
        MULETILLAS_MARKETING,
        PROMPT_SISTEMA_MARKETING,
        SALUDO_MARKETING,
    )

    Perfil = apps.get_model("voice_agent_panel", "Perfil")
    VersionPrompt = apps.get_model("voice_agent_panel", "VersionPrompt")
    Herramienta = apps.get_model("voice_agent_panel", "Herramienta")

    # Primero desactivar, después crear el activo: la restricción parcial
    # `un_solo_perfil_activo` no admite dos a la vez ni un instante.
    Perfil.objects.filter(activo=True).update(activo=False)
    marketing = Perfil.objects.create(
        nombre="Marketing",
        descripcion="Asesora comercial de Voz Digital: descubre necesidades y deja el brief.",
        activo=True,
    )
    VersionPrompt.objects.create(
        perfil=marketing,
        mensaje="Perfil comercial de fábrica",
        prompt_sistema=PROMPT_SISTEMA_MARKETING,
        alma="",
        saludo_inicial=SALUDO_MARKETING,
        muletillas={k: list(v) for k, v in MULETILLAS_MARKETING.items()},
        activa=True,
    )
    for nombre in HERRAMIENTAS_CLINICAS:
        Herramienta.objects.update_or_create(
            perfil=marketing, nombre=nombre, defaults={"habilitada": False}
        )
    por_defecto = Perfil.objects.filter(nombre="Por defecto").first()
    if por_defecto is not None:
        for nombre in HERRAMIENTAS_COMERCIALES:
            Herramienta.objects.update_or_create(
                perfil=por_defecto, nombre=nombre, defaults={"habilitada": False}
            )


def _revertir_perfil_marketing(apps, schema_editor):
    Perfil = apps.get_model("voice_agent_panel", "Perfil")
    Herramienta = apps.get_model("voice_agent_panel", "Herramienta")

    # El borrado arrastra en cascada sus versiones de prompt y herramientas.
    Perfil.objects.filter(nombre="Marketing").delete()
    por_defecto = Perfil.objects.filter(nombre="Por defecto").first()
    if por_defecto is not None:
        Herramienta.objects.filter(perfil=por_defecto, nombre__in=HERRAMIENTAS_COMERCIALES).delete()
        if not Perfil.objects.filter(activo=True).exists():
            por_defecto.activo = True
            por_defecto.save(update_fields=["activo"])


class Migration(migrations.Migration):
    dependencies = [
        ("voice_agent_panel", "0006_tareaprogramada_procedimiento"),
    ]

    operations = [
        migrations.RunPython(_sembrar_perfil_marketing, _revertir_perfil_marketing),
    ]
