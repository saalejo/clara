"""La página de prospectos del panel: el padrón comercial y la ficha.

El agente escribe la base y el panel solo lee, como con los pacientes; aquí lo
que se protege es que la página pinte lo anotado, que un id inexistente sea un
404 limpio y que una base ausente —perfil clínico activo— salga vacía sin error.
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
