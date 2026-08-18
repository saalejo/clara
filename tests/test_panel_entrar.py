"""El login del panel no puede dejarse probar contraseñas en bucle.

El panel da ejecución de comandos en la placa y está publicado en internet. Y
el freno importa incluso si nadie acierta: cada intento cuesta un PBKDF2 de la
misma CPU que sintetiza la voz del agente.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from voice_agent_panel.acceso import limitador_de_entrada

pytestmark = pytest.mark.django_db

CLAVE = "una-clave-larga-de-prueba"


@pytest.fixture(autouse=True)
def _cubo_limpio() -> Iterator[None]:
    limitador_de_entrada().limpiar()
    yield
    limitador_de_entrada().limpiar()


@pytest.fixture
def usuario() -> User:
    return User.objects.create_user(username="operador", password=CLAVE)


def _fallar(cliente: Client, ip: str = "1.1.1.1") -> int:
    respuesta = cliente.post(
        reverse("login"),
        {"username": "operador", "password": "no-es"},
        headers={"cf-connecting-ip": ip},
    )
    return int(respuesta.status_code)


def test_la_pagina_de_entrar_sigue_siendo_publica(client: Client) -> None:
    # Es el test que atrapa la pérdida de `login_not_required`: una subclase de
    # LoginView que redefina `dispatch` deja el login detrás del login, y el
    # síntoma es un bucle de redirecciones que no se parece a su causa.
    respuesta = client.get(reverse("login"))
    assert respuesta.status_code == 200


def test_la_contraseña_correcta_entra(client: Client, usuario: User) -> None:
    respuesta = client.post(reverse("login"), {"username": "operador", "password": CLAVE})
    assert respuesta.status_code == 302
    assert respuesta.url == "/panel/"


def test_insistir_acaba_en_429(client: Client, usuario: User) -> None:
    for _ in range(5):
        assert _fallar(client) == 200
    assert _fallar(client) == 429


def test_durante_el_bloqueo_no_entra_ni_la_contraseña_buena(client: Client, usuario: User) -> None:
    for _ in range(5):
        _fallar(client)
    respuesta = client.post(
        reverse("login"),
        {"username": "operador", "password": CLAVE},
        headers={"cf-connecting-ip": "1.1.1.1"},
    )
    assert respuesta.status_code == 429
    assert not respuesta.wsgi_request.user.is_authenticated


def test_el_aviso_dice_cuánto_falta(client: Client, usuario: User) -> None:
    for _ in range(6):
        _fallar(client)
    respuesta = client.post(
        reverse("login"),
        {"username": "operador", "password": "no-es"},
        headers={"cf-connecting-ip": "1.1.1.1"},
    )
    assert "Demasiados intentos" in respuesta.content.decode()
    assert "minuto" in respuesta.content.decode()


def test_acertar_limpia_el_cubo(client: Client, usuario: User) -> None:
    for _ in range(4):
        _fallar(client)
    client.post(
        reverse("login"),
        {"username": "operador", "password": CLAVE},
        headers={"cf-connecting-ip": "1.1.1.1"},
    )
    for _ in range(5):
        assert _fallar(client) == 200


def test_el_bloqueo_es_por_ip(client: Client, usuario: User) -> None:
    for _ in range(5):
        _fallar(client, ip="1.1.1.1")
    assert _fallar(client, ip="1.1.1.1") == 429
    assert _fallar(client, ip="2.2.2.2") == 200
