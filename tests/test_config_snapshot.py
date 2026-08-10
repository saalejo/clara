"""La instantánea que escribe el panel y su sitio en el orden de prioridad.

Lo que se comprueba aquí es justo lo que decide si el panel sirve para algo: si
la instantánea no ganase al entorno, guardar un cambio no tendría efecto dentro
del contenedor, donde la unidad de systemd inyecta el `.env` como variables de
entorno reales.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from voice_agent_core.config import CAMPOS_PROTEGIDOS, Settings
from voice_agent_core.rutas import VAR_ENTORNO_SNAPSHOT


def _settings(**kwargs: Any) -> Settings:
    """Construye un Settings sin leer el .env del proyecto."""
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


def _con_instantanea(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contenido: object | str
) -> Path:
    """Escribe una instantánea y apunta la configuración hacia ella."""
    ruta = tmp_path / "settings.json"
    ruta.write_text(
        contenido if isinstance(contenido, str) else json.dumps(contenido),
        encoding="utf-8",
    )
    monkeypatch.setenv(VAR_ENTORNO_SNAPSHOT, str(ruta))
    return ruta


def test_la_instantanea_gana_al_entorno(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Es EL test de esta funcionalidad. El Quadlet mete el .env como entorno del
    # proceso; si el entorno ganara, el panel sería decorativo.
    monkeypatch.setenv("GEMINI_MODEL", "modelo/del-entorno")
    _con_instantanea(monkeypatch, tmp_path, {"gemini_model": "modelo/del-panel"})

    assert _settings().gemini_model == "modelo/del-panel"


def test_el_entorno_manda_donde_la_instantanea_calla(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GEMINI_MODEL", "modelo/del-entorno")
    _con_instantanea(monkeypatch, tmp_path, {"llm_temperature": 0.1})

    settings = _settings()
    assert settings.gemini_model == "modelo/del-entorno"
    assert settings.llm_temperature == 0.1


def test_los_kwargs_ganan_a_la_instantanea(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Si esto se rompiera, cualquier test que construya un Settings explícito
    # pasaría a depender de lo último que se hubiera guardado desde el panel.
    _con_instantanea(monkeypatch, tmp_path, {"gemini_model": "modelo/del-panel"})

    assert _settings(gemini_model="modelo/explicito").gemini_model == "modelo/explicito"


@pytest.mark.parametrize("campo", sorted(CAMPOS_PROTEGIDOS))
def test_los_campos_protegidos_se_ignoran(
    campo: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # data_dir y corpus_dir son rutas del contenedor; las claves son secretos que
    # el panel no monta. Que estén en el fichero no basta para que se apliquen.
    _con_instantanea(monkeypatch, tmp_path, {campo: "/valor/colado"})

    settings = _settings()
    assert str(getattr(settings, campo)) != "/valor/colado"


def test_una_clave_desconocida_no_rompe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _con_instantanea(monkeypatch, tmp_path, {"campo_que_no_existe": 42, "llm_max_tokens": 123})

    assert _settings().llm_max_tokens == 123


def test_un_json_roto_no_impide_arrancar(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Una placa muda porque alguien guardó algo raro desde el navegador sería el
    # peor fallo posible de todo el panel.
    _con_instantanea(monkeypatch, tmp_path, "{esto no es json,,,")

    assert _settings().gemini_model  # arranca con los valores de siempre


def test_sin_fichero_no_pasa_nada(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(VAR_ENTORNO_SNAPSHOT, str(tmp_path / "no" / "existe.json"))

    assert _settings().llm_temperature == 0.6


def test_la_instantanea_se_valida_como_cualquier_otra_fuente(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 5.0 se sale del rango declarado (0..2). Debe fallar igual que si viniera
    # del entorno: el panel valida antes de escribir, pero esta es la red.
    _con_instantanea(monkeypatch, tmp_path, {"llm_temperature": 5.0})

    with pytest.raises(ValueError):
        _settings()
