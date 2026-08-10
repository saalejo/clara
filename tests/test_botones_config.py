"""Los ajustes del demonio de botones.

Lo que se fija aquí no es que pydantic funcione, sino las tres cosas que un error
de configuración dejaría roto **en silencio**: que el nivel 3 exista, que las
rutas derivadas apunten al sitio compartido con el resto del proyecto, y que la
lista blanca de unidades no crezca sola.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from voice_agent_botones.config import DISPOSITIVO_POR_DEFECTO, Ajustes, ModoMicrofono


def _ajustes(**kwargs: Any) -> Ajustes:
    """Construye ajustes sin dejar que el entorno de la máquina se cuele."""
    return Ajustes(_env_file=None, **kwargs)  # type: ignore[call-arg]


def test_los_valores_por_defecto_son_los_medidos_en_la_placa() -> None:
    a = _ajustes()
    assert a.dispositivo == DISPOSITIVO_POR_DEFECTO
    assert a.acaparar is True
    assert a.modo_micro is ModoMicrofono.SWITCH
    assert a.tarjeta_alsa == "Device"
    # 6% son 16 pulsaciones de fondo a tope, medido con `amixer -M`.
    assert a.paso_volumen == 6
    assert a.umbral_largo_ms < a.umbral_muy_largo_ms


def test_las_rutas_derivadas_cuelgan_del_directorio_de_datos(tmp_path: Path) -> None:
    a = _ajustes(directorio_datos=tmp_path)
    assert a.socket_telefonia == tmp_path / "run" / "telefonia.sock"
    assert a.dir_pitidos == tmp_path / "pitidos"


def test_unos_umbrales_invertidos_no_arrancan() -> None:
    # Si el nivel 3 fuera inalcanzable, parar y reiniciar el agente dejarían de
    # existir sin que nada lo dijera. Mejor no arrancar.
    with pytest.raises(ValidationError, match="nivel 3 es inalcanzable"):
        _ajustes(umbral_largo_ms=2000, umbral_muy_largo_ms=1000)


def test_umbrales_iguales_tampoco_arrancan() -> None:
    with pytest.raises(ValidationError, match="nivel 3 es inalcanzable"):
        _ajustes(umbral_largo_ms=700, umbral_muy_largo_ms=700)


def test_solo_se_gobiernan_el_agente_y_el_puente() -> None:
    a = _ajustes()
    assert a.unidades_gobernadas == frozenset(
        {"voice-agent.service", "voice-agent-telefonia.service"}
    )
    # En particular, ni el panel ni la ingesta: un botón no debería poder tirar
    # la interfaz desde la que se diagnostica el botón.
    assert "voice-agent-panel.service" not in a.unidades_gobernadas
    assert "voice-agent-ingest.service" not in a.unidades_gobernadas


def test_data_dir_llega_sin_prefijo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # DATA_DIR es del proyecto entero, no del demonio, así que NO lleva el
    # prefijo BOTONES_. Es la clase de detalle que se rompe al refactorizar.
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert _ajustes().directorio_datos == tmp_path


def test_el_argumento_explicito_le_gana_a_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Un campo con alias tiene que poder poblarse también por su nombre.

    Sin `populate_by_name` en el `model_config`, `Ajustes(directorio_datos=...)`
    se descarta en silencio —lo traga `extra="ignore"`— y gana la variable de
    entorno. Es un fallo mudo: el código parece configurar una ruta y en realidad
    usa otra. Aquí se fija con un entorno hostil, que es como se descubrió.
    """
    monkeypatch.setenv("DATA_DIR", "/no/deberia/ganar")
    assert _ajustes(directorio_datos=tmp_path).directorio_datos == tmp_path


def test_el_prefijo_de_los_ajustes_propios_es_botones(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOTONES_PASO_VOLUMEN", "10")
    monkeypatch.setenv("BOTONES_ACAPARAR", "0")
    a = _ajustes()
    assert a.paso_volumen == 10
    assert a.acaparar is False


def test_la_tilde_de_una_ruta_escrita_a_mano_se_expande() -> None:
    a = _ajustes(directorio_datos="~/datos")
    assert "~" not in str(a.directorio_datos)
    assert a.directorio_datos.is_absolute()
