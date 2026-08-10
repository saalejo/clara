"""La página de evaluaciones clínicas del panel: alertas y resúmenes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from voice_agent_core.rutas import dir_alertas, dir_resumenes

pytestmark = pytest.mark.django_db


@pytest.fixture
def identificado(client: Client) -> Client:
    client.force_login(User.objects.create_user(username="ember", password="una-clave-larga"))
    return client


@pytest.fixture
def data_dir(settings: Any, tmp_path: Path) -> Path:
    settings.DATA_DIR = tmp_path
    return tmp_path


def test_ensena_alertas_y_resumenes(identificado: Client, data_dir: Path) -> None:
    carpeta = dir_alertas(data_dir)
    carpeta.mkdir(parents=True)
    (carpeta / "20260809-120000.json").write_text(
        json.dumps(
            {
                "id_llamada": "llamada-x",
                "momento": "2026-08-09T12:00:00",
                "nivel": "rojo",
                "sintomas": "Fiebre de treinta y nueve.",
                "justificacion": "Signo de alarma según la guía.",
            }
        ),
        encoding="utf-8",
    )
    resumenes = dir_resumenes(data_dir)
    resumenes.mkdir(parents=True)
    (resumenes / "20260809-121000.json").write_text(
        json.dumps(
            {
                "id_llamada": "llamada-x",
                "momento": "2026-08-09T12:10:00",
                "paciente_y_procedimiento": "Nora, apendicectomía.",
                "sintomas": "Fiebre alta.",
                "decision": "Rojo, urgencias.",
                "referencias": "guía de apendicectomía",
                "proximos_pasos": "Acudir a urgencias ya.",
                "documentos_consultados": ["apendicitis/guia.pdf"],
            }
        ),
        encoding="utf-8",
    )

    respuesta = identificado.get(reverse("evaluaciones"))
    assert respuesta.status_code == 200
    cuerpo = respuesta.content.decode()
    assert "rojo" in cuerpo
    assert "Nora" in cuerpo
    assert "apendicitis/guia.pdf" in cuerpo


def test_sin_ficheros_no_revienta(identificado: Client, data_dir: Path) -> None:
    respuesta = identificado.get(reverse("evaluaciones"))
    assert respuesta.status_code == 200
    assert "Ninguna todavía" in respuesta.content.decode()


def test_un_json_corrupto_se_ignora(identificado: Client, data_dir: Path) -> None:
    carpeta = dir_alertas(data_dir)
    carpeta.mkdir(parents=True)
    (carpeta / "roto.json").write_text("{a medias", encoding="utf-8")

    respuesta = identificado.get(reverse("evaluaciones"))
    assert respuesta.status_code == 200
