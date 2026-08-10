"""Tests de la traza documental de las llamadas."""

from __future__ import annotations

import json
from pathlib import Path

from voice_agent.rag.retriever import Pasaje
from voice_agent.traza import TrazaLlamada


def _pasaje(origen: str) -> Pasaje:
    return Pasaje(texto="texto", origen=origen, distancia=0.4, tema="apendicitis")


def test_cada_consulta_es_una_linea_del_jsonl(tmp_path: Path) -> None:
    traza = TrazaLlamada(tmp_path, id_llamada="llamada-x")
    traza.registrar("herida mojada", [_pasaje("a.pdf"), _pasaje("b.pdf")])
    traza.registrar("fiebre", [])

    lineas = [json.loads(linea) for linea in traza.ruta.read_text().splitlines()]
    assert len(lineas) == 2
    assert lineas[0]["consulta"] == "herida mojada"
    assert [p["origen"] for p in lineas[0]["pasajes"]] == ["a.pdf", "b.pdf"]
    assert lineas[1]["pasajes"] == []


def test_los_documentos_consultados_no_se_repiten_y_conservan_orden(tmp_path: Path) -> None:
    traza = TrazaLlamada(tmp_path)
    traza.registrar("una", [_pasaje("a.pdf"), _pasaje("b.pdf")])
    traza.registrar("otra", [_pasaje("b.pdf"), _pasaje("c.pdf")])

    assert traza.documentos_consultados == ["a.pdf", "b.pdf", "c.pdf"]


def test_el_id_derivado_no_choca_como_ruta(tmp_path: Path) -> None:
    traza = TrazaLlamada(tmp_path)
    assert traza.id_llamada.startswith("llamada-")
    assert traza.ruta.parent == tmp_path / "evaluaciones" / "trazas"


def test_un_fallo_de_disco_no_lanza(tmp_path: Path) -> None:
    # Se apunta la traza a una ruta imposible: bajo un fichero normal.
    tope = tmp_path / "fichero"
    tope.write_text("x")
    traza = TrazaLlamada(tope / "imposible")
    traza.registrar("consulta", [_pasaje("a.pdf")])  # no debe lanzar
    assert traza.documentos_consultados == ["a.pdf"]
