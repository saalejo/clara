"""El reparto del índice en una colección por tema.

Lo que más importa aquí es que **todo tema que el panel deje crear produzca un
nombre que ChromaDB acepte**. Si no, el fallo aparecería mucho después y en otro
sitio: al indexar, dentro del contenedor de la ingesta, con el tema ya creado y
el documento ya subido.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from voice_agent.rag.store import (
    NombreDeColeccionInvalido,
    borrar_coleccion,
    nombre_coleccion,
    tema_de_coleccion,
    temas_indexados,
)
from voice_agent_core import corpus
from voice_agent_core.config import Settings
from voice_agent_core.corpus import TEMA_RAIZ


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path)  # type: ignore[call-arg]


class ColeccionFalsa:
    """Lo justo para que `list_collections` devuelva algo con `.name`."""

    def __init__(self, name: str) -> None:
        self.name = name


class ClienteFalso:
    """Doble del cliente de ChromaDB, sin tocar disco ni cargar modelos."""

    def __init__(self, nombres: list[str]) -> None:
        self._nombres = list(nombres)
        self.borradas: list[str] = []

    def list_collections(self) -> list[Any]:
        return [ColeccionFalsa(n) for n in self._nombres]

    def delete_collection(self, name: str) -> None:
        self._nombres.remove(name)
        self.borradas.append(name)


# --- Nombres -----------------------------------------------------------------


def test_la_raiz_conserva_el_nombre_de_siempre(settings: Settings) -> None:
    # El índice que ya existía se llamaba así, y sus documentos estaban en la
    # raíz del corpus: encaja sin migración.
    assert nombre_coleccion(settings, TEMA_RAIZ) == "conocimiento"


def test_cada_tema_lleva_el_prefijo(settings: Settings) -> None:
    assert nombre_coleccion(settings, "la-placa") == "conocimiento__la-placa"


@pytest.mark.parametrize("tema", [TEMA_RAIZ, "la-placa", "a", "tema_1", "el.agente"])
def test_nombre_y_tema_son_inversos(settings: Settings, tema: str) -> None:
    assert tema_de_coleccion(settings, nombre_coleccion(settings, tema)) == tema


def test_una_coleccion_ajena_no_es_nuestra(settings: Settings) -> None:
    # El mismo chroma_dir podría albergar otras colecciones; no son cosa nuestra.
    assert tema_de_coleccion(settings, "otra_cosa") is None
    assert tema_de_coleccion(settings, "conocimientoX") is None


@pytest.mark.parametrize(
    "propuesto",
    [
        "Guía de la Placa",
        "Ñoño",
        "a",
        "x" * 200,
        "1. Primero",
        "MAYÚSCULAS Y ESPACIOS",
        "con---guiones",
    ],
)
def test_todo_tema_valido_da_un_nombre_que_chroma_acepta(
    settings: Settings, propuesto: str
) -> None:
    """El contrato entre `corpus.py` y ChromaDB.

    `MAX_LONGITUD_TEMA` está puesto en 40 justo para que esto se cumpla con el
    prefijo por defecto. Si alguien lo sube, este test lo caza aquí y no en la
    placa a mitad de una reindexación.
    """
    tema = corpus.normalizar_tema(propuesto)
    nombre = nombre_coleccion(settings, tema)

    assert 3 <= len(nombre) <= 63
    assert nombre[0].isalnum() and nombre[-1].isalnum()
    assert all(c.isalnum() or c in "._-" for c in nombre)


def test_un_prefijo_imposible_falla_al_componer(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, chroma_collection="_malo")  # type: ignore[call-arg]
    with pytest.raises(NombreDeColeccionInvalido):
        nombre_coleccion(settings, TEMA_RAIZ)


# --- Inventario del índice ---------------------------------------------------


def test_solo_se_listan_las_colecciones_del_prefijo(settings: Settings) -> None:
    cliente = ClienteFalso(["conocimiento", "conocimiento__la-placa", "otra_cosa"])

    assert temas_indexados(settings, cliente) == [TEMA_RAIZ, "la-placa"]  # type: ignore[arg-type]


def test_borrar_todo_no_toca_lo_ajeno(settings: Settings) -> None:
    cliente = ClienteFalso(["conocimiento", "conocimiento__la-placa", "otra_cosa"])

    borrados = borrar_coleccion(settings, None, cliente)  # type: ignore[arg-type]

    assert sorted(borrados) == [TEMA_RAIZ, "la-placa"]
    assert cliente.borradas == ["conocimiento", "conocimiento__la-placa"]


def test_borrar_un_tema_solo_borra_el_suyo(settings: Settings) -> None:
    cliente = ClienteFalso(["conocimiento", "conocimiento__la-placa"])

    assert borrar_coleccion(settings, "la-placa", cliente) == ["la-placa"]  # type: ignore[arg-type]
    assert cliente.borradas == ["conocimiento__la-placa"]


def test_borrar_lo_que_no_esta_no_falla(settings: Settings) -> None:
    cliente = ClienteFalso(["conocimiento"])

    assert borrar_coleccion(settings, "inventado", cliente) == []  # type: ignore[arg-type]
    assert cliente.borradas == []
