"""La página de prospectos del panel: el padrón comercial y la ficha.

El agente escribe la base y el panel lee — con una única excepción: la
supresión a petición del titular (Ley 1581 art. 8), que es la única escritura
del panel en `prospectos.sqlite3`. Aquí se protege que la página pinte lo
anotado (incluido el registro de consentimiento), que un id inexistente sea un
404 limpio, que una base ausente —perfil clínico activo— salga vacía sin
error, y que la supresión borre en cascada.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from voice_agent_core.prospectos import AlmacenProspectos
from voice_agent_core.rutas import ruta_prospectos

pytestmark = pytest.mark.django_db

ID = "a3f9c2d1e8b7460fa1b2c3d4e5f60718"


@pytest.fixture
def identificado(client: Client) -> Client:
    client.force_login(User.objects.create_user(username="ember", password="una-clave-larga"))
    return client


@pytest.fixture
def data_dir(settings: Any, tmp_path: Path) -> Path:
    settings.DATA_DIR = tmp_path
    return tmp_path


def _con_una_conversacion(data_dir: Path) -> AlmacenProspectos:
    almacen = AlmacenProspectos(ruta_prospectos(data_dir))
    almacen.registrar_conversacion("conv-x", ID, momento=datetime(2026, 8, 14, 12, 0, 0))
    almacen.identificar(ID, nombre="Marta Ruiz", empresa="Óptica Andina", contacto="3001234567")
    almacen.guardar_brief(
        "conv-x",
        ID,
        necesidad="Perder menos citas por llamadas sin contestar",
        caso_de_uso="Agente que agenda citas por teléfono",
        proximos_pasos="El equipo la llama el lunes",
    )
    almacen.anotar_transcripcion("conv-x", "visitante: Tengo una óptica\nagente: Cuénteme más")
    return almacen


def test_el_padron_ensena_las_fichas(identificado: Client, data_dir: Path) -> None:
    _con_una_conversacion(data_dir)

    cuerpo = identificado.get(reverse("prospectos")).content.decode()

    assert "Marta Ruiz" in cuerpo
    assert "Óptica Andina" in cuerpo
    assert "Perder menos citas" in cuerpo
    assert reverse("prospecto", args=[ID]) in cuerpo


def test_la_ficha_ensena_brief_y_transcripcion(identificado: Client, data_dir: Path) -> None:
    _con_una_conversacion(data_dir)

    cuerpo = identificado.get(reverse("prospecto", args=[ID])).content.decode()

    assert "Marta Ruiz" in cuerpo
    assert "3001234567" in cuerpo
    assert "Agente que agenda citas" in cuerpo
    assert "Tengo una óptica" in cuerpo


def test_un_id_inexistente_es_404(identificado: Client, data_dir: Path) -> None:
    _con_una_conversacion(data_dir)

    respuesta = identificado.get(reverse("prospecto", args=["b" * 32]))

    assert respuesta.status_code == 404


def test_sin_base_la_pagina_sale_vacia(identificado: Client, data_dir: Path) -> None:
    # El caso normal con el perfil clínico activo: el fichero ni existe.
    respuesta = identificado.get(reverse("prospectos"))

    assert respuesta.status_code == 200
    assert "Ninguno todavía" in respuesta.content.decode()


# --- El registro de consentimiento -------------------------------------------


def test_la_ficha_ensena_el_consentimiento(identificado: Client, data_dir: Path) -> None:
    almacen = _con_una_conversacion(data_dir)
    almacen.anotar_aviso(
        "conv-x", "Le aviso que esto se graba", momento=datetime(2026, 8, 16, 10, 0)
    )
    almacen.anotar_consentimiento("conv-x")

    cuerpo = identificado.get(reverse("prospecto", args=[ID])).content.decode()

    assert "2026-08-16T10:00:00" in cuerpo
    assert "conducta inequívoca" in cuerpo
    assert "Le aviso que esto se graba" in cuerpo


def test_con_aviso_pero_sin_habla_no_se_reclama_consentimiento(
    identificado: Client, data_dir: Path
) -> None:
    almacen = _con_una_conversacion(data_dir)
    almacen.anotar_aviso("conv-x", "Le aviso que esto se graba")

    cuerpo = identificado.get(reverse("prospecto", args=[ID])).content.decode()

    assert "no llegó a hablar" in cuerpo
    assert "conducta inequívoca" not in cuerpo


def test_una_conversacion_antigua_dice_que_no_hay_registro(
    identificado: Client, data_dir: Path
) -> None:
    _con_una_conversacion(data_dir)

    cuerpo = identificado.get(reverse("prospecto", args=[ID])).content.decode()

    assert "Sin registro de aviso" in cuerpo


# --- La supresión (Ley 1581 art. 8) ------------------------------------------


def test_suprimir_borra_la_ficha_y_redirige_al_padron(identificado: Client, data_dir: Path) -> None:
    almacen = _con_una_conversacion(data_dir)

    respuesta = identificado.post(reverse("prospecto_borrar", args=[ID]))

    assert respuesta.status_code == 302
    assert respuesta["Location"] == reverse("prospectos")
    assert almacen.ficha(ID) is None
    assert almacen.brief("conv-x") is None
    assert identificado.get(reverse("prospecto", args=[ID])).status_code == 404


def test_suprimir_una_ficha_inexistente_avisa_sin_romper(
    identificado: Client, data_dir: Path
) -> None:
    respuesta = identificado.post(reverse("prospecto_borrar", args=["b" * 32]), follow=True)

    assert respuesta.status_code == 200
    assert "No se pudo suprimir" in respuesta.content.decode()


def test_suprimir_solo_admite_post(identificado: Client, data_dir: Path) -> None:
    _con_una_conversacion(data_dir)
    assert identificado.get(reverse("prospecto_borrar", args=[ID])).status_code == 405


def test_suprimir_esta_cerrado_a_anonimos(client: Client, data_dir: Path) -> None:
    _con_una_conversacion(data_dir)

    respuesta = client.post(reverse("prospecto_borrar", args=[ID]))

    assert respuesta.status_code == 302
    assert "/entrar/" in respuesta["Location"]


def test_la_ficha_ofrece_el_boton_de_suprimir(identificado: Client, data_dir: Path) -> None:
    _con_una_conversacion(data_dir)

    cuerpo = identificado.get(reverse("prospecto", args=[ID])).content.decode()

    assert reverse("prospecto_borrar", args=[ID]) in cuerpo
    assert "Suprimir este prospecto" in cuerpo
