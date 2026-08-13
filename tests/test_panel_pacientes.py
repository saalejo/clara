"""La página de pacientes del panel: el padrón, no el expediente.

Existía sin un solo test que la renderizara. Ahora que dejó de repetir las
tarjetas de llamada —que están en Evaluaciones— conviene que quede escrito.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from voice_agent_core.historial import HistorialPacientes
from voice_agent_core.rutas import ruta_historial

pytestmark = pytest.mark.django_db


@pytest.fixture
def identificado(client: Client) -> Client:
    client.force_login(User.objects.create_user(username="ember", password="una-clave-larga"))
    return client


@pytest.fixture
def data_dir(settings: Any, tmp_path: Path) -> Path:
    settings.DATA_DIR = tmp_path
    return tmp_path


def _con_una_llamada(data_dir: Path) -> HistorialPacientes:
    historial = HistorialPacientes(ruta_historial(data_dir))
    historial.registrar_llamada(
        "llamada-x",
        "3001112233",
        "entrante",
        nombre="Nora",
        momento=datetime(2026, 8, 9, 12, 0, 0),
    )
    historial.anotar_alerta("llamada-x", "rojo")
    historial.anotar_resumen(
        "llamada-x",
        paciente_y_procedimiento="Nora, cataratas",
        decision="Ir a urgencias hoy mismo.",
        proximos_pasos="Llevar la historia clínica.",
        procedimiento="cataratas",
    )
    return historial


def test_ensena_las_fichas(identificado: Client, data_dir: Path) -> None:
    _con_una_llamada(data_dir)

    cuerpo = identificado.get(reverse("pacientes")).content.decode()

    assert "3001112233" in cuerpo
    assert "Nora" in cuerpo
    assert "rojo" in cuerpo
    assert "cataratas" in cuerpo


def test_ya_no_repite_las_llamadas(identificado: Client, data_dir: Path) -> None:
    _con_una_llamada(data_dir)

    cuerpo = identificado.get(reverse("pacientes")).content.decode()

    assert "Llamadas registradas" not in cuerpo


def test_la_ficha_enlaza_a_sus_evaluaciones(identificado: Client, data_dir: Path) -> None:
    _con_una_llamada(data_dir)

    cuerpo = identificado.get(reverse("pacientes")).content.decode()

    assert f"{reverse('evaluaciones')}?numero=3001112233" in cuerpo
    assert reverse("evaluacion_detalle", args=["llamada-x"]) in cuerpo


def test_sin_pacientes_lo_dice(identificado: Client, data_dir: Path) -> None:
    respuesta = identificado.get(reverse("pacientes"))

    assert respuesta.status_code == 200
    assert "Ninguno todavía" in respuesta.content.decode()
