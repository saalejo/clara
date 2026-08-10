"""El modo llamada: qué significa cada botón según lo que diga el puente.

El demonio no pregunta si hay una llamada: se suscribe al canal de eventos del
puente y va cambiando de modo. Aquí se prueba con un cliente falso, sin socket ni
móvil.

Lo que más importa fijar es la degradación: **que el puente no esté es el estado
normal la mitad del tiempo**, porque el modo «solo tarjeta de sonido» lo para a
propósito. Un modo de llamada que sobreviviera a la caída del puente haría que un
clic de MUTE intentara contestar a nadie en lugar de silenciar el micrófono.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from test_botones_acciones import PitidosFalsos
from test_botones_mezclador import MIC_ABIERTO, MIC_SILENCIADO, EjecutorFalso
from voice_agent_botones.acciones import Modo
from voice_agent_botones.config import Ajustes
from voice_agent_botones.demonio import Demonio
from voice_agent_botones.gestos import KEY_MUTE, KEY_VOLUMEUP, Gesto, Nivel
from voice_agent_botones.mezclador import Mezclador
from voice_agent_core.telefonia import EventoTelefonia, TipoEvento

CLIC_MUTE = Gesto(KEY_MUTE, Nivel.CORTO, 20)
VOL_MAS_LARGO = Gesto(KEY_VOLUMEUP, Nivel.LARGO, 900)
VOL_MAS_CORTO = Gesto(KEY_VOLUMEUP, Nivel.CORTO, 90)


class TelefoniaFalsa:
    """Cuenta lo que se le pide y entrega los eventos que el test decida."""

    def __init__(
        self,
        *,
        eventos_a_entregar: list[TipoEvento] | None = None,
        contestar_ok: bool = True,
        colgar_ok: bool = True,
        autocontestar: bool | None = False,
        revienta_canal: bool = False,
    ) -> None:
        self.eventos_a_entregar = eventos_a_entregar or []
        self.contestar_ok = contestar_ok
        self.colgar_ok = colgar_ok
        self.autocontestar = autocontestar
        self.revienta_canal = revienta_canal
        self.llamadas: list[str] = []
        self.conexiones = 0

    async def contestar(self) -> bool:
        self.llamadas.append("contestar")
        return self.contestar_ok

    async def colgar(self) -> bool:
        self.llamadas.append("colgar")
        return self.colgar_ok

    async def alternar_autocontestar(self) -> bool | None:
        self.llamadas.append("autocontestar")
        if self.autocontestar is None:
            return None
        self.autocontestar = not self.autocontestar
        return self.autocontestar

    async def eventos(
        self, *, al_conectar: Callable[[], None] | None = None
    ) -> AsyncIterator[EventoTelefonia]:
        if self.revienta_canal:
            raise ConnectionError("el puente no está")
        self.conexiones += 1
        if al_conectar is not None:
            al_conectar()
        for tipo in self.eventos_a_entregar:
            yield EventoTelefonia(tipo=tipo, momento=datetime.now(UTC))
        # Un canal real no termina; se queda esperando.
        await asyncio.sleep(3600)


def _modo(demonio: Demonio) -> Modo:
    """Lee el modo sin que mypy lo estreche.

    Un `assert demonio._modo is Modo.X` estrecha el tipo del atributo a ese literal
    y mypy da por imposible el assert siguiente, aunque el modo cambie de verdad
    entre medias. Pasando por una función que devuelve `Modo` no hay nada que
    estrechar.
    """
    return demonio._modo


def _demonio(
    tmp_path: Path,
    telefonia: TelefoniaFalsa,
    *,
    respuestas: list[str] | None = None,
    **kwargs: Any,
) -> tuple[Demonio, PitidosFalsos]:
    ajustes = Ajustes(_env_file=None, directorio_datos=tmp_path, **kwargs)  # type: ignore[call-arg]
    pitidos = PitidosFalsos(tmp_path / "pitidos")
    demonio = Demonio(
        ajustes,
        mezclador=Mezclador(ajustes, ejecutor=EjecutorFalso(respuestas or [])),
        pitidos=pitidos,
        telefonia=telefonia,  # type: ignore[arg-type]
    )
    return demonio, pitidos


# --- La máquina de modos ------------------------------------------------------


@pytest.mark.parametrize(
    ("tipo", "modo"),
    [
        (TipoEvento.LLAMADA_ENTRANTE, Modo.LLAMADA_ENTRANTE),
        (TipoEvento.LLAMADA_CONTESTADA, Modo.LLAMADA_EN_CURSO),
        (TipoEvento.LLAMADA_SALIENTE, Modo.LLAMADA_EN_CURSO),
        (TipoEvento.LLAMADA_TERMINADA, Modo.NORMAL),
        (TipoEvento.TELEFONO_DESCONECTADO, Modo.NORMAL),
    ],
)
def test_cada_evento_lleva_a_su_modo(tmp_path: Path, tipo: TipoEvento, modo: Modo) -> None:
    demonio, _ = _demonio(tmp_path, TelefoniaFalsa())
    demonio._al_llegar_evento(tipo)
    assert _modo(demonio) is modo


@pytest.mark.parametrize(
    "tipo",
    [
        TipoEvento.TELEFONO_CONECTADO,
        TipoEvento.AGENDA_ACTUALIZADA,
        TipoEvento.AUTOCONTESTAR_CAMBIADO,
    ],
)
def test_hay_eventos_que_no_cambian_el_modo(tmp_path: Path, tipo: TipoEvento) -> None:
    demonio, _ = _demonio(tmp_path, TelefoniaFalsa())
    demonio._al_llegar_evento(TipoEvento.LLAMADA_ENTRANTE)
    demonio._al_llegar_evento(tipo)
    assert _modo(demonio) is Modo.LLAMADA_ENTRANTE


def test_el_ciclo_completo_de_una_llamada(tmp_path: Path) -> None:
    demonio, _ = _demonio(tmp_path, TelefoniaFalsa())
    assert _modo(demonio) is Modo.NORMAL

    demonio._al_llegar_evento(TipoEvento.LLAMADA_ENTRANTE)
    assert _modo(demonio) is Modo.LLAMADA_ENTRANTE

    demonio._al_llegar_evento(TipoEvento.LLAMADA_CONTESTADA)
    assert _modo(demonio) is Modo.LLAMADA_EN_CURSO

    demonio._al_llegar_evento(TipoEvento.LLAMADA_TERMINADA)
    assert _modo(demonio) is Modo.NORMAL


# --- Qué hace MUTE en cada modo ----------------------------------------------


async def test_con_el_telefono_sonando_mute_contesta(tmp_path: Path) -> None:
    telefonia = TelefoniaFalsa()
    demonio, pitidos = _demonio(tmp_path, telefonia)
    demonio._al_llegar_evento(TipoEvento.LLAMADA_ENTRANTE)

    await demonio.atender(CLIC_MUTE)

    assert telefonia.llamadas == ["contestar"]
    assert pitidos.sonados == ["si"]


async def test_en_llamada_mute_vuelve_a_ser_el_microfono(tmp_path: Path) -> None:
    """El gesto corto significa siempre lo mismo donde significa algo.

    Es lo único que hay que memorizar en una interfaz sin pantalla, y colgar con un
    toque sería peligroso cuando el modo lo decide un estado invisible.
    """
    telefonia = TelefoniaFalsa()
    demonio, pitidos = _demonio(tmp_path, telefonia, respuestas=[MIC_ABIERTO, MIC_SILENCIADO])
    demonio._al_llegar_evento(TipoEvento.LLAMADA_CONTESTADA)

    await demonio.atender(CLIC_MUTE)

    assert telefonia.llamadas == []
    assert pitidos.sonados == ["no"]


async def test_sin_llamada_mute_es_el_microfono(tmp_path: Path) -> None:
    telefonia = TelefoniaFalsa()
    demonio, pitidos = _demonio(tmp_path, telefonia, respuestas=[MIC_ABIERTO, MIC_SILENCIADO])

    await demonio.atender(CLIC_MUTE)

    assert telefonia.llamadas == []
    assert pitidos.sonados == ["no"]


# --- Cortar la llamada --------------------------------------------------------


async def test_vol_mas_mantenido_rechaza_una_entrante(tmp_path: Path) -> None:
    telefonia = TelefoniaFalsa()
    demonio, pitidos = _demonio(tmp_path, telefonia)
    demonio._al_llegar_evento(TipoEvento.LLAMADA_ENTRANTE)

    await demonio.atender(VOL_MAS_LARGO)

    assert telefonia.llamadas == ["colgar"]
    assert pitidos.sonados == ["si"]


async def test_vol_mas_mantenido_cuelga_una_en_curso(tmp_path: Path) -> None:
    """Rechazar y colgar mandan la misma orden: para oFono es lo mismo."""
    telefonia = TelefoniaFalsa()
    demonio, _pitidos = _demonio(tmp_path, telefonia)
    demonio._al_llegar_evento(TipoEvento.LLAMADA_CONTESTADA)

    await demonio.atender(VOL_MAS_LARGO)

    assert telefonia.llamadas == ["colgar"]


async def test_un_fallo_del_puente_pita_error(tmp_path: Path) -> None:
    telefonia = TelefoniaFalsa(contestar_ok=False)
    demonio, pitidos = _demonio(tmp_path, telefonia)
    demonio._al_llegar_evento(TipoEvento.LLAMADA_ENTRANTE)

    await demonio.atender(CLIC_MUTE)

    assert pitidos.sonados == ["error"]


# --- El autocontestar ---------------------------------------------------------


async def test_vol_mas_mantenido_alterna_el_autocontestar(tmp_path: Path) -> None:
    telefonia = TelefoniaFalsa()
    demonio, pitidos = _demonio(tmp_path, telefonia)

    await demonio.atender(VOL_MAS_LARGO)
    assert telefonia.llamadas == ["autocontestar"]
    assert pitidos.sonados == ["si"]

    await demonio.atender(VOL_MAS_LARGO)
    assert pitidos.sonados == ["si", "no"]


async def test_sin_puente_el_autocontestar_pita_error(tmp_path: Path) -> None:
    telefonia = TelefoniaFalsa(autocontestar=None)
    demonio, pitidos = _demonio(tmp_path, telefonia)

    await demonio.atender(VOL_MAS_LARGO)

    assert pitidos.sonados == ["error"]


# --- Degradación sin puente ---------------------------------------------------


async def test_el_modo_de_llamada_no_sobrevive_a_la_caida_del_puente(
    tmp_path: Path,
) -> None:
    """Si sobreviviera, un clic de MUTE intentaría contestar a nadie.

    Y el usuario habría perdido el botón de silenciar el micrófono sin que nada se
    lo dijera, que es el peor tipo de fallo en una interfaz sin pantalla.
    """
    telefonia = TelefoniaFalsa(revienta_canal=True)
    demonio, _ = _demonio(tmp_path, telefonia)
    demonio._al_llegar_evento(TipoEvento.LLAMADA_ENTRANTE)
    assert _modo(demonio) is Modo.LLAMADA_ENTRANTE

    escucha = asyncio.create_task(demonio._escuchar_telefonia())
    for _ in range(10):
        await asyncio.sleep(0)
    escucha.cancel()

    assert _modo(demonio) is Modo.NORMAL


async def test_la_escucha_reintenta_y_no_muere(tmp_path: Path) -> None:
    """Que el puente no esté es normal: el modo «solo tarjeta» lo para a propósito."""
    telefonia = TelefoniaFalsa(revienta_canal=True)
    demonio, _ = _demonio(tmp_path, telefonia)

    escucha = asyncio.create_task(demonio._escuchar_telefonia())
    await asyncio.sleep(0.05)

    assert not escucha.done()
    escucha.cancel()


async def test_los_eventos_del_canal_mueven_el_modo(tmp_path: Path) -> None:
    telefonia = TelefoniaFalsa(eventos_a_entregar=[TipoEvento.LLAMADA_ENTRANTE])
    demonio, _ = _demonio(tmp_path, telefonia)

    escucha = asyncio.create_task(demonio._escuchar_telefonia())
    for _ in range(20):
        await asyncio.sleep(0)
        if _modo(demonio) is Modo.LLAMADA_ENTRANTE:
            break
    escucha.cancel()

    assert _modo(demonio) is Modo.LLAMADA_ENTRANTE


# --- Lo que no cambia nunca ---------------------------------------------------


@pytest.mark.parametrize("modo", [Modo.NORMAL, Modo.LLAMADA_ENTRANTE, Modo.LLAMADA_EN_CURSO])
async def test_el_volumen_funciona_en_los_tres_modos(tmp_path: Path, modo: Modo) -> None:
    from test_botones_mezclador import ALTAVOZ_AL_64, ALTAVOZ_AL_70

    telefonia = TelefoniaFalsa()
    demonio, pitidos = _demonio(tmp_path, telefonia, respuestas=[ALTAVOZ_AL_64, ALTAVOZ_AL_70])
    demonio._modo = modo

    await demonio.atender(VOL_MAS_CORTO)

    assert pitidos.sonados == ["si"]
    assert telefonia.llamadas == []


async def test_avisa_de_la_conexion_aunque_el_canal_este_callado(tmp_path: Path) -> None:
    """Se descubrió probándolo: el «canal conectado» salía 41 s tarde.

    Se registraba al llegar el primer evento, no al abrirse el flujo. Con esa
    lógica un canal sano y callado —lo normal, si no hay llamadas— es
    indistinguible de uno caído, y el retroceso exponencial no se reinicia hasta
    que pase algo.
    """
    telefonia = TelefoniaFalsa()  # sin eventos que entregar
    demonio, _ = _demonio(tmp_path, telefonia)

    escucha = asyncio.create_task(demonio._escuchar_telefonia())
    await asyncio.sleep(0.02)
    escucha.cancel()

    assert telefonia.conexiones == 1
