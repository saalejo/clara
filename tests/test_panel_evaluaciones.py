"""La página de evaluaciones del panel: la lista fusionada y el detalle.

Lo que se vigila aquí no es el HTML por gusto, sino tres cosas que se rompen
solas: que la lista siga siendo un índice y no un muro (el detalle se mudó a su
página), que los filtros viajen en la URL, y que una llamada sin ficha o sin
resumen se explique en vez de salir como un hueco.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from voice_agent_core.historial import HistorialPacientes
from voice_agent_core.rutas import dir_alertas, dir_resumenes, dir_trazas, ruta_historial

pytestmark = pytest.mark.django_db


@pytest.fixture
def identificado(client: Client) -> Client:
    client.force_login(User.objects.create_user(username="ember", password="una-clave-larga"))
    return client


@pytest.fixture
def data_dir(settings: Any, tmp_path: Path) -> Path:
    settings.DATA_DIR = tmp_path
    return tmp_path


def _alerta(data_dir: Path, fichero: str, **campos: Any) -> None:
    carpeta = dir_alertas(data_dir)
    carpeta.mkdir(parents=True, exist_ok=True)
    datos: dict[str, Any] = {
        "id_llamada": "llamada-x",
        "momento": "2026-08-09T12:00:00",
        "nivel": "rojo",
        "sintomas": "Fiebre de treinta y nueve.",
        "justificacion": "Fiebre alta el cuarto día tras la cirugía.",
    }
    datos.update(campos)
    (carpeta / fichero).write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")


def _resumen(data_dir: Path, fichero: str, **campos: Any) -> None:
    carpeta = dir_resumenes(data_dir)
    carpeta.mkdir(parents=True, exist_ok=True)
    datos: dict[str, Any] = {
        "id_llamada": "llamada-x",
        "momento": "2026-08-09T12:05:00",
        "paciente_y_procedimiento": "Nora, cataratas",
        "sintomas": "Fiebre de treinta y nueve.",
        "decision": "Ir a urgencias hoy mismo.",
        "proximos_pasos": "Llevar la historia clínica.",
        "documentos_consultados": ["apendicitis/guia.pdf"],
    }
    datos.update(campos)
    (carpeta / fichero).write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")


class TestLaLista:
    def test_ensena_una_llamada_fusionada(self, identificado: Client, data_dir: Path) -> None:
        HistorialPacientes(ruta_historial(data_dir)).registrar_llamada(
            "llamada-x", "3001112233", "entrante", nombre="Nora"
        )
        _alerta(data_dir, "20260809-120000.json")
        _resumen(data_dir, "20260809-120500.json")

        cuerpo = identificado.get(reverse("evaluaciones")).content.decode()

        assert "rojo" in cuerpo
        assert "Nora" in cuerpo
        assert "1 llamada" in cuerpo, "la alerta y el resumen son la MISMA llamada"

    def test_la_lista_es_un_indice_y_no_un_muro(self, identificado: Client, data_dir: Path) -> None:
        """El detalle se mudó a su página; si vuelve aquí, la lista deja de servir."""
        _alerta(data_dir, "20260809-120000.json")
        _resumen(data_dir, "20260809-120500.json")

        cuerpo = identificado.get(reverse("evaluaciones")).content.decode()

        assert "Fiebre alta el cuarto día" not in cuerpo
        assert "Ir a urgencias hoy mismo" not in cuerpo

    def test_enlaza_al_detalle(self, identificado: Client, data_dir: Path) -> None:
        _alerta(data_dir, "20260809-120000.json")

        cuerpo = identificado.get(reverse("evaluaciones")).content.decode()

        assert reverse("evaluacion_detalle", args=["llamada-x"]) in cuerpo

    def test_los_filtros_viajan_en_el_enlace_al_detalle(
        self, identificado: Client, data_dir: Path
    ) -> None:
        _alerta(data_dir, "20260809-120000.json")

        cuerpo = identificado.get(reverse("evaluaciones"), {"nivel": "rojo"}).content.decode()

        assert "?nivel=rojo" in cuerpo

    def test_filtra_por_nivel(self, identificado: Client, data_dir: Path) -> None:
        _alerta(data_dir, "20260809-120000.json", id_llamada="la-roja", nivel="rojo")
        _alerta(
            data_dir,
            "20260809-130000.json",
            id_llamada="la-verde",
            nivel="verde",
            momento="2026-08-09T13:00:00",
        )

        cuerpo = identificado.get(reverse("evaluaciones"), {"nivel": "verde"}).content.decode()

        assert "la-verde" in cuerpo
        assert "la-roja" not in cuerpo

    def test_filtra_por_paciente(self, identificado: Client, data_dir: Path) -> None:
        historial = HistorialPacientes(ruta_historial(data_dir))
        historial.registrar_llamada("de-nora", "3001112233", "entrante", nombre="Nora")
        historial.registrar_llamada("de-otro", "3004445566", "entrante")

        cuerpo = identificado.get(
            reverse("evaluaciones"), {"numero": "3001112233"}
        ).content.decode()

        assert "de-nora" in cuerpo
        assert "de-otro" not in cuerpo

    def test_un_filtro_basura_no_revienta_y_avisa(
        self, identificado: Client, data_dir: Path
    ) -> None:
        respuesta = identificado.get(reverse("evaluaciones"), {"nivel": "azul", "desde": "ayer"})

        assert respuesta.status_code == 200
        cuerpo = respuesta.content.decode()
        assert "azul" in cuerpo
        assert "ayer" in cuerpo

    def test_sin_resultados_lo_dice_distinto_de_sin_datos(
        self, identificado: Client, data_dir: Path
    ) -> None:
        _alerta(data_dir, "20260809-120000.json", nivel="rojo")

        con_filtro = identificado.get(reverse("evaluaciones"), {"nivel": "verde"})
        assert "Ninguna llamada cumple estos filtros" in con_filtro.content.decode()

    def test_sin_ficheros_no_revienta(self, identificado: Client, data_dir: Path) -> None:
        respuesta = identificado.get(reverse("evaluaciones"))

        assert respuesta.status_code == 200
        assert "Ninguna todavía" in respuesta.content.decode()

    def test_un_json_corrupto_se_ignora(self, identificado: Client, data_dir: Path) -> None:
        carpeta = dir_alertas(data_dir)
        carpeta.mkdir(parents=True)
        (carpeta / "roto.json").write_text("{a medias", encoding="utf-8")

        assert identificado.get(reverse("evaluaciones")).status_code == 200


class TestElDetalle:
    def test_ensena_alertas_resumen_y_traza(self, identificado: Client, data_dir: Path) -> None:
        HistorialPacientes(ruta_historial(data_dir)).registrar_llamada(
            "llamada-x", "3001112233", "entrante", nombre="Nora"
        )
        _alerta(data_dir, "20260809-120000.json")
        _resumen(data_dir, "20260809-120500.json")
        trazas = dir_trazas(data_dir)
        trazas.mkdir(parents=True, exist_ok=True)
        (trazas / "llamada-x.jsonl").write_text(
            json.dumps(
                {
                    "momento": "2026-08-09T12:01:00",
                    "consulta": "signos de alarma tras cataratas",
                    "pasajes": [
                        {"origen": "cataratas/guia.pdf", "tema": "cataratas", "distancia": 0.31}
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        cuerpo = identificado.get(
            reverse("evaluacion_detalle", args=["llamada-x"])
        ).content.decode()

        assert "Fiebre alta el cuarto día" in cuerpo
        assert "Ir a urgencias hoy mismo" in cuerpo
        assert "signos de alarma tras cataratas" in cuerpo
        assert "cataratas/guia.pdf" in cuerpo
        assert "3001112233" in cuerpo

    def test_una_llamada_sin_ficha_lo_explica(self, identificado: Client, data_dir: Path) -> None:
        _alerta(data_dir, "20260809-120000.json", id_llamada="de-navegador")

        cuerpo = identificado.get(
            reverse("evaluacion_detalle", args=["de-navegador"])
        ).content.decode()

        assert "no abrió ficha de paciente" in cuerpo

    def test_una_llamada_sin_resumen_lo_dice(self, identificado: Client, data_dir: Path) -> None:
        _alerta(data_dir, "20260809-120000.json")

        cuerpo = identificado.get(
            reverse("evaluacion_detalle", args=["llamada-x"])
        ).content.decode()

        assert "terminó sin resumen" in cuerpo

    def test_sin_traza_lo_explica_en_vez_de_dejarlo_en_blanco(
        self, identificado: Client, data_dir: Path
    ) -> None:
        _alerta(data_dir, "20260809-120000.json")

        cuerpo = identificado.get(
            reverse("evaluacion_detalle", args=["llamada-x"])
        ).content.decode()

        assert "Sin consultas al RAG" in cuerpo

    def test_vuelve_a_la_lista_con_los_filtros_puestos(
        self, identificado: Client, data_dir: Path
    ) -> None:
        _alerta(data_dir, "20260809-120000.json")

        cuerpo = identificado.get(
            reverse("evaluacion_detalle", args=["llamada-x"]), {"nivel": "rojo"}
        ).content.decode()

        assert f"{reverse('evaluaciones')}?nivel=rojo" in cuerpo

    def test_un_id_desconocido_da_404(self, identificado: Client, data_dir: Path) -> None:
        respuesta = identificado.get(reverse("evaluacion_detalle", args=["no-existe"]))
        assert respuesta.status_code == 404
