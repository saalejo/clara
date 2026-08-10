"""Perfiles: cada uno con su prompt, sus ajustes y su selección de herramientas.

La siembra crea el perfil "Por defecto", activo, y le cuelga todo lo que ya
había: así una instalación existente se comporta exactamente igual después de
migrar. Los interruptores globales `habilitado` de servidores MCP y hooks se
convierten en pertenencia a los M2M del perfil por defecto **antes** de borrar
las columnas, que es la razón del orden de las operaciones.
"""

import django.db.models.deletion
from django.db import migrations, models


def _sembrar_perfil_por_defecto(apps, schema_editor):
    Perfil = apps.get_model("voice_agent_panel", "Perfil")
    VersionPrompt = apps.get_model("voice_agent_panel", "VersionPrompt")
    AjusteAgente = apps.get_model("voice_agent_panel", "AjusteAgente")
    Herramienta = apps.get_model("voice_agent_panel", "Herramienta")
    ServidorMCP = apps.get_model("voice_agent_panel", "ServidorMCP")
    Hook = apps.get_model("voice_agent_panel", "Hook")

    perfil = Perfil.objects.create(nombre="Por defecto", activo=True)
    VersionPrompt.objects.update(perfil=perfil)
    AjusteAgente.objects.update(perfil=perfil)
    Herramienta.objects.update(perfil=perfil)
    perfil.mcp_habilitados.set(ServidorMCP.objects.filter(habilitado=True))
    perfil.hooks_habilitados.set(Hook.objects.filter(habilitado=True))


class Migration(migrations.Migration):
    dependencies = [
        ("voice_agent_panel", "0002_reindexado"),
    ]

    operations = [
        migrations.CreateModel(
            name="Perfil",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("nombre", models.CharField(max_length=100, unique=True)),
                ("descripcion", models.CharField(blank=True, max_length=200)),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("activo", models.BooleanField(default=False)),
            ],
            options={
                "ordering": ["nombre"],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("activo", True)),
                        fields=("activo",),
                        name="un_solo_perfil_activo",
                    )
                ],
            },
        ),
        migrations.AddField(
            model_name="perfil",
            name="mcp_habilitados",
            field=models.ManyToManyField(
                blank=True, related_name="perfiles", to="voice_agent_panel.servidormcp"
            ),
        ),
        migrations.AddField(
            model_name="perfil",
            name="hooks_habilitados",
            field=models.ManyToManyField(
                blank=True, related_name="perfiles", to="voice_agent_panel.hook"
            ),
        ),
        # FKs primero anulables, para poder sembrar las filas existentes.
        migrations.AddField(
            model_name="versionprompt",
            name="perfil",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="versiones",
                to="voice_agent_panel.perfil",
            ),
        ),
        migrations.AddField(
            model_name="ajusteagente",
            name="perfil",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="ajustes",
                to="voice_agent_panel.perfil",
            ),
        ),
        migrations.AddField(
            model_name="herramienta",
            name="perfil",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="herramientas",
                to="voice_agent_panel.perfil",
            ),
        ),
        # Las unicidades globales dejan paso a las unicidades por perfil.
        migrations.RemoveConstraint(
            model_name="versionprompt",
            name="una_sola_version_activa",
        ),
        migrations.AlterField(
            model_name="ajusteagente",
            name="clave",
            field=models.CharField(max_length=100),
        ),
        migrations.AlterField(
            model_name="herramienta",
            name="nombre",
            field=models.CharField(max_length=100),
        ),
        migrations.RunPython(_sembrar_perfil_por_defecto, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="versionprompt",
            name="perfil",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="versiones",
                to="voice_agent_panel.perfil",
            ),
        ),
        migrations.AlterField(
            model_name="ajusteagente",
            name="perfil",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="ajustes",
                to="voice_agent_panel.perfil",
            ),
        ),
        migrations.AlterField(
            model_name="herramienta",
            name="perfil",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="herramientas",
                to="voice_agent_panel.perfil",
            ),
        ),
        migrations.AddConstraint(
            model_name="versionprompt",
            constraint=models.UniqueConstraint(
                condition=models.Q(("activa", True)),
                fields=("perfil",),
                name="una_sola_version_activa_por_perfil",
            ),
        ),
        migrations.AddConstraint(
            model_name="ajusteagente",
            constraint=models.UniqueConstraint(
                fields=("perfil", "clave"), name="ajuste_unico_por_perfil"
            ),
        ),
        migrations.AddConstraint(
            model_name="herramienta",
            constraint=models.UniqueConstraint(
                fields=("perfil", "nombre"), name="herramienta_unica_por_perfil"
            ),
        ),
        # Al final, cuando la siembra ya los ha leído: los interruptores
        # globales viven ahora en los M2M del perfil.
        migrations.RemoveField(
            model_name="servidormcp",
            name="habilitado",
        ),
        migrations.RemoveField(
            model_name="hook",
            name="habilitado",
        ),
    ]
