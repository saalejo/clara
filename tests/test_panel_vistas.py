"""Las vistas del panel: acceso, CSRF y las acciones que tocan el servicio.

El panel ejecuta comandos en la placa y para el agente, así que lo primero que
hay que garantizar es que nada de eso es accesible sin identificarse y que no se
puede disparar desde otra página.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from voice_agent_panel import control
from voice_agent_panel.models import Herramienta, Hook, Perfil, ServidorMCP, VersionPrompt

pytestmark = pytest.mark.django_db

#: Todo lo que debe estar cerrado a cal y canto.
RUTAS_PRIVADAS = [
    "panel",
    "perfiles",
    "prompt",
    "ajustes",
    "herramientas",
    "conocimiento",
    "mcp",
    "hooks",
    "tareas",
    "pacientes",
    "calidad",
    "logs",
    "despliegues",
]

#: Las que solo admiten POST. Se comprueban aparte porque con sesión abierta
#: darían 405, no 200; lo que importa es que sin identificarse tampoco se llegue.
RUTAS_PRIVADAS_POST = [
    "tema_crear",
    "tema_borrar",
    "documento_borrar",
    "calidad_lanzar",
]


@pytest.fixture
def usuario() -> User:
    return User.objects.create_user(username="ember", password="una-clave-larga")


@pytest.fixture
def identificado(client: Client, usuario: User) -> Client:
    client.force_login(usuario)
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
    monkeypatch.setattr(control, "estado_calidad", _estado)
    for accion in ("arrancar", "parar", "reiniciar", "lanzar_ingesta", "lanzar_calidad"):
        monkeypatch.setattr(control, accion, lambda *a, **k: "/job/1")


# --- Acceso ------------------------------------------------------------------


@pytest.mark.parametrize("nombre", RUTAS_PRIVADAS)
def test_sin_identificarse_no_se_entra(client: Client, nombre: str) -> None:
    respuesta = client.get(reverse(nombre))
    assert respuesta.status_code == 302
    assert "/entrar/" in respuesta["Location"]


def test_la_comprobacion_de_salud_es_publica(client: Client) -> None:
    assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize("nombre", RUTAS_PRIVADAS_POST)
def test_las_acciones_tambien_estan_cerradas(client: Client, nombre: str) -> None:
    # El middleware corre antes que `@require_POST`, así que un anónimo se topa
    # con el login y no con un 405 que le confirmaría que la ruta existe.
    respuesta = client.post(reverse(nombre))
    assert respuesta.status_code == 302
    assert "/entrar/" in respuesta["Location"]


@pytest.mark.parametrize("nombre", RUTAS_PRIVADAS)
def test_identificado_se_entra(identificado: Client, nombre: str) -> None:
    assert identificado.get(reverse(nombre)).status_code == 200


# --- Métodos y CSRF ----------------------------------------------------------


@pytest.mark.parametrize("nombre,argumentos", [("desplegar", []), ("servicio", ["reiniciar"])])
def test_las_acciones_no_admiten_get(
    identificado: Client, nombre: str, argumentos: list[Any]
) -> None:
    assert identificado.get(reverse(nombre, args=argumentos)).status_code == 405


def test_desplegar_sin_csrf_se_rechaza(usuario: User) -> None:
    cliente = Client(enforce_csrf_checks=True)
    cliente.force_login(usuario)

    assert cliente.post(reverse("desplegar")).status_code == 403


# --- Acciones ----------------------------------------------------------------


def test_desplegar_exporta_y_reinicia(
    identificado: Client, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from django.test import override_settings

    reinicios: list[str] = []

    def _reiniciar(*a: Any, **k: Any) -> str:
        reinicios.append("sí")
        return "/job/1"

    monkeypatch.setattr(control, "reiniciar", _reiniciar)

    with override_settings(DATA_DIR=tmp_path):
        respuesta = identificado.post(reverse("desplegar"), follow=True)

    assert respuesta.status_code == 200
    assert reinicios == ["sí"]
    assert (tmp_path / "config" / "settings.json").is_file()
    assert (tmp_path / "config" / "runtime.json").is_file()


def test_solo_exportar_no_reinicia(
    identificado: Client, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from django.test import override_settings

    reinicios: list[str] = []

    def _reiniciar(*a: Any, **k: Any) -> str:
        reinicios.append("sí")
        return "/job/1"

    monkeypatch.setattr(control, "reiniciar", _reiniciar)

    with override_settings(DATA_DIR=tmp_path):
        identificado.post(reverse("desplegar"), {"reiniciar": "no"}, follow=True)

    assert reinicios == []
    assert (tmp_path / "config" / "settings.json").is_file()


def test_una_accion_desconocida_no_hace_nada(identificado: Client) -> None:
    respuesta = identificado.post(reverse("servicio", args=["formatear"]), follow=True)
    assert respuesta.status_code == 200


def test_el_prompt_se_siembra_la_primera_vez(identificado: Client) -> None:
    # Al abrirlo sin nada guardado, se enseña lo que el agente trae de fábrica,
    # que es lo que de verdad está corriendo.
    from voice_agent_core.prompts import PROMPT_SISTEMA

    identificado.get(reverse("prompt"))

    activa = VersionPrompt.activa_de(Perfil.objects.get(activo=True))
    assert activa is not None
    assert activa.prompt_sistema == PROMPT_SISTEMA


def test_guardar_el_prompt_crea_una_version_nueva(identificado: Client) -> None:
    perfil = Perfil.objects.get(activo=True)
    identificado.get(reverse("prompt"))
    original = VersionPrompt.activa_de(perfil)
    assert original is not None

    identificado.post(
        reverse("prompt"),
        {
            "mensaje": "más seco",
            "prompt_sistema": "Responde en español.",
            "alma": "Eres parco.",
            "saludo_inicial": "Hola.",
            "muletillas_texto": "consulta:\n  Un momento.",
        },
    )

    assert VersionPrompt.objects.count() == 2
    nueva = VersionPrompt.activa_de(perfil)
    assert nueva is not None and nueva.pk != original.pk
    assert nueva.alma == "Eres parco."
    assert nueva.muletillas == {"consulta": ["Un momento."]}
    # El historial no se reescribe.
    original.refresh_from_db()
    assert not original.activa


def test_volver_a_una_version_la_copia(identificado: Client) -> None:
    perfil = Perfil.objects.get(activo=True)
    identificado.get(reverse("prompt"))
    vieja = VersionPrompt.activa_de(perfil)
    assert vieja is not None
    identificado.post(
        reverse("prompt"),
        {
            "mensaje": "cambio",
            "prompt_sistema": "Otra cosa.",
            "alma": "",
            "saludo_inicial": "Hola.",
            "muletillas_texto": "",
        },
    )

    identificado.post(reverse("volver_a_version", args=[vieja.pk]))

    assert VersionPrompt.objects.count() == 3
    activa = VersionPrompt.activa_de(perfil)
    assert activa is not None
    assert activa.prompt_sistema == vieja.prompt_sistema
    assert activa.pk != vieja.pk


def test_guardar_ajustes_borra_los_que_se_vacian(identificado: Client) -> None:
    from voice_agent_panel.models import AjusteAgente

    AjusteAgente.objects.create(
        perfil=Perfil.objects.get(activo=True), clave="llm_max_tokens", valor="500"
    )

    identificado.post(reverse("ajustes"), {"llm_temperature": "0.3"})

    claves = {a.clave: a.valor for a in AjusteAgente.objects.all()}
    assert claves == {"llm_temperature": "0.3"}


def test_las_herramientas_se_guardan_desde_el_estado_publicado(
    identificado: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    import voice_agent_panel.views as vistas
    from voice_agent_core.estado import EstadoArranque, HerramientaExpuesta

    estado = EstadoArranque.ahora(
        herramientas=[
            HerramientaExpuesta(nombre="buscar_en_documentos"),
            HerramientaExpuesta(nombre="estado_del_sistema"),
        ]
    )
    monkeypatch.setattr(vistas, "leer_estado", lambda *_: estado)

    identificado.post(reverse("herramientas"), {"activa": ["buscar_en_documentos"]})

    assert Herramienta.objects.get(nombre="buscar_en_documentos").habilitada
    assert not Herramienta.objects.get(nombre="estado_del_sistema").habilitada


def test_alta_de_un_servidor_mcp(identificado: Client) -> None:
    respuesta = identificado.post(
        reverse("mcp_nuevo"),
        {
            "nombre": "ficheros",
            "transporte": "stdio",
            "comando": "mcp-fs",
            "timeout_secs": "20",
            "argumentos_texto": "--raiz /data",
            "entorno_texto": "TOKEN=${MI_CLAVE}",
            "cabeceras_texto": "",
            "herramientas_texto": "leer, escribir",
        },
    )

    assert respuesta.status_code == 302
    servidor = ServidorMCP.objects.get(nombre="ficheros")
    assert servidor.argumentos == ["--raiz", "/data"]
    assert servidor.entorno == {"TOKEN": "${MI_CLAVE}"}
    assert servidor.herramientas_permitidas == ["leer", "escribir"]


def test_un_mcp_stdio_sin_comando_no_se_guarda(identificado: Client) -> None:
    # La validación es la misma que usará el agente: el modelo de pydantic.
    respuesta = identificado.post(
        reverse("mcp_nuevo"),
        {"nombre": "roto", "transporte": "stdio", "comando": "", "timeout_secs": "20"},
    )

    assert respuesta.status_code == 200
    assert not ServidorMCP.objects.exists()


def test_alta_de_un_hook(identificado: Client) -> None:
    respuesta = identificado.post(
        reverse("hook_nuevo"),
        {
            "nombre": "corrige-nanopi",
            "orden": "10",
            "evento": "transcripcion_lista",
            "accion": "reescribir",
            "timeout_secs": "5",
            "patron": "nanopi",
            "reemplazo": "NanoPi",
            "comando_texto": "",
            "entorno_texto": "",
        },
    )

    assert respuesta.status_code == 302
    hook = Hook.objects.get(nombre="corrige-nanopi")
    # No pertenece a ningún perfil: nace apagado, que es lo que se quiere.
    assert not hook.perfiles.exists()


def test_un_hook_con_un_patron_que_no_compila_se_rechaza(identificado: Client) -> None:
    respuesta = identificado.post(
        reverse("hook_nuevo"),
        {
            "nombre": "roto",
            "orden": "10",
            "evento": "transcripcion_lista",
            "accion": "reescribir",
            "timeout_secs": "5",
            "patron": "(sin cerrar",
            "reemplazo": "x",
            "comando_texto": "",
            "entorno_texto": "",
        },
    )

    assert respuesta.status_code == 200
    assert not Hook.objects.exists()


def test_no_se_puede_reescribir_un_evento_sin_texto(identificado: Client) -> None:
    respuesta = identificado.post(
        reverse("hook_nuevo"),
        {
            "nombre": "imposible",
            "orden": "10",
            "evento": "usuario_termino",
            "accion": "reescribir",
            "timeout_secs": "5",
            "patron": "x",
            "reemplazo": "y",
            "comando_texto": "",
            "entorno_texto": "",
        },
    )

    assert respuesta.status_code == 200
    assert not Hook.objects.exists()


def test_un_patron_larguisimo_se_rechaza(identificado: Client) -> None:
    # Un patrón complejo sobre una transcripción puede congelar el bucle de
    # audio del agente.
    respuesta = identificado.post(
        reverse("hook_nuevo"),
        {
            "nombre": "largo",
            "orden": "10",
            "evento": "transcripcion_lista",
            "accion": "reescribir",
            "timeout_secs": "5",
            "patron": "(a+)+" * 60,
            "reemplazo": "x",
            "comando_texto": "",
            "entorno_texto": "",
        },
    )

    assert respuesta.status_code == 200
    assert not Hook.objects.exists()
