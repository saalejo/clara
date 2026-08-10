"""Perfiles: solo uno activo, y lo que se exporta sale siempre del activo."""

from __future__ import annotations

from typing import cast

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from voice_agent_core.runtime import RuntimeConfig
from voice_agent_panel import control
from voice_agent_panel.exporter import construir_runtime, construir_snapshot_settings
from voice_agent_panel.models import (
    AjusteAgente,
    Despliegue,
    Herramienta,
    Hook,
    Perfil,
    ServidorMCP,
    VersionPrompt,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def perfil() -> Perfil:
    """El perfil "Por defecto" que siembra la migración, ya activo."""
    return cast(Perfil, Perfil.objects.get(activo=True))


@pytest.fixture
def identificado(client: Client) -> Client:
    client.force_login(User.objects.create_user(username="ember", password="una-clave-larga"))
    return client


@pytest.fixture(autouse=True)
def _sin_dbus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Los tests no hablan con systemd: no hay bus en el entorno de pruebas."""

    def _estado(unidad: str | None = None) -> control.EstadoUnidad:
        return control.EstadoUnidad(
            unidad="voice-agent.service",
            active_state="active",
            sub_state="running",
            resultado="success",
        )

    monkeypatch.setattr(control, "estado", _estado)


# --- Modelo ------------------------------------------------------------------


def test_la_migracion_siembra_el_perfil_por_defecto(perfil: Perfil) -> None:
    assert perfil.nombre == "Por defecto"


def test_activar_desactiva_el_anterior(perfil: Perfil) -> None:
    otro = Perfil.objects.create(nombre="Nocturno")

    otro.activar()

    perfil.refresh_from_db()
    assert not perfil.activo
    assert Perfil.activo_o_none() == otro


def test_dos_activos_a_la_vez_no_caben(perfil: Perfil) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        Perfil.objects.create(nombre="Intruso", activo=True)


def test_cada_perfil_tiene_su_version_activa(perfil: Perfil) -> None:
    otro = Perfil.objects.create(nombre="Nocturno")
    VersionPrompt.objects.create(
        perfil=perfil, prompt_sistema="diurno", saludo_inicial="hola", muletillas={}, activa=True
    )
    VersionPrompt.objects.create(
        perfil=otro, prompt_sistema="nocturno", saludo_inicial="hola", muletillas={}, activa=True
    )

    activa = VersionPrompt.activa_de(otro)
    assert activa is not None and activa.prompt_sistema == "nocturno"


def test_duplicar_copia_todo_sin_activar(perfil: Perfil) -> None:
    VersionPrompt.objects.create(
        perfil=perfil,
        prompt_sistema="base",
        alma="Parco.",
        saludo_inicial="hola",
        muletillas={},
        activa=True,
    )
    AjusteAgente.objects.create(perfil=perfil, clave="llm_temperature", valor="0.3")
    Herramienta.objects.create(perfil=perfil, nombre="estado_del_sistema", habilitada=False)
    servidor = ServidorMCP.objects.create(nombre="ficheros", transporte="stdio", comando="mcp-fs")
    perfil.mcp_habilitados.add(servidor)

    copia = perfil.duplicar("Copia")

    assert not copia.activo
    version = VersionPrompt.activa_de(copia)
    assert version is not None and version.alma == "Parco."
    assert copia.ajustes.get().clave == "llm_temperature"
    assert not copia.herramientas.get(nombre="estado_del_sistema").habilitada
    assert list(copia.mcp_habilitados.all()) == [servidor]
    # El original sigue intacto y activo.
    assert Perfil.activo_o_none() == perfil


# --- Exportación -------------------------------------------------------------


def test_se_exporta_el_prompt_del_perfil_activo(perfil: Perfil) -> None:
    otro = Perfil.objects.create(nombre="Nocturno")
    VersionPrompt.objects.create(
        perfil=perfil, prompt_sistema="diurno", saludo_inicial="hola", muletillas={}, activa=True
    )
    VersionPrompt.objects.create(
        perfil=otro, prompt_sistema="nocturno", saludo_inicial="hola", muletillas={}, activa=True
    )

    assert construir_runtime().prompt.prompt_sistema == "diurno"

    otro.activar()
    runtime = construir_runtime()
    assert runtime.prompt.prompt_sistema == "nocturno"
    assert runtime.perfil == "Nocturno"


def test_se_exportan_los_ajustes_del_perfil_activo(perfil: Perfil) -> None:
    otro = Perfil.objects.create(nombre="Nocturno")
    AjusteAgente.objects.create(perfil=perfil, clave="llm_temperature", valor="0.3")
    AjusteAgente.objects.create(perfil=otro, clave="llm_temperature", valor="0.9")

    assert construir_snapshot_settings() == {"llm_temperature": 0.3}

    otro.activar()
    assert construir_snapshot_settings() == {"llm_temperature": 0.9}


def test_un_runtime_viejo_sin_perfil_sigue_valiendo() -> None:
    # Los runtime.json anteriores a los perfiles no llevan la clave.
    runtime = RuntimeConfig.model_validate({"version": 1})
    assert runtime.perfil == ""


# --- Vistas ------------------------------------------------------------------


def test_crear_un_perfil_no_lo_activa(identificado: Client, perfil: Perfil) -> None:
    identificado.post(reverse("perfiles"), {"nombre": "Nocturno", "descripcion": ""})

    nuevo = Perfil.objects.get(nombre="Nocturno")
    assert not nuevo.activo
    assert Perfil.activo_o_none() == perfil


def test_activar_desde_la_vista(identificado: Client, perfil: Perfil) -> None:
    otro = Perfil.objects.create(nombre="Nocturno")

    identificado.post(reverse("perfil_activar", args=[otro.pk]))

    assert Perfil.activo_o_none() == otro


def test_seleccionar_cambia_lo_que_se_edita(identificado: Client, perfil: Perfil) -> None:
    # Con otro perfil seleccionado, la página del prompt siembra y edita el
    # suyo, no el del activo.
    otro = Perfil.objects.create(nombre="Nocturno")

    identificado.post(reverse("perfil_seleccionar", args=[otro.pk]))
    identificado.get(reverse("prompt"))

    assert VersionPrompt.activa_de(otro) is not None
    assert VersionPrompt.activa_de(perfil) is None


def test_duplicar_desde_la_vista(identificado: Client, perfil: Perfil) -> None:
    identificado.post(reverse("perfil_duplicar", args=[perfil.pk]), {"nombre": ""})

    assert Perfil.objects.filter(nombre="Por defecto (copia)").exists()


def test_duplicar_con_nombre_repetido_avisa(identificado: Client, perfil: Perfil) -> None:
    respuesta = identificado.post(
        reverse("perfil_duplicar", args=[perfil.pk]), {"nombre": "Por defecto"}, follow=True
    )

    assert Perfil.objects.count() == 1
    assert "Ya existe" in respuesta.content.decode()


def test_el_perfil_activo_no_se_borra(identificado: Client, perfil: Perfil) -> None:
    identificado.post(reverse("perfil_borrar", args=[perfil.pk]))

    assert Perfil.objects.filter(pk=perfil.pk).exists()


def test_el_ultimo_perfil_no_se_borra(identificado: Client, perfil: Perfil) -> None:
    otro = Perfil.objects.create(nombre="Nocturno")
    otro.activar()
    identificado.post(reverse("perfil_borrar", args=[perfil.pk]))

    identificado.post(reverse("perfil_borrar", args=[otro.pk]))

    assert Perfil.objects.count() == 1


def test_borrar_arrastra_lo_suyo(identificado: Client, perfil: Perfil) -> None:
    otro = Perfil.objects.create(nombre="Nocturno")
    VersionPrompt.objects.create(
        perfil=otro, prompt_sistema="x", saludo_inicial="hola", muletillas={}, activa=True
    )
    AjusteAgente.objects.create(perfil=otro, clave="llm_temperature", valor="0.9")

    identificado.post(reverse("perfil_borrar", args=[otro.pk]))

    assert not Perfil.objects.filter(nombre="Nocturno").exists()
    assert not VersionPrompt.objects.filter(prompt_sistema="x").exists()
    assert not AjusteAgente.objects.exists()


def test_la_seleccion_de_mcp_es_por_perfil(identificado: Client, perfil: Perfil) -> None:
    servidor = ServidorMCP.objects.create(nombre="ficheros", transporte="stdio", comando="mcp-fs")

    identificado.post(reverse("mcp"), {"habilitado": [str(servidor.pk)]})

    assert list(perfil.mcp_habilitados.all()) == [servidor]

    identificado.post(reverse("mcp"), {})
    assert not perfil.mcp_habilitados.exists()


def test_la_seleccion_de_hooks_es_por_perfil(identificado: Client, perfil: Perfil) -> None:
    hook = Hook.objects.create(
        nombre="corrige",
        evento="transcripcion_lista",
        accion="reescribir",
        patron="nanopi",
        reemplazo="NanoPi",
    )

    identificado.post(reverse("hooks"), {"habilitado": [str(hook.pk)]})

    assert list(perfil.hooks_habilitados.all()) == [hook]


def test_la_portada_avisa_si_el_perfil_activo_no_se_ha_desplegado(
    identificado: Client, perfil: Perfil
) -> None:
    Despliegue.objects.create(
        resultado=Despliegue.Resultado.REINICIADO,
        instantanea_runtime={"perfil": "Por defecto"},
    )
    otro = Perfil.objects.create(nombre="Nocturno")
    otro.activar()

    respuesta = identificado.get(reverse("panel"))

    assert "el último despliegue" in respuesta.content.decode()
