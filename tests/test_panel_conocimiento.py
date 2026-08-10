"""La página de Conocimiento: subir, borrar y no escribir donde no se debe.

Es la única parte del panel que escribe ficheros en una ruta que viene de la
petición, así que la mitad de estos tests comprueban que un nombre hostil no
consigue tocar nada fuera de `corpus/`. La otra mitad, que lo que se ve en la
página y el aviso de "falta reindexar" dicen la verdad.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse

from voice_agent_core import corpus
from voice_agent_core.corpus import TEMA_RAIZ
from voice_agent_panel import control, views
from voice_agent_panel.models import Reindexado

pytestmark = pytest.mark.django_db


@pytest.fixture
def usuario() -> User:
    return User.objects.create_user(username="ember", password="una-clave-larga")


@pytest.fixture
def identificado(client: Client, usuario: User) -> Client:
    client.force_login(usuario)
    return client


@pytest.fixture
def corpus_dir(tmp_path: Path) -> Path:
    raiz = tmp_path / "corpus"
    raiz.mkdir()
    return raiz


@pytest.fixture(autouse=True)
def _corpus_aislado(corpus_dir: Path) -> Any:
    """Ningún test toca el corpus de verdad de la placa."""
    with override_settings(CORPUS_DIR=corpus_dir):
        yield


@pytest.fixture(autouse=True)
def _sin_dbus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Los tests no hablan con systemd."""

    def _estado(unidad: str | None = None) -> control.EstadoUnidad:
        return control.EstadoUnidad(
            unidad=unidad or "voice-agent.service",
            active_state="inactive",
            sub_state="dead",
            resultado="success",
        )

    monkeypatch.setattr(control, "estado", _estado)
    monkeypatch.setattr(control, "lanzar_ingesta", lambda: "/job/1")


def _subir(cliente: Client, tema: str, nombre: str, contenido: bytes = b"# T\n\nTexto.") -> Any:
    return cliente.post(
        reverse("conocimiento"),
        {"tema": tema, "archivo": SimpleUploadedFile(nombre, contenido)},
        follow=True,
    )


def _subir_crudo(cliente: Client, tema: str, nombre: str, contenido: bytes = b"x") -> Any:
    """Sube componiendo el multipart a mano, con el nombre de fichero literal.

    `SimpleUploadedFile` valida el nombre **al construirse**, así que con él no se
    pueden probar los nombres realmente hostiles: el test reventaría antes de
    llegar a la vista. Quien ataca escribe el cuerpo HTTP directamente, y esto es
    lo que de verdad llega al servidor.
    """
    frontera = "frontera-de-prueba"
    cuerpo = (
        (
            f"--{frontera}\r\n"
            f'Content-Disposition: form-data; name="tema"\r\n\r\n{tema}\r\n'
            f"--{frontera}\r\n"
            f'Content-Disposition: form-data; name="archivo"; filename="{nombre}"\r\n'
            f"Content-Type: text/markdown\r\n\r\n"
        ).encode()
        + contenido
        + f"\r\n--{frontera}--\r\n".encode()
    )
    return cliente.post(
        reverse("conocimiento"),
        data=cuerpo,
        content_type=f"multipart/form-data; boundary={frontera}",
        follow=True,
    )


def _arbol(raiz: Path) -> set[str]:
    return {str(r.relative_to(raiz)) for r in raiz.rglob("*")}


def _fuera_del_corpus(tmp_path: Path, corpus_dir: Path) -> set[str]:
    """Todo lo que hay bajo tmp_path que NO está dentro del corpus.

    Es lo que nunca puede cambiar: que un documento acabe dentro de `corpus/` es
    justo lo que se pretende; que acabe en su directorio padre, no.
    """
    return {
        str(r.relative_to(tmp_path))
        for r in tmp_path.rglob("*")
        if not r.is_relative_to(corpus_dir)
    }


# --- Temas -------------------------------------------------------------------


def test_crear_un_tema_lo_slugifica(identificado: Client, corpus_dir: Path) -> None:
    respuesta = identificado.post(
        reverse("tema_crear"), {"nombre": "Guía de la Placa"}, follow=True
    )

    assert (corpus_dir / "guia-de-la-placa").is_dir()
    # Y se dice con qué nombre ha quedado, que no es el que se escribió.
    assert "guia-de-la-placa" in respuesta.content.decode()


def test_crear_dos_veces_el_mismo_tema_avisa(identificado: Client, corpus_dir: Path) -> None:
    identificado.post(reverse("tema_crear"), {"nombre": "la-placa"})
    respuesta = identificado.post(reverse("tema_crear"), {"nombre": "la-placa"}, follow=True)

    assert "ya existe" in respuesta.content.decode()


def test_borrar_un_tema_vacio(identificado: Client, corpus_dir: Path) -> None:
    corpus.crear_tema(corpus_dir, "la-placa")

    identificado.post(reverse("tema_borrar"), {"tema": "la-placa"}, follow=True)

    assert not (corpus_dir / "la-placa").exists()


def test_no_se_borra_un_tema_con_documentos(identificado: Client, corpus_dir: Path) -> None:
    corpus.crear_tema(corpus_dir, "la-placa")
    corpus.guardar_documento(corpus_dir, "la-placa", "a.md", [b"x"])

    respuesta = identificado.post(reverse("tema_borrar"), {"tema": "la-placa"}, follow=True)

    assert "Bórralos primero" in respuesta.content.decode()
    assert (corpus_dir / "la-placa" / "a.md").is_file()


# --- Documentos --------------------------------------------------------------


def test_subir_un_documento_a_un_tema(identificado: Client, corpus_dir: Path) -> None:
    corpus.crear_tema(corpus_dir, "la-placa")

    _subir(identificado, "la-placa", "Manual de la Placa.md", b"# Manual\n\nSeis nucleos.")

    guardado = corpus_dir / "la-placa" / "manual-de-la-placa.md"
    assert guardado.read_bytes() == b"# Manual\n\nSeis nucleos."


def test_subir_a_la_raiz(identificado: Client, corpus_dir: Path) -> None:
    _subir(identificado, TEMA_RAIZ, "suelto.md")
    assert (corpus_dir / "suelto.md").is_file()


def test_una_extension_no_indexable_se_rechaza(identificado: Client, corpus_dir: Path) -> None:
    respuesta = _subir(identificado, TEMA_RAIZ, "virus.exe", b"MZ")

    assert _arbol(corpus_dir) == set()
    assert "no es un documento indexable" in respuesta.content.decode()


def test_no_se_sobrescribe_un_documento(identificado: Client, corpus_dir: Path) -> None:
    corpus.guardar_documento(corpus_dir, TEMA_RAIZ, "a.md", [b"original"])

    respuesta = _subir(identificado, TEMA_RAIZ, "a.md", b"impostor")

    assert (corpus_dir / "a.md").read_bytes() == b"original"
    assert "Ya hay un documento" in respuesta.content.decode()


def test_borrar_un_documento(identificado: Client, corpus_dir: Path) -> None:
    corpus.guardar_documento(corpus_dir, TEMA_RAIZ, "a.md", [b"x"])

    identificado.post(
        reverse("documento_borrar"), {"tema": TEMA_RAIZ, "nombre": "a.md"}, follow=True
    )

    assert not (corpus_dir / "a.md").exists()


# --- Nada se escribe ni se borra fuera del corpus ----------------------------


@pytest.mark.parametrize(
    "tema", ["../../etc", "..", "/etc", "tema/sub", "a\\b", ".oculto", "x" * 300]
)
def test_un_tema_hostil_no_escribe_nada(
    identificado: Client, corpus_dir: Path, tmp_path: Path, tema: str
) -> None:
    antes = _arbol(tmp_path)

    _subir_crudo(identificado, tema, "a.md")

    # Ni fuera del corpus ni dentro: un tema que no existe no se crea al vuelo.
    assert _arbol(tmp_path) == antes


@pytest.mark.parametrize(
    "nombre",
    [
        "../../../etc/passwd",
        "..",
        ".",
        "/etc/passwd",
        "a/b.md",
        "..\\..\\windows.md",
        ".oculto.md",
        "virus.exe",
    ],
)
def test_un_nombre_hostil_al_subir_no_escribe_fuera(
    identificado: Client, corpus_dir: Path, tmp_path: Path, nombre: str
) -> None:
    """Pase lo que pase, nada acaba fuera de `corpus/`.

    Alguno de estos llega hasta la vista convertido en un nombre inofensivo
    —Django se queda con el `basename`— y entonces se guarda, legítimamente,
    dentro del corpus. Lo que se afirma aquí no es que no se escriba nada, sino
    que **no se escribe nada fuera**, que es la propiedad que importa.
    """
    antes = _fuera_del_corpus(tmp_path, corpus_dir)

    _subir_crudo(identificado, TEMA_RAIZ, nombre)

    assert _fuera_del_corpus(tmp_path, corpus_dir) == antes
    for escrito in corpus_dir.rglob("*"):
        assert escrito.resolve().is_relative_to(corpus_dir.resolve())


@pytest.mark.parametrize("nombre", ["../senuelo.md", "../../senuelo.md", "/etc/passwd"])
def test_un_nombre_hostil_al_borrar_no_borra_fuera(
    identificado: Client, corpus_dir: Path, tmp_path: Path, nombre: str
) -> None:
    senuelo = tmp_path / "senuelo.md"
    senuelo.write_text("no se toca")

    identificado.post(
        reverse("documento_borrar"), {"tema": TEMA_RAIZ, "nombre": nombre}, follow=True
    )

    assert senuelo.read_text() == "no se toca"


def test_un_tema_hostil_al_borrar_no_borra_fuera(
    identificado: Client, corpus_dir: Path, tmp_path: Path
) -> None:
    victima = tmp_path / "importante"
    victima.mkdir()

    identificado.post(reverse("tema_borrar"), {"tema": "../importante"}, follow=True)

    assert victima.is_dir()


# --- El aviso de índice viejo ------------------------------------------------


def test_sin_haber_reindexado_nunca_no_se_alarma(identificado: Client, corpus_dir: Path) -> None:
    """Lo normal si el índice se construyó con `make ingest`.

    Un aviso que sale siempre es un aviso que se aprende a ignorar.
    """
    corpus.guardar_documento(corpus_dir, TEMA_RAIZ, "a.md", [b"x"])

    contenido = identificado.get(reverse("panel")).content.decode()

    assert "no conoce lo que has subido" not in contenido


def test_tras_subir_algo_la_portada_avisa(identificado: Client, corpus_dir: Path) -> None:
    identificado.post(reverse("servicio", args=["ingesta"]))
    _subir(identificado, TEMA_RAIZ, "nuevo.md")

    contenido = identificado.get(reverse("panel")).content.decode()

    assert "no conoce lo que has subido" in contenido


def test_tras_reindexar_el_aviso_desaparece(identificado: Client, corpus_dir: Path) -> None:
    _subir(identificado, TEMA_RAIZ, "nuevo.md")
    identificado.post(reverse("servicio", args=["ingesta"]))

    contenido = identificado.get(reverse("panel")).content.decode()

    assert "no conoce lo que has subido" not in contenido


def test_reindexar_guarda_la_marca_del_corpus(identificado: Client, corpus_dir: Path) -> None:
    corpus.guardar_documento(corpus_dir, TEMA_RAIZ, "a.md", [b"x"])

    identificado.post(reverse("servicio", args=["ingesta"]))

    ultimo = Reindexado.objects.first()
    assert ultimo is not None
    assert ultimo.resultado == Reindexado.Resultado.LANZADO
    # La marca es la del corpus, no la hora del panel: es lo que hace que el
    # aviso no dependa de que ambos relojes compartan referencia.
    assert ultimo.marca_indexada == corpus.marca_de_cambio(corpus_dir)


def test_si_la_unidad_de_ingesta_fallo_el_indice_se_da_por_viejo(
    identificado: Client, corpus_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identificado.post(reverse("servicio", args=["ingesta"]))

    def _fallida(unidad: str | None = None) -> control.EstadoUnidad:
        activo = unidad == "voice-agent-ingest.service"
        return control.EstadoUnidad(
            unidad=unidad or "voice-agent.service",
            active_state="failed" if activo else "inactive",
            sub_state="failed" if activo else "dead",
            resultado="exit-code" if activo else "success",
        )

    monkeypatch.setattr(control, "estado", _fallida)

    contenido = identificado.get(reverse("panel")).content.decode()
    assert "La última reindexación de la base de conocimiento falló" in contenido


def test_no_se_puede_hablar_con_systemd_y_la_pagina_sigue_sirviendo(
    identificado: Client, corpus_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ver y editar el corpus no depende de que systemd conteste."""

    def _revienta(unidad: str | None = None) -> control.EstadoUnidad:
        raise control.ErrorDeControl("no hay bus")

    monkeypatch.setattr(control, "estado", _revienta)

    assert identificado.get(reverse("conocimiento")).status_code == 200


# --- La página ---------------------------------------------------------------


def test_la_pagina_lista_los_temas_y_sus_documentos(identificado: Client, corpus_dir: Path) -> None:
    corpus.crear_tema(corpus_dir, "la-placa")
    corpus.guardar_documento(corpus_dir, "la-placa", "cpu.md", [b"x"])
    corpus.guardar_documento(corpus_dir, TEMA_RAIZ, "suelto.md", [b"x"])

    contenido = identificado.get(reverse("conocimiento")).content.decode()

    assert "la-placa" in contenido
    assert "cpu.md" in contenido
    assert "suelto.md" in contenido
    assert "Sin tema" in contenido


def test_la_pagina_avisa_de_lo_que_queda_fuera_del_indice(
    identificado: Client, corpus_dir: Path
) -> None:
    hondo = corpus_dir / "tema" / "subtema"
    hondo.mkdir(parents=True)
    (hondo / "perdido.md").write_text("x")

    contenido = identificado.get(reverse("conocimiento")).content.decode()

    assert "Fuera del índice" in contenido
    assert "perdido.md" in contenido


def test_el_estado_del_indice_se_calcula_sin_reventar_con_el_corpus_vacio(
    identificado: Client,
) -> None:
    assert views._estado_del_indice().marca_actual >= 0.0
