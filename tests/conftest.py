"""Configuración común de la batería de tests.

Desde que `Settings` lee también la instantánea que escribe el panel, los tests
dejarían de ser herméticos sin este fichero: en la placa existe de verdad un
`data/config/settings.json`, y cualquier test que construyera un `Settings`
acabaría leyéndolo, con resultados distintos según lo último que se hubiera
guardado desde el navegador.

El apaño es apuntar la instantánea a una ruta que no existe, para todos los
tests y sin que ninguno tenga que acordarse.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from voice_agent_core.rutas import VAR_ENTORNO_SNAPSHOT


@pytest.fixture(autouse=True)
def _sin_instantanea_del_panel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Aísla cada test de la configuración que el panel haya dejado en disco."""
    monkeypatch.setenv(VAR_ENTORNO_SNAPSHOT, str(tmp_path / "sin-panel" / "settings.json"))
