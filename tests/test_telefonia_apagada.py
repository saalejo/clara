"""Sin puente de telefonía, el agente tiene que ser el de siempre.

Esta es la prueba de que todo el cambio es **aditivo**. Si alguno de estos
tests se pone rojo, significa que instalar la telefonía ha modificado el
comportamiento de un agente que no la usa, y eso no es aceptable: la mayoría de
los arranques —y desde luego el primero tras un reinicio de la placa— ocurren
con el puente todavía levantándose.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from voice_agent.bot import _preparar_telefonia
from voice_agent.tools import HERRAMIENTAS, herramientas_activas, nombre_de
from voice_agent_core.config import ModoTelefonia, Settings

NOMBRES_DE_SIEMPRE = {
    "buscar_en_documentos",
    "registrar_alerta",
    "finalizar_llamada",
    "obtener_fecha_hora",
    "estado_del_sistema",
    "guardar_respuestas",
    "historial_paciente",
    "identificar_prospecto",
    "guardar_brief",
    "historial_prospecto",
}


def settings_con(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg,arg-type]


class TestElCatalogoNoCambia:
    def test_por_defecto_solo_estan_las_de_siempre(self) -> None:
        activas = herramientas_activas(set())
        assert {nombre_de(h) for h in activas} == NOMBRES_DE_SIEMPRE

    def test_el_registro_principal_sigue_teniendo_diez(self) -> None:
        assert len(HERRAMIENTAS) == 10

    def test_con_telefonia_hay_diecisiete(self) -> None:
        activas = herramientas_activas(set(), incluir_telefonia=True)
        assert len(activas) == 17
        assert {nombre_de(h) for h in activas} > NOMBRES_DE_SIEMPRE

    def test_el_panel_puede_apagar_una_de_telefono(self) -> None:
        """Las de teléfono se filtran igual que las demás."""
        activas = herramientas_activas({"llamar_a_numero"}, incluir_telefonia=True)
        nombres = {nombre_de(h) for h in activas}
        assert "llamar_a_numero" not in nombres
        assert "contestar_llamada" in nombres

    def test_la_lista_devuelta_sigue_siendo_una_copia(self) -> None:
        """Que nadie pueda mutar el registro por accidente."""
        activas = herramientas_activas(set(), incluir_telefonia=True)
        activas.clear()
        assert len(HERRAMIENTAS) == 10


class TestElSondeo:
    async def test_modo_off_ni_siquiera_sondea(self, tmp_path: Path) -> None:
        settings = settings_con(data_dir=tmp_path, telefonia_modo=ModoTelefonia.OFF)
        assert await _preparar_telefonia(settings) is None

    async def test_modo_auto_sin_socket_no_activa_nada(self, tmp_path: Path) -> None:
        """El caso normal: el puente no está instalado o está parado."""
        settings = settings_con(data_dir=tmp_path, telefonia_modo=ModoTelefonia.AUTO)
        assert await _preparar_telefonia(settings) is None

    async def test_modo_on_activa_aunque_no_haya_puente(self, tmp_path: Path) -> None:
        """Para poder arrancar el puente DESPUÉS del agente."""
        settings = settings_con(data_dir=tmp_path, telefonia_modo=ModoTelefonia.ON)
        assert await _preparar_telefonia(settings) is not None

    async def test_un_socket_que_no_existe_no_lanza(self, tmp_path: Path) -> None:
        """Un fallo aquí impediría arrancar el agente entero."""
        settings = settings_con(
            data_dir=tmp_path,
            telefonia_socket_path=tmp_path / "ni" / "existe" / "esto.sock",
        )
        assert await _preparar_telefonia(settings) is None


class TestLaConfiguracion:
    def test_el_modo_por_defecto_es_auto(self) -> None:
        assert settings_con().telefonia_modo is ModoTelefonia.AUTO

    def test_la_ruta_del_socket_se_deriva(self, tmp_path: Path) -> None:
        assert settings_con(data_dir=tmp_path).telefonia_socket == (
            tmp_path / "run" / "telefonia.sock"
        )

    def test_el_panel_no_puede_cambiar_la_ruta_del_socket(self) -> None:
        """Misma razón que `data_dir`: vale distinto dentro y fuera del
        contenedor."""
        from voice_agent_core.config import CAMPOS_PROTEGIDOS

        assert "telefonia_socket_path" in CAMPOS_PROTEGIDOS

    @pytest.mark.parametrize(
        "campo",
        [
            "telefonia_modo",
            "telefonia_timeout_secs",
            "telefonia_bluetooth_address",
            "telefonia_contactos_ttl_horas",
            "telefonia_max_candidatos",
            "telefonia_anuncio",
        ],
    )
    def test_todos_los_ajustes_nuevos_estan_documentados(self, campo: str) -> None:
        """El panel enseña estas descripciones tal cual."""
        assert Settings.model_fields[campo].description
