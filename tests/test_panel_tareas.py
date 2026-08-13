"""La gestión de tareas programadas desde el panel.

Lo crítico es el contrato: lo que se guarda por el formulario tiene que salir
en un `tareas.json` que `cargar_tareas` —el lector del agente— acepte tal
cual. Y la agenda del puente puede no estar: el formulario degrada, no revienta.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from voice_agent_core.tareas import TipoTarea, cargar_tareas
from voice_agent_panel import agenda
from voice_agent_panel.models import TareaProgramada

pytestmark = pytest.mark.django_db


@pytest.fixture
def identificado(client: Client) -> Client:
    client.force_login(User.objects.create_user(username="ember", password="una-clave-larga"))
    return client


@pytest.fixture
def data_dir(settings: Any, tmp_path: Path) -> Path:
    settings.DATA_DIR = tmp_path
    return tmp_path


def _datos(**cambios: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "nombre": "pastillas-manana",
        "titulo": "Pastillas de la mañana",
        "tipo": "sala",
        "cron": "0 8 * * 1-5",
        "mision": "Recuérdale a Nora la pastilla de la tensión.",
        "contacto_nombre": "",
        "contacto_numero": "",
    }
    base.update(cambios)
    return base


def test_crear_exporta_un_json_que_el_agente_lee(identificado: Client, data_dir: Path) -> None:
    respuesta = identificado.post(reverse("tarea_nueva"), _datos())
    assert respuesta.status_code == 302

    tarea = TareaProgramada.objects.get()
    assert not tarea.habilitada  # nace apagada, como los hooks

    config = cargar_tareas(data_dir)  # el round-trip ES el contrato
    assert len(config.tareas) == 1
    assert config.tareas[0].id == "pastillas-manana"
    assert config.tareas[0].cron == "0 8 * * 1-5"
    assert not config.tareas[0].habilitada


def test_un_cron_invalido_no_guarda(identificado: Client, data_dir: Path) -> None:
    respuesta = identificado.post(reverse("tarea_nueva"), _datos(cron="cada mañana"))
    assert respuesta.status_code == 200  # re-renderiza con el error
    assert not TareaProgramada.objects.exists()
    assert b"5 campos" in respuesta.content


def test_un_cron_imposible_no_guarda(identificado: Client, data_dir: Path) -> None:
    respuesta = identificado.post(reverse("tarea_nueva"), _datos(cron="0 0 31 2 *"))
    assert respuesta.status_code == 200
    assert not TareaProgramada.objects.exists()


def test_una_llamada_sin_numero_no_guarda(identificado: Client, data_dir: Path) -> None:
    respuesta = identificado.post(reverse("tarea_nueva"), _datos(tipo="llamada"))
    assert respuesta.status_code == 200
    assert not TareaProgramada.objects.exists()


def test_una_llamada_con_numero_congelado_guarda(identificado: Client, data_dir: Path) -> None:
    identificado.post(
        reverse("tarea_nueva"),
        _datos(
            nombre="revision-abuela",
            tipo="llamada",
            contacto_nombre="Abuela",
            contacto_numero="+573001234567",
        ),
    )
    config = cargar_tareas(data_dir)
    assert config.tareas[0].tipo is TipoTarea.LLAMADA
    assert config.tareas[0].contacto_numero == "+573001234567"


@pytest.fixture
def corpus_dir(settings: Any, tmp_path: Path) -> Path:
    """Un corpus con dos temas, para las sugerencias del procedimiento."""
    corpus = tmp_path / "corpus"
    (corpus / "colecistitis").mkdir(parents=True)
    (corpus / "apendicitis").mkdir()
    settings.CORPUS_DIR = corpus
    return corpus


class TestElProcedimiento:
    """El dato que arma la puerta de cobertura antes de que suene el teléfono.

    Sin él, el agente solo sabe de qué se operó el paciente si se lo dice
    hablando, y eso lo puede confundir un reconocedor de voz o torcerlo el
    propio modelo. Escrito en la tarea, la decisión está tomada de antemano.
    """

    def test_cruza_la_frontera_hasta_el_json_del_agente(
        self, identificado: Client, data_dir: Path, corpus_dir: Path
    ) -> None:
        identificado.post(
            reverse("tarea_nueva"),
            _datos(
                nombre="revision-nora",
                tipo="llamada",
                contacto_nombre="Nora",
                contacto_numero="+573001234567",
                procedimiento="colecistitis",
            ),
        )

        config = cargar_tareas(data_dir)
        assert config.tareas[0].procedimiento == "colecistitis"

    def test_una_cirugia_que_el_corpus_no_cubre_se_guarda_igual(
        self, identificado: Client, data_dir: Path, corpus_dir: Path
    ) -> None:
        """Son justo las llamadas para las que existe la puerta.

        Si el formulario rechazara lo que no es un tema, no se podría
        programar la llamada de un paciente de cataratas — que es precisamente
        el caso en el que hace falta que el agente diga que no puede ayudar.
        """
        identificado.post(
            reverse("tarea_nueva"),
            _datos(
                nombre="revision-ojo",
                tipo="llamada",
                contacto_numero="+573001234567",
                procedimiento="me operaron de cataratas",
            ),
        )

        config = cargar_tareas(data_dir)
        assert config.tareas[0].procedimiento == "me operaron de cataratas"

    def test_un_tema_escrito_con_mayusculas_casa_con_su_carpeta(
        self, identificado: Client, data_dir: Path, corpus_dir: Path
    ) -> None:
        """«Colecistitis» tiene que acabar siendo `colecistitis`, o no casaría."""
        identificado.post(
            reverse("tarea_nueva"),
            _datos(
                nombre="revision-vesicula",
                tipo="llamada",
                contacto_numero="+573001234567",
                procedimiento="Colecistitis",
            ),
        )

        config = cargar_tareas(data_dir)
        assert config.tareas[0].procedimiento == "colecistitis"

    def test_el_formulario_sugiere_los_temas_indexados(
        self, identificado: Client, data_dir: Path, corpus_dir: Path
    ) -> None:
        """Un `<datalist>` nativo, sin JavaScript: el panel no carga ninguno."""
        respuesta = identificado.get(reverse("tarea_nueva"))

        assert b'<datalist id="temas-corpus">' in respuesta.content
        assert b'<option value="apendicitis">' in respuesta.content
        assert b'<option value="colecistitis">' in respuesta.content

    def test_sin_corpus_el_formulario_sigue_funcionando(
        self, identificado: Client, data_dir: Path, settings: Any, tmp_path: Path
    ) -> None:
        """Las sugerencias son un lujo; que falte la carpeta no puede tumbarlo."""
        settings.CORPUS_DIR = tmp_path / "no-existe"

        assert identificado.get(reverse("tarea_nueva")).status_code == 200


def test_conmutar_habilitada_reexporta(identificado: Client, data_dir: Path) -> None:
    identificado.post(reverse("tarea_nueva"), _datos())
    tarea = TareaProgramada.objects.get()

    identificado.post(reverse("tareas"), {"habilitada": [str(tarea.pk)]})
    assert cargar_tareas(data_dir).tareas[0].habilitada

    identificado.post(reverse("tareas"), {})
    assert not cargar_tareas(data_dir).tareas[0].habilitada


def test_borrar_reexporta(identificado: Client, data_dir: Path) -> None:
    identificado.post(reverse("tarea_nueva"), _datos())
    tarea = TareaProgramada.objects.get()

    identificado.post(reverse("tarea_borrar", args=[tarea.pk]))
    assert not TareaProgramada.objects.exists()
    assert cargar_tareas(data_dir).tareas == []


def test_la_vista_previa_ensena_proximas_ejecuciones(identificado: Client, data_dir: Path) -> None:
    # Un POST de búsqueda de contacto re-renderiza el formulario; si el cron es
    # válido, la validación deja las próximas ejecuciones a mano de la plantilla.
    respuesta = identificado.post(reverse("tarea_nueva"), {**_datos(), "accion": "buscar_contacto"})
    assert respuesta.status_code == 200
    assert b"Pr\xc3\xb3ximas ejecuciones" in respuesta.content


def test_la_agenda_caida_no_rompe_el_formulario(
    identificado: Client, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `buscar_contactos` ya degrada a lista vacía por dentro; aquí se fija esa
    # degradación desde fuera: la vista responde 200 con el aviso.
    monkeypatch.setattr(agenda, "buscar_contactos", lambda *a, **k: [])
    respuesta = identificado.post(
        reverse("tarea_nueva"),
        {**_datos(contacto_nombre="Luis"), "accion": "buscar_contacto"},
    )
    assert respuesta.status_code == 200
    assert not TareaProgramada.objects.exists()


def test_el_buscador_ensena_candidatos(
    identificado: Client, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidatos = [{"nombre": "Luis Pérez", "numero": "+573001112233", "puntuacion": 95}]
    monkeypatch.setattr(agenda, "buscar_contactos", lambda *a, **k: candidatos)
    respuesta = identificado.post(
        reverse("tarea_nueva"),
        {**_datos(contacto_nombre="Luis"), "accion": "buscar_contacto"},
    )
    assert b"+573001112233" in respuesta.content


def test_resultados_lee_los_ficheros_del_agente(identificado: Client, data_dir: Path) -> None:
    identificado.post(reverse("tarea_nueva"), _datos())
    tarea = TareaProgramada.objects.get()

    from voice_agent_core.rutas import dir_resultados_tareas, ruta_bitacora_tareas

    carpeta = dir_resultados_tareas(data_dir) / "pastillas-manana"
    carpeta.mkdir(parents=True)
    (carpeta / "20260805-080000.json").write_text(
        '{"id_tarea": "pastillas-manana", "momento": "2026-08-05T08:00:00",'
        ' "resumen": "Todo bien", "respuestas": "Durmió bien."}',
        encoding="utf-8",
    )
    ruta_bitacora_tareas(data_dir).write_text(
        '{"id_tarea": "pastillas-manana", "programada": "2026-08-05T08:00:00",'
        ' "ejecutada": "2026-08-05T08:00:05", "resultado": "hablado", "detalle": ""}\n'
        '{"id_tarea": "otra", "programada": "x", "ejecutada": "y", "resultado": "error", "detalle": ""}\n'
        "esta línea está corrupta y se ignora\n",
        encoding="utf-8",
    )

    respuesta = identificado.get(reverse("tarea_resultados", args=[tarea.pk]))
    assert respuesta.status_code == 200
    assert "Durmió bien." in respuesta.content.decode()
    assert b"hablado" in respuesta.content
    assert b'"otra"' not in respuesta.content  # la bitácora se filtra por tarea
