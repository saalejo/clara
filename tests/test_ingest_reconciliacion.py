"""La ingesta reconcilia el índice con el corpus, en vez de solo acumular.

Es la propiedad que hace útil el reparto en una colección por tema: hasta ahora,
olvidar un documento borrado exigía reconstruir el índice entero con `--reset`,
porque una ingesta incremental no puede recorrer lo que ya no existe. Con cada
tema en su colección sí se puede: se compara la colección con su carpeta.

Se usan dobles de ChromaDB. No es por comodidad: cargar el modelo de embeddings
de verdad tardaría minutos y bajaría de la red, y lo que se prueba aquí es la
lógica de qué se escribe y qué se borra, no la calidad de los vectores.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from voice_agent.rag import ingest
from voice_agent_core import corpus
from voice_agent_core.config import Settings
from voice_agent_core.corpus import TEMA_RAIZ


class ColeccionFalsa:
    """Doble de una colección: guarda ids y recuerda lo que le borran."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.ids: set[str] = set()
        self.metadatos: dict[str, dict[str, Any]] = {}

    def upsert(self, *, ids: list[str], documents: list[str], metadatas: list[Any]) -> None:
        self.ids.update(ids)
        for identificador, meta in zip(ids, metadatas, strict=True):
            self.metadatos[identificador] = meta

    def get(self, *, include: Any = None) -> dict[str, Any]:
        return {"ids": sorted(self.ids)}

    def delete(self, *, ids: list[str]) -> None:
        self.ids.difference_update(ids)


class ClienteFalso:
    """Doble del cliente persistente, con las colecciones en memoria."""

    def __init__(self) -> None:
        self.colecciones: dict[str, ColeccionFalsa] = {}

    def list_collections(self) -> list[ColeccionFalsa]:
        return list(self.colecciones.values())

    def get_or_create_collection(self, *, name: str, **_: Any) -> ColeccionFalsa:
        return self.colecciones.setdefault(name, ColeccionFalsa(name))

    def delete_collection(self, name: str) -> None:
        del self.colecciones[name]


@pytest.fixture
def cliente(monkeypatch: pytest.MonkeyPatch) -> ClienteFalso:
    """Sustituye el cliente de ChromaDB en todo el camino de la ingesta."""
    falso = ClienteFalso()
    monkeypatch.setattr(ingest, "abrir_cliente", lambda *a, **k: falso)
    monkeypatch.setattr(
        "voice_agent.rag.store.abrir_cliente",
        lambda *a, **k: falso,
    )
    # `abrir_coleccion` construye la función de embeddings de verdad, que
    # descargaría el modelo. Aquí solo hace falta la colección.
    monkeypatch.setattr(
        ingest,
        "abrir_coleccion",
        lambda settings, tema=TEMA_RAIZ, **k: falso.get_or_create_collection(
            name=_nombre(settings, tema)
        ),
    )
    return falso


def _nombre(settings: Settings, tema: str) -> str:
    from voice_agent.rag.store import nombre_coleccion

    return nombre_coleccion(settings, tema)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    (tmp_path / "corpus").mkdir()
    return Settings(  # type: ignore[call-arg]
        _env_file=None, data_dir=tmp_path / "data", corpus_dir=tmp_path / "corpus"
    )


def _documento(settings: Settings, tema: str, nombre: str, texto: str) -> None:
    if tema != TEMA_RAIZ and not (settings.corpus_dir / tema).is_dir():
        corpus.crear_tema(settings.corpus_dir, tema)
    corpus.guardar_documento(settings.corpus_dir, tema, nombre, [texto.encode()])


# --- Reparto por temas -------------------------------------------------------


def test_cada_tema_va_a_su_coleccion(settings: Settings, cliente: ClienteFalso) -> None:
    _documento(settings, TEMA_RAIZ, "suelto.md", "# Suelto\n\nTexto de la raíz.")
    _documento(settings, "la-placa", "cpu.md", "# CPU\n\nSeis núcleos.")

    ingest.ingerir(settings)

    assert set(cliente.colecciones) == {"conocimiento", "conocimiento__la-placa"}
    assert cliente.colecciones["conocimiento__la-placa"].ids


def test_el_tema_viaja_como_metadato(settings: Settings, cliente: ClienteFalso) -> None:
    _documento(settings, "la-placa", "cpu.md", "# CPU\n\nSeis núcleos.")

    ingest.ingerir(settings)

    metadatos = list(cliente.colecciones["conocimiento__la-placa"].metadatos.values())
    assert all(m["tema"] == "la-placa" for m in metadatos)
    assert all(m["origen"] == str(Path("la-placa") / "cpu.md") for m in metadatos)


def test_reindexar_sin_cambios_no_borra_nada(settings: Settings, cliente: ClienteFalso) -> None:
    _documento(settings, "la-placa", "cpu.md", "# CPU\n\nSeis núcleos.")
    ingest.ingerir(settings)
    antes = set(cliente.colecciones["conocimiento__la-placa"].ids)

    ingest.ingerir(settings)

    assert cliente.colecciones["conocimiento__la-placa"].ids == antes


# --- Lo que antes exigía --reset ---------------------------------------------


def test_borrar_un_documento_lo_olvida_sin_reset(settings: Settings, cliente: ClienteFalso) -> None:
    """El caso que motiva todo esto."""
    _documento(settings, "la-placa", "cpu.md", "# CPU\n\nSeis núcleos.")
    _documento(settings, "la-placa", "ram.md", "# RAM\n\nCuatro gigas.")
    ingest.ingerir(settings)
    assert len(cliente.colecciones["conocimiento__la-placa"].ids) == 2

    corpus.borrar_documento(settings.corpus_dir, "la-placa", "ram.md")
    ingest.ingerir(settings)

    quedan = cliente.colecciones["conocimiento__la-placa"].metadatos
    origenes = {
        m["origen"]
        for i, m in quedan.items()
        if i in cliente.colecciones["conocimiento__la-placa"].ids
    }
    assert origenes == {str(Path("la-placa") / "cpu.md")}


def test_editar_un_documento_no_deja_la_version_vieja(
    settings: Settings, cliente: ClienteFalso
) -> None:
    _documento(settings, "la-placa", "cpu.md", "# CPU\n\nSeis núcleos.")
    ingest.ingerir(settings)

    (settings.corpus_dir / "la-placa" / "cpu.md").write_text("# CPU\n\nDos A72 y cuatro A53.")
    ingest.ingerir(settings)

    # Un solo fragmento: el nuevo. El id incluye un resumen del contenido, así
    # que el de la versión anterior es distinto y habría quedado colgado.
    assert len(cliente.colecciones["conocimiento__la-placa"].ids) == 1


def test_borrar_un_tema_elimina_su_coleccion(settings: Settings, cliente: ClienteFalso) -> None:
    _documento(settings, "la-placa", "cpu.md", "# CPU\n\nSeis núcleos.")
    ingest.ingerir(settings)
    assert "conocimiento__la-placa" in cliente.colecciones

    corpus.borrar_documento(settings.corpus_dir, "la-placa", "cpu.md")
    corpus.borrar_tema(settings.corpus_dir, "la-placa")
    ingest.ingerir(settings)

    assert "conocimiento__la-placa" not in cliente.colecciones


def test_un_tema_vacio_no_gasta_una_coleccion(settings: Settings, cliente: ClienteFalso) -> None:
    # Cada colección carga su índice HNSW; una que nunca devolverá nada sobra.
    corpus.crear_tema(settings.corpus_dir, "vacio")

    ingest.ingerir(settings)

    assert "conocimiento__vacio" not in cliente.colecciones


# --- Casos límite ------------------------------------------------------------


def test_reset_borra_todo_antes_de_empezar(settings: Settings, cliente: ClienteFalso) -> None:
    _documento(settings, "la-placa", "cpu.md", "# CPU\n\nSeis núcleos.")
    ingest.ingerir(settings)
    cliente.colecciones["conocimiento__la-placa"].ids.add("basura-de-otra-epoca")

    ingest.ingerir(settings, reset=True)

    assert "basura-de-otra-epoca" not in cliente.colecciones["conocimiento__la-placa"].ids


def test_un_documento_vacio_se_omite(settings: Settings, cliente: ClienteFalso) -> None:
    _documento(settings, TEMA_RAIZ, "hueco.md", "   \n\n  ")
    _documento(settings, TEMA_RAIZ, "bueno.md", "# Bueno\n\nContenido de verdad.")

    assert ingest.ingerir(settings) > 0
    origenes = {m["origen"] for m in cliente.colecciones["conocimiento"].metadatos.values()}
    assert origenes == {"bueno.md"}


def test_sin_corpus_falla_con_un_mensaje_util(tmp_path: Path, cliente: ClienteFalso) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, data_dir=tmp_path, corpus_dir=tmp_path / "no-esta"
    )
    with pytest.raises(FileNotFoundError, match="No existe la carpeta del corpus"):
        ingest.ingerir(settings)


def test_un_documento_muy_hondo_se_avisa(
    settings: Settings, cliente: ClienteFalso, caplog: pytest.LogCaptureFixture
) -> None:
    hondo = settings.corpus_dir / "tema" / "subtema"
    hondo.mkdir(parents=True)
    (hondo / "perdido.md").write_text("# Perdido\n\nNadie me indexa.")

    ingest.ingerir(settings)

    # No se indexa, pero tampoco se calla: es la diferencia entre un límite y un
    # agujero negro.
    assert corpus.documentos_ignorados(settings.corpus_dir) == [hondo / "perdido.md"]
    assert "conocimiento__tema" not in cliente.colecciones
