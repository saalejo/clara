"""El saludo comercial gana el aviso de privacidad (Ley 1581 arts. 9 y 12).

El saludo nuevo de `voice_agent_core.prompts.SALUDO_MARKETING` anuncia que
Clara es una IA, que la conversación se graba y se transcribe, y dónde está la
política de datos. Esta migración lo lleva a la base ya sembrada: si la versión
activa del perfil Marketing conserva el saludo de fábrica **sin editar**, se
crea una versión nueva con el texto nuevo (el historial es inmutable: nunca se
reescribe una versión); si alguien lo editó desde el panel, no se toca nada —
no se pisan ediciones— y el aviso hay que incorporarlo a mano.

Como toda migración de perfiles, NO despliega: tras migrar sigue haciendo falta
el ciclo exportar → reiniciar (el botón Desplegar del panel).
"""

from django.db import migrations

#: El saludo de fábrica ANTERIOR, literal: la constante de core ya cambió y la
#: comparación necesita el texto que sembró la migración 0007 en su día.
_SALUDO_ANTERIOR = (
    "Buenas, le habla Clara, de Voz Digital. Nosotros diseñamos agentes de voz a "
    "la medida de cada negocio, y de hecho yo misma soy uno. Cuénteme, ¿cómo se "
    "llama y qué negocio tiene?"
)


def _anunciar_privacidad(apps, schema_editor):
    from voice_agent_core.prompts import SALUDO_MARKETING

    Perfil = apps.get_model("voice_agent_panel", "Perfil")
    VersionPrompt = apps.get_model("voice_agent_panel", "VersionPrompt")

    marketing = Perfil.objects.filter(nombre="Marketing").first()
    if marketing is None:
        return
    activa = VersionPrompt.objects.filter(perfil=marketing, activa=True).first()
    # Solo se actúa sobre el texto de fábrica intacto: cualquier otra cosa es
    # una edición del usuario (o ya el texto nuevo, en instalaciones frescas
    # donde 0007 siembra directamente la constante actual).
    if activa is None or activa.saludo_inicial != _SALUDO_ANTERIOR:
        return
    # Desactivar antes de crear: la restricción parcial
    # `una_sola_version_activa_por_perfil` no admite dos activas ni un instante.
    activa.activa = False
    activa.save(update_fields=["activa"])
    VersionPrompt.objects.create(
        perfil=marketing,
        mensaje="Aviso de privacidad en el saludo",
        prompt_sistema=activa.prompt_sistema,
        alma=activa.alma,
        saludo_inicial=SALUDO_MARKETING,
        muletillas=activa.muletillas,
        activa=True,
    )


def _revertir(apps, schema_editor):
    # El historial es inmutable: revertir es reactivar la versión anterior si
    # la creada por esta migración sigue activa, y borrarla.
    Perfil = apps.get_model("voice_agent_panel", "Perfil")
    VersionPrompt = apps.get_model("voice_agent_panel", "VersionPrompt")

    marketing = Perfil.objects.filter(nombre="Marketing").first()
    if marketing is None:
        return
    creada = VersionPrompt.objects.filter(
        perfil=marketing, activa=True, mensaje="Aviso de privacidad en el saludo"
    ).first()
    if creada is None:
        return
    creada.delete()
    anterior = VersionPrompt.objects.filter(perfil=marketing).order_by("-creado_en").first()
    if anterior is not None:
        anterior.activa = True
        anterior.save(update_fields=["activa"])


class Migration(migrations.Migration):
    dependencies = [
        ("voice_agent_panel", "0007_perfil_marketing"),
    ]

    operations = [
        migrations.RunPython(_anunciar_privacidad, _revertir),
    ]
