"""El mapa de gestos y el despachador.

El test de la tabla es el que impide que una acción cambie de gesto sin que nadie
se dé cuenta: se recorre entera, parametrizada, para que un diff sea visible.

Y hay un test que merece explicación: `test_mute_solo_aparece_en_nivel_corto`.
Está medido que `KEY_MUTE` manda pulsar y soltar en el mismo microsegundo, así que
una entrada del mapa con MUTE en nivel 2 o 3 sería código muerto que nadie
descubriría hasta apretar el botón diez segundos sin resultado.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

# Las salidas grabadas de `amixer` viven en el test del mezclador, que es su
# sitio. `tests/` no es un paquete, así que pytest mete su directorio en sys.path
# y el import es plano.
from test_botones_mezclador import (
    ALTAVOZ_AL_64,
    ALTAVOZ_AL_70,
    ALTAVOZ_AL_100,
    MIC_ABIERTO,
    MIC_SILENCIADO,
    EjecutorFalso,
)
from voice_agent_botones.acciones import (
    DESTRUCTIVOS,
    MAPA,
    NECESITAN_PUENTE,
    Modo,
    Verbo,
    verbo_de,
)
from voice_agent_botones.config import Ajustes
from voice_agent_botones.demonio import VERBOS_IMPLEMENTADOS, Demonio
from voice_agent_botones.gestos import (
    KEY_MUTE,
    KEY_VOLUMEDOWN,
    KEY_VOLUMEUP,
    TECLAS_CON_NIVELES,
    Gesto,
    Nivel,
)
from voice_agent_botones.mezclador import Mezclador
from voice_agent_botones.pitidos import Pitidos

# --- El mapa, entero ----------------------------------------------------------

ESPERADO = [
    # Sin llamada.
    (Modo.NORMAL, KEY_MUTE, Nivel.CORTO, Verbo.MICRO),
    (Modo.NORMAL, KEY_VOLUMEUP, Nivel.CORTO, Verbo.VOLUMEN_MAS),
    (Modo.NORMAL, KEY_VOLUMEDOWN, Nivel.CORTO, Verbo.VOLUMEN_MENOS),
    (Modo.NORMAL, KEY_VOLUMEUP, Nivel.LARGO, Verbo.AUTOCONTESTAR),
    (Modo.NORMAL, KEY_VOLUMEDOWN, Nivel.LARGO, Verbo.SOLO_TARJETA),
    (Modo.NORMAL, KEY_VOLUMEUP, Nivel.MUY_LARGO, Verbo.AGENTE_ALTERNAR),
    (Modo.NORMAL, KEY_VOLUMEDOWN, Nivel.MUY_LARGO, Verbo.AGENTE_REINICIAR),
    # Teléfono sonando.
    (Modo.LLAMADA_ENTRANTE, KEY_MUTE, Nivel.CORTO, Verbo.CONTESTAR),
    (Modo.LLAMADA_ENTRANTE, KEY_VOLUMEUP, Nivel.CORTO, Verbo.VOLUMEN_MAS),
    (Modo.LLAMADA_ENTRANTE, KEY_VOLUMEDOWN, Nivel.CORTO, Verbo.VOLUMEN_MENOS),
    (Modo.LLAMADA_ENTRANTE, KEY_VOLUMEUP, Nivel.LARGO, Verbo.RECHAZAR),
    # Llamada en curso.
    (Modo.LLAMADA_EN_CURSO, KEY_MUTE, Nivel.CORTO, Verbo.MICRO),
    (Modo.LLAMADA_EN_CURSO, KEY_VOLUMEUP, Nivel.CORTO, Verbo.VOLUMEN_MAS),
    (Modo.LLAMADA_EN_CURSO, KEY_VOLUMEDOWN, Nivel.CORTO, Verbo.VOLUMEN_MENOS),
    (Modo.LLAMADA_EN_CURSO, KEY_VOLUMEUP, Nivel.LARGO, Verbo.COLGAR),
]


@pytest.mark.parametrize(("modo", "tecla", "nivel", "verbo"), ESPERADO)
def test_el_mapa_es_el_documentado(modo: Modo, tecla: int, nivel: Nivel, verbo: Verbo) -> None:
    assert verbo_de(modo, Gesto(tecla=tecla, nivel=nivel, duracion_ms=0)) is verbo


def test_el_mapa_no_tiene_entradas_de_mas() -> None:
    """Si alguien añade una acción, tiene que pasar por la lista de arriba."""
    assert set(MAPA) == {(m, t, n) for m, t, n, _ in ESPERADO}


def test_mute_solo_aparece_en_nivel_corto() -> None:
    """El hardware no distingue mantener pulsado en MUTE: sería código muerto."""
    niveles = {nivel for _, tecla, nivel in MAPA if tecla == KEY_MUTE}
    assert niveles == {Nivel.CORTO}


def test_el_volumen_es_volumen_en_los_tres_modos() -> None:
    """Nivel 1 del rocker no cambia de significado nunca: es lo memorizable."""
    for modo in Modo:
        assert verbo_de(modo, Gesto(KEY_VOLUMEUP, Nivel.CORTO, 0)) is Verbo.VOLUMEN_MAS
        assert verbo_de(modo, Gesto(KEY_VOLUMEDOWN, Nivel.CORTO, 0)) is Verbo.VOLUMEN_MENOS


def test_durante_una_llamada_no_se_tocan_los_servicios() -> None:
    """Parar el agente mientras hablas por teléfono no lo quiere nadie."""
    prohibidos = {Verbo.AGENTE_ALTERNAR, Verbo.AGENTE_REINICIAR, Verbo.SOLO_TARJETA}
    for modo in (Modo.LLAMADA_ENTRANTE, Modo.LLAMADA_EN_CURSO):
        for tecla in TECLAS_CON_NIVELES:
            for nivel in Nivel:
                assert verbo_de(modo, Gesto(tecla, nivel, 0)) not in prohibidos


def test_un_gesto_sin_accion_devuelve_nada() -> None:
    assert verbo_de(Modo.NORMAL, Gesto(KEY_MUTE, Nivel.MUY_LARGO, 0)) is Verbo.NADA
    assert verbo_de(Modo.LLAMADA_EN_CURSO, Gesto(KEY_VOLUMEDOWN, Nivel.MUY_LARGO, 0)) is Verbo.NADA


def test_los_conjuntos_de_verbos_son_coherentes() -> None:
    todos = set(Verbo)
    assert todos >= DESTRUCTIVOS
    assert todos >= NECESITAN_PUENTE
    assert todos >= VERBOS_IMPLEMENTADOS
    # Un verbo destructivo no puede necesitar el puente y viceversa: se ejecutan
    # por caminos distintos y mezclarlos indicaría un error de clasificación.
    assert not (DESTRUCTIVOS & NECESITAN_PUENTE)
    # Y `NADA` no es ninguna de las tres cosas.
    assert Verbo.NADA not in DESTRUCTIVOS | NECESITAN_PUENTE | VERBOS_IMPLEMENTADOS


def test_todo_verbo_del_mapa_esta_clasificado() -> None:
    """Un verbo nuevo tiene que declararse implementado, destructivo o de puente.

    Si no, caería en el «todavía no está implementado» y pitaría error para
    siempre sin que nada lo delatara.
    """
    clasificados = VERBOS_IMPLEMENTADOS | DESTRUCTIVOS | NECESITAN_PUENTE
    sin_clasificar = set(MAPA.values()) - clasificados - {Verbo.NADA}
    assert not sin_clasificar, f"Verbos del mapa sin clasificar: {sin_clasificar}"


# --- El despachador -----------------------------------------------------------


class PitidosFalsos(Pitidos):
    """Registra los pitidos en lugar de reproducirlos."""

    def __init__(self, directorio: Path) -> None:
        super().__init__(directorio, reproductor=self._nada)
        self.sonados: list[str] = []

    @staticmethod
    async def _nada(_ruta: Path) -> None:
        return None

    def sonar(self, nombre: str) -> None:
        self.sonados.append(nombre)


def _demonio(
    tmp_path: Path, respuestas: list[str], **kwargs: Any
) -> tuple[Demonio, PitidosFalsos, EjecutorFalso]:
    ajustes = Ajustes(_env_file=None, directorio_datos=tmp_path, **kwargs)  # type: ignore[call-arg]
    ejecutor = EjecutorFalso(respuestas)
    pitidos = PitidosFalsos(tmp_path / "pitidos")
    demonio = Demonio(ajustes, mezclador=Mezclador(ajustes, ejecutor=ejecutor), pitidos=pitidos)
    return demonio, pitidos, ejecutor


async def test_un_clic_de_mute_silencia_y_pita_grave(tmp_path: Path) -> None:
    demonio, pitidos, _ = _demonio(tmp_path, [MIC_ABIERTO, MIC_SILENCIADO])
    await demonio.atender(Gesto(KEY_MUTE, Nivel.CORTO, 12))
    assert pitidos.sonados == ["no"]


async def test_reabrir_el_microfono_pita_agudo(tmp_path: Path) -> None:
    demonio, pitidos, _ = _demonio(tmp_path, [MIC_SILENCIADO, MIC_ABIERTO])
    await demonio.atender(Gesto(KEY_MUTE, Nivel.CORTO, 12))
    assert pitidos.sonados == ["si"]


async def test_subir_el_volumen_pita_si(tmp_path: Path) -> None:
    demonio, pitidos, _ = _demonio(tmp_path, [ALTAVOZ_AL_64, ALTAVOZ_AL_70])
    await demonio.atender(Gesto(KEY_VOLUMEUP, Nivel.CORTO, 90))
    assert pitidos.sonados == ["si"]


async def test_el_volumen_al_limite_pita_tope(tmp_path: Path) -> None:
    demonio, pitidos, _ = _demonio(tmp_path, [ALTAVOZ_AL_100, ALTAVOZ_AL_100])
    await demonio.atender(Gesto(KEY_VOLUMEUP, Nivel.CORTO, 90))
    assert pitidos.sonados == ["tope"]


async def test_un_fallo_del_mezclador_pita_error(tmp_path: Path) -> None:
    demonio, pitidos, ejecutor = _demonio(tmp_path, [])
    ejecutor.codigo = 1
    await demonio.atender(Gesto(KEY_MUTE, Nivel.CORTO, 12))
    assert pitidos.sonados == ["error"]


async def test_un_gesto_sin_accion_pita_error(tmp_path: Path) -> None:
    """El silencio se confundiría con un botón que no funciona."""
    demonio, pitidos, _ = _demonio(tmp_path, [])
    await demonio.atender(Gesto(KEY_MUTE, Nivel.MUY_LARGO, 3000))
    assert pitidos.sonados == ["error"]


async def test_un_verbo_todavia_no_implementado_pita_error(tmp_path: Path) -> None:
    demonio, pitidos, _ = _demonio(tmp_path, [])
    await demonio.atender(Gesto(KEY_VOLUMEUP, Nivel.LARGO, 900))
    assert pitidos.sonados == ["error"]


async def test_cruzar_una_frontera_pita_el_nivel(tmp_path: Path) -> None:
    demonio, pitidos, _ = _demonio(tmp_path, [])
    demonio._al_cruzar_nivel(KEY_VOLUMEUP, Nivel.LARGO)
    demonio._al_cruzar_nivel(KEY_VOLUMEUP, Nivel.MUY_LARGO)
    assert pitidos.sonados == ["nivel2", "nivel3"]


async def test_el_micro_silenciado_se_reaplica_tras_reconectar(tmp_path: Path) -> None:
    """La tarjeta vuelve con los valores por defecto del driver al reenumerarse.

    Sin esto, un silencio puesto por el usuario se deshace solo y el micrófono se
    reabre sin que nadie lo haya pedido.
    """
    import asyncio

    demonio, _, ejecutor = _demonio(tmp_path, [MIC_ABIERTO, MIC_SILENCIADO])
    await demonio.atender(Gesto(KEY_MUTE, Nivel.CORTO, 12))
    ejecutor.respuestas = [MIC_SILENCIADO]

    demonio._al_reconectar()
    for _ in range(6):
        await asyncio.sleep(0)

    assert ejecutor.ordenes[-1] == ["amixer", "-c", "Device", "sset", "Mic", "nocap"]


async def test_con_el_micro_abierto_no_se_reaplica_nada(tmp_path: Path) -> None:
    import asyncio

    demonio, _, ejecutor = _demonio(tmp_path, [MIC_SILENCIADO, MIC_ABIERTO])
    await demonio.atender(Gesto(KEY_MUTE, Nivel.CORTO, 12))
    ordenes_antes = len(ejecutor.ordenes)

    demonio._al_reconectar()
    for _ in range(6):
        await asyncio.sleep(0)

    assert len(ejecutor.ordenes) == ordenes_antes


# --- El recordatorio de micrófono silenciado ----------------------------------
#
# Existe porque el agente NO sabe que está mudo: el silencio se hace en el
# mezclador de ALSA, así que su pipeline recibe bloques de ceros y no tiene nada
# que registrar. Desde fuera es indistinguible de un agente averiado.


async def test_el_recordatorio_avisa_mientras_el_micro_siga_silenciado(tmp_path: Path) -> None:
    import asyncio

    demonio, pitidos, _ = _demonio(tmp_path, [MIC_ABIERTO, MIC_SILENCIADO])
    await demonio.atender(Gesto(KEY_MUTE, Nivel.CORTO, 12))
    assert pitidos.sonados == ["no"]

    tarea = asyncio.create_task(demonio._recordar_micro(intervalo=0.01))
    await asyncio.sleep(0.05)
    tarea.cancel()

    # El `no` del silencio, más al menos un recordatorio.
    assert pitidos.sonados.count("no") >= 2


async def test_el_recordatorio_calla_con_el_micro_abierto(tmp_path: Path) -> None:
    import asyncio

    demonio, pitidos, _ = _demonio(tmp_path, [MIC_SILENCIADO, MIC_ABIERTO])
    await demonio.atender(Gesto(KEY_MUTE, Nivel.CORTO, 12))
    assert pitidos.sonados == ["si"]

    tarea = asyncio.create_task(demonio._recordar_micro(intervalo=0.01))
    await asyncio.sleep(0.05)
    tarea.cancel()

    assert pitidos.sonados == ["si"]


async def test_el_recordatorio_no_insiste_si_no_sabe_el_estado(tmp_path: Path) -> None:
    """Con estado desconocido no se molesta: avisar de algo que no consta es ruido."""
    import asyncio

    demonio, pitidos, _ = _demonio(tmp_path, [])
    assert demonio._micro_abierto is None

    tarea = asyncio.create_task(demonio._recordar_micro(intervalo=0.01))
    await asyncio.sleep(0.05)
    tarea.cancel()

    assert pitidos.sonados == []
