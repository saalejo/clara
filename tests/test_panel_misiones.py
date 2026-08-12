"""Las misiones del agente vistas desde el panel: se leen, se cancelan, no se tocan.

El contrato que sostiene este fichero es el de un fichero, un escritor. El
panel enseña `misiones_agente.json` y **no lo escribe jamás**: si lo hiciera,
pisaría lo que el agente acabara de apuntar en mitad de una conversación. Lo
que escribe es la petición de cancelación, en su propio fichero, que el
planificador recoge por mtime.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from voice_agent_core.misiones import (
    EstadoMision,
    MisionesAgente,
    MisionPuntual,
    cargar_cancelaciones,
)
from voice_agent_core.rutas import escribir_json_atomico, ruta_misiones_agente

pytestmark = pytest.mark.django_db

CUANDO = datetime.now() + timedelta(hours=3)


@pytest.fixture
def identificado(client: Client) -> Client:
    client.force_login(User.objects.create_user(username="ember", password="una-clave-larga"))
    return client


@pytest.fixture
def data_dir(settings: Any, tmp_path: Path) -> Path:
    settings.DATA_DIR = tmp_path
    return tmp_path


def mision(**cambios: Any) -> MisionPuntual:
    base: dict[str, Any] = {
        "id": "agenda-20260813-1700-a3f9",
        "cuando": CUANDO,
        "mision": "Retomar el control del día cinco.",
        "contacto_numero": "3046411802",
        "contacto_nombre": "Nora",
    }
    base.update(cambios)
    return MisionPuntual.model_validate(base)


def escribir_misiones(data_dir: Path, *misiones: MisionPuntual) -> None:
    """Escribe lo que habría escrito el agente. Aquí el panel solo lee."""
    escribir_json_atomico(
        ruta_misiones_agente(data_dir),
        MisionesAgente(misiones=list(misiones)).model_dump(mode="json"),
    )


class TestLaLista:
    def test_ensena_las_pendientes_del_agente(self, identificado: Client, data_dir: Path) -> None:
        escribir_misiones(data_dir, mision())
        respuesta = identificado.get(reverse("tareas"))
        assert respuesta.status_code == 200
        assert b"Nora" in respuesta.content
        assert b"Retomar el control" in respuesta.content

    def test_no_ensena_las_ya_terminadas(self, identificado: Client, data_dir: Path) -> None:
        escribir_misiones(data_dir, mision(estado=EstadoMision.EJECUTADA))
        respuesta = identificado.get(reverse("tareas"))
        assert b"Retomar el control" not in respuesta.content

    def test_sin_fichero_la_pagina_sigue_funcionando(
        self, identificado: Client, data_dir: Path
    ) -> None:
        # El panel arranca antes que el agente, y hasta que alguien no agende
        # nada por voz este fichero no existe.
        respuesta = identificado.get(reverse("tareas"))
        assert respuesta.status_code == 200
        assert b"Ninguna pendiente" in respuesta.content

    def test_distingue_un_reintento_de_una_pedida_por_voz(
        self, identificado: Client, data_dir: Path
    ) -> None:
        escribir_misiones(
            data_dir, mision(origen="reintento", intento=1, id_tarea_origen="revision-abuela")
        )
        respuesta = identificado.get(reverse("tareas"))
        assert b"revision-abuela" in respuesta.content


class TestLaCancelacion:
    def test_escribe_el_fichero_que_el_agente_lee(
        self, identificado: Client, data_dir: Path
    ) -> None:
        una = mision()
        escribir_misiones(data_dir, una)
        respuesta = identificado.post(reverse("mision_cancelar"), {"id_mision": una.id})
        assert respuesta.status_code == 302
        # `cargar_cancelaciones` es el lector real del agente.
        assert cargar_cancelaciones(data_dir).ids == [una.id]

    def test_poda_los_ids_que_ya_no_estan_pendientes(
        self, identificado: Client, data_dir: Path
    ) -> None:
        # Si no, el fichero crece sin fin: el agente no lo limpia, porque no es
        # suyo.
        viva = mision(id="agenda-viva")
        muerta = mision(id="agenda-muerta", estado=EstadoMision.EJECUTADA)
        escribir_misiones(data_dir, viva, muerta)
        identificado.post(reverse("mision_cancelar"), {"id_mision": muerta.id})
        identificado.post(reverse("mision_cancelar"), {"id_mision": viva.id})
        assert cargar_cancelaciones(data_dir).ids == ["agenda-viva"]

    def test_cancelar_una_que_ya_no_esta_avisa_sin_romper(
        self, identificado: Client, data_dir: Path
    ) -> None:
        # El botón puede ser de una misión que sonó mientras se miraba la
        # página.
        escribir_misiones(data_dir)
        respuesta = identificado.post(reverse("mision_cancelar"), {"id_mision": "agenda-vieja"})
        assert respuesta.status_code == 302
        assert cargar_cancelaciones(data_dir).ids == []

    def test_por_get_no_hace_nada(self, identificado: Client, data_dir: Path) -> None:
        assert identificado.get(reverse("mision_cancelar")).status_code == 405

    def test_sin_identificarse_no_se_puede_cancelar(self, client: Client, data_dir: Path) -> None:
        # `LoginRequiredMiddleware` cierra todo por defecto; esta vista no lleva
        # `login_not_required`, así que tiene que redirigir a la entrada.
        respuesta = client.post(reverse("mision_cancelar"), {"id_mision": "agenda-x"})
        assert respuesta.status_code == 302
        assert reverse("login") in respuesta["Location"]

    def test_el_panel_no_toca_el_fichero_del_agente(
        self, identificado: Client, data_dir: Path
    ) -> None:
        # **El test que sostiene la doctrina.** Si algún día alguien "arregla"
        # esto escribiendo directamente en misiones_agente.json, aquí se ve.
        una = mision()
        escribir_misiones(data_dir, una)
        antes = ruta_misiones_agente(data_dir).stat().st_mtime_ns

        identificado.get(reverse("tareas"))
        identificado.post(reverse("mision_cancelar"), {"id_mision": una.id})

        assert ruta_misiones_agente(data_dir).stat().st_mtime_ns == antes


class TestElEspacioDeNombres:
    def test_una_tarea_no_puede_llamarse_como_una_mision(
        self, identificado: Client, data_dir: Path
    ) -> None:
        # Comparten la carpeta de resultados. El agente evita chocar con las
        # tareas; esto cierra el trato por el lado del panel.
        respuesta = identificado.post(
            reverse("tarea_nueva"),
            {
                "nombre": "agenda-revision",
                "titulo": "Intento de invasión",
                "tipo": "sala",
                "cron": "0 8 * * *",
                "mision": "Lo que sea.",
                "contacto_nombre": "",
                "contacto_numero": "",
            },
        )
        assert respuesta.status_code == 200  # re-renderiza con el error
        assert b"reservado" in respuesta.content
