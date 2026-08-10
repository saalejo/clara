"""La elección de a qué móvil llama la placa cuando el enlace se pierde.

Lo que se fija aquí es el criterio de candidato —emparejado, de confianza y
con el perfil de pasarela HFP— y las dos salidas que no deben confundirse:
lista vacía (no hay a quién llamar) y `None` (ya hay uno conectado, no toques
nada). Confundirlas haría que el puente llamara a `Connect` sobre un enlace
vivo en cada ciclo de vigilancia.
"""

from __future__ import annotations

from typing import Any

from voice_agent_telefonia.reconexion import UUID_HFP_AG, elegir_candidatos

RUTA = "/org/bluez/hci0/dev_04_01_BB_5D_68_12"


def movil(**cambios: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "Address": "04:01:BB:5D:68:12",
        "Alias": "TECNO POVA",
        "Paired": True,
        "Trusted": True,
        "Connected": False,
        "UUIDs": [UUID_HFP_AG, "0000110a-0000-1000-8000-00805f9b34fb"],
    }
    base.update(cambios)
    return base


def objetos(dispositivo: dict[str, Any], ruta: str = RUTA) -> dict[str, dict[str, Any]]:
    return {
        "/org/bluez/hci0": {"org.bluez.Adapter1": {}},
        ruta: {"org.bluez.Device1": dispositivo},
    }


class TestElegirCandidatos:
    def test_movil_emparejado_y_desconectado_es_candidato(self) -> None:
        assert elegir_candidatos(objetos(movil())) == [(RUTA, "TECNO POVA")]

    def test_conectado_devuelve_none_para_no_tocar_el_enlace(self) -> None:
        assert elegir_candidatos(objetos(movil(Connected=True))) is None

    def test_sin_emparejar_o_sin_confianza_no_cuenta(self) -> None:
        assert elegir_candidatos(objetos(movil(Paired=False))) == []
        assert elegir_candidatos(objetos(movil(Trusted=False))) == []

    def test_unos_auriculares_no_son_un_telefono(self) -> None:
        # Anuncian el lado HF (111e), no la pasarela AG (111f).
        auriculares = movil(UUIDs=["0000111e-0000-1000-8000-00805f9b34fb"])
        assert elegir_candidatos(objetos(auriculares)) == []

    def test_direccion_preferida_filtra_sin_distinguir_mayusculas(self) -> None:
        assert elegir_candidatos(objetos(movil()), "04:01:bb:5d:68:12") == [(RUTA, "TECNO POVA")]
        assert elegir_candidatos(objetos(movil()), "AA:BB:CC:DD:EE:FF") == []

    def test_sin_alias_se_usa_la_direccion(self) -> None:
        sin_alias = movil(Alias="")
        assert elegir_candidatos(objetos(sin_alias)) == [(RUTA, "04:01:BB:5D:68:12")]

    def test_uuids_en_mayusculas_tambien_casan(self) -> None:
        assert elegir_candidatos(objetos(movil(UUIDs=[UUID_HFP_AG.upper()]))) == [
            (RUTA, "TECNO POVA")
        ]
