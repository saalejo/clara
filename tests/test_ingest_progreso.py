"""La ingesta cuenta por dónde va, y el panel lo pinta.

Reindexar el corpus clínico puede irse a los minutos, y hasta ahora el panel solo
sabía decir "reindexando ahora". Aquí se comprueban las dos mitades del canal: que
`ingerir` deja el fichero de progreso con cifras que cuadran —incluida la de
documentos que no ha tocado— y que la barra que sale de ellas no miente.

Con los mismos dobles de ChromaDB que `test_ingest_reconciliacion`: lo que se
prueba es la contabilidad, no la calidad de los vectores.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from voice_agent.rag import ingest
from voice_agent_core import corpus
from voice_agent_core.config import Settings
from voice_agent_core.corpus import TEMA_RAIZ
from voice_agent_core.ingesta import FaseIngesta, ProgresoIngesta, leer_progreso
from voice_agent_core.rutas import ruta_progreso_ingesta


class ColeccionFalsa:
    """Lo mínimo de una colección para que la ingesta funcione."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.ids: set[str] = set()
        self.metadatos: dict[str, dict[str, Any]] = {}

    def upsert(self, *, ids: list[str], documents: list[str], metadatas: list[Any]) -> None:
        self.ids.update(ids)
        for identificador, meta in zip(ids, metadatas, strict=True):
            self.metadatos[identificador] = meta

    def update(self, *, ids: list[str], metadatas: list[Any]) -> None:
        for identificador, meta in zip(ids, metadatas, strict=True):
            self.metadatos[identificador] = meta

    def get(self, *, include: Any = None) -> dict[str, Any]:
        ids = sorted(self.ids)
        datos: dict[str, Any] = {"ids": ids}
        if include and "metadatas" in include:
            datos["metadatas"] = [self.metadatos.get(i, {}) for i in ids]
        return datos

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
    falso = ClienteFalso()
    monkeypatch.setattr(ingest, "abrir_cliente", lambda *a, **k: falso)
    monkeypatch.setattr("voice_agent.rag.store.abrir_cliente", lambda *a, **k: falso)
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


def _documento(settings: Settings, nombre: str, texto: str) -> None:
    corpus.guardar_documento(settings.corpus_dir, TEMA_RAIZ, nombre, [texto.encode()])


def _progreso(**campos: Any) -> ProgresoIngesta:
    ahora = datetime(2026, 8, 12, 9, 0, 0)
    campos.setdefault("iniciada_en", ahora)
    campos.setdefault("actualizada_en", ahora)
    return ProgresoIngesta(**campos)


# --- Lo que la ingesta publica -----------------------------------------------


def test_al_terminar_queda_el_resumen_de_la_pasada(
    settings: Settings, cliente: ClienteFalso
) -> None:
    _documento(settings, "cpu.md", "# CPU\n\nSeis núcleos.")
    _documento(settings, "ram.md", "# RAM\n\nCuatro gigas.")

    ingest.ingerir(settings)

    progreso = leer_progreso(settings.data_dir)
    assert progreso is not None
    assert progreso.fase is FaseIngesta.TERMINADO
    assert progreso.porcentaje == 100
    assert progreso.documentos_total == 2
    assert progreso.documentos_pendientes == 2
    assert progreso.documentos_hechos == 2
    assert progreso.documentos_sin_cambios == 0
    assert progreso.fragmentos_nuevos == 2
    assert progreso.fragmentos_total == 2


def test_la_segunda_pasada_lo_cuenta_como_sin_cambios(
    settings: Settings, cliente: ClienteFalso
) -> None:
    """La cifra que explica por qué reindexar sin cambios tarda un suspiro."""
    _documento(settings, "cpu.md", "# CPU\n\nSeis núcleos.")
    ingest.ingerir(settings)

    ingest.ingerir(settings)

    progreso = leer_progreso(settings.data_dir)
    assert progreso is not None
    assert progreso.documentos_sin_cambios == 1
    assert progreso.documentos_pendientes == 0
    assert progreso.fragmentos_nuevos == 0
    assert progreso.fragmentos_total == 1
    assert progreso.porcentaje == 100


def test_lo_que_se_olvida_tambien_se_cuenta(settings: Settings, cliente: ClienteFalso) -> None:
    _documento(settings, "cpu.md", "# CPU\n\nSeis núcleos.")
    _documento(settings, "ram.md", "# RAM\n\nCuatro gigas.")
    ingest.ingerir(settings)

    corpus.borrar_documento(settings.corpus_dir, TEMA_RAIZ, "ram.md")
    ingest.ingerir(settings)

    progreso = leer_progreso(settings.data_dir)
    assert progreso is not None
    assert progreso.fragmentos_olvidados == 1
    assert progreso.fragmentos_total == 1


def test_si_revienta_lo_dice_en_vez_de_dejar_la_barra_a_medias(
    settings: Settings, cliente: ClienteFalso
) -> None:
    """Sin esto, el panel se queda enseñando un 40 % para siempre."""
    settings = settings.model_copy(update={"corpus_dir": settings.corpus_dir / "no-esta"})

    with pytest.raises(FileNotFoundError):
        ingest.ingerir(settings)

    progreso = leer_progreso(settings.data_dir)
    assert progreso is not None
    assert progreso.fase is FaseIngesta.ERROR
    assert "No existe la carpeta del corpus" in progreso.error


def test_el_progreso_ilegible_no_tumba_a_quien_lo_lee(settings: Settings) -> None:
    ruta = ruta_progreso_ingesta(settings.data_dir)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text("{esto no es json", encoding="utf-8")

    assert leer_progreso(settings.data_dir) is None


# --- Lo que sale de esas cifras ----------------------------------------------


def test_la_barra_reparte_explorar_indexar_y_limpiar() -> None:
    explorando = _progreso(fase=FaseIngesta.EXPLORANDO, temas_total=4, temas_hechos=2)
    assert explorando.porcentaje == 5

    a_medias = _progreso(fase=FaseIngesta.INDEXANDO, documentos_pendientes=10, documentos_hechos=5)
    assert a_medias.porcentaje == 52

    limpiando = _progreso(fase=FaseIngesta.LIMPIANDO)
    assert limpiando.porcentaje == 95


def test_sin_nada_pendiente_la_barra_no_se_queda_clavada_en_el_diez() -> None:
    """Reindexar sin cambios pasa por indexar sin un solo documento que indexar."""
    progreso = _progreso(fase=FaseIngesta.INDEXANDO, documentos_sin_cambios=106)

    assert progreso.porcentaje == 95


def test_una_ingesta_fallida_tambien_esta_terminada() -> None:
    progreso = _progreso(fase=FaseIngesta.ERROR, error="sin sitio en disco")

    assert progreso.terminada
    assert progreso.porcentaje == 100


def test_la_duracion_sale_de_las_dos_marcas() -> None:
    inicio = datetime(2026, 8, 12, 9, 0, 0)
    progreso = _progreso(iniciada_en=inicio, actualizada_en=inicio + timedelta(seconds=90))

    assert progreso.duracion_s == 90.0
