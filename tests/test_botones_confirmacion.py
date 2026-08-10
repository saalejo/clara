"""Confirmación de lo destructivo, órdenes en vuelo y resultado diferido.

Lo que se fija aquí es el conjunto de barreras que impide que un codazo pare el
agente, y la honestidad del feedback: que el pitido de «hecho» suene cuando la
unidad ha llegado de verdad a su sitio y no cuando systemd aceptó la orden.

Sin bus de D-Bus y sin `sleep` reales: el control se sustituye por un doble y el
reloj de la espera se inyecta.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from test_botones_acciones import PitidosFalsos
from test_botones_mezclador import MIC_ABIERTO, MIC_SILENCIADO, EjecutorFalso
from voice_agent_botones.acciones import Verbo
from voice_agent_botones.config import Ajustes
from voice_agent_botones.demonio import Demonio
from voice_agent_botones.gestos import (
    KEY_MUTE,
    KEY_VOLUMEDOWN,
    KEY_VOLUMEUP,
    Gesto,
    Nivel,
)
from voice_agent_botones.mezclador import Mezclador
from voice_agent_botones.servicios import Servicios
from voice_agent_core.rutas import ruta_estado
from voice_agent_core.systemd import ControlSystemd, ErrorDeControl, EstadoUnidad

AGENTE = "voice-agent.service"
TELEFONIA = "voice-agent-telefonia.service"

# Gestos que el mapa traduce a los tres verbos destructivos.
PARAR_O_ARRANCAR_AGENTE = Gesto(KEY_VOLUMEUP, Nivel.MUY_LARGO, 3000)
REINICIAR_AGENTE = Gesto(KEY_VOLUMEDOWN, Nivel.MUY_LARGO, 3000)
SOLO_TARJETA = Gesto(KEY_VOLUMEDOWN, Nivel.LARGO, 900)
CONFIRMAR = Gesto(KEY_MUTE, Nivel.CORTO, 20)


class ControlFalso(ControlSystemd):
    """Un systemd de mentira, con estados que el test decide.

    Distingue entre lo que la unidad **es** (`activas`) y lo que `estado()`
    **reporta** (`visibles`), separadas por `retardo` consultas. Es lo que permite
    simular la asimetría real: `StartUnit` vuelve al instante y el agente tarda
    doce segundos en reportarse activo.

    El retardo se arma al mandar la orden y NO afecta a la consulta previa, que es
    la que decide si el gesto arranca o para. Falsearla también haría que el
    demonio tomara la decisión contraria y el test mediría otra cosa.
    """

    def __init__(
        self,
        activas: set[str] | None = None,
        *,
        revienta: bool = False,
        retardo: int = 0,
        testigo: Path | None = None,
        retardo_testigo: int = 0,
    ) -> None:
        super().__init__(frozenset({AGENTE, TELEFONIA}))
        self.activas = set(activas or ())
        self.visibles = set(self.activas)
        self.revienta = revienta
        self.retardo = retardo
        self.testigo = testigo
        self.retardo_testigo = retardo_testigo
        self.ordenes: list[tuple[str, str]] = []
        # Lo que `estado()` ha ido reportando. Es la base de las aserciones
        # deterministas: contar turnos del bucle para afirmar «esto todavía no ha
        # pasado» sería flaky, porque `asyncio.to_thread` usa hilos de verdad y el
        # número de turnos necesarios depende de la carga de la máquina.
        self.reportados: list[str] = []
        self._quedan = 0
        self._quedan_testigo = 0
        self._transicion: str | None = None

    @property
    def consultas(self) -> int:
        """Cuántas veces se ha preguntado el estado."""
        return len(self.reportados)

    def estado(self, unidad: str) -> EstadoUnidad:
        if self.revienta:
            raise ErrorDeControl("no hay bus")

        if self._quedan > 0:
            self._quedan -= 1
            # En plena transición systemd no dice `active` ni `inactive`, y esa es
            # justo la fase donde el código se equivocaba.
            active_state = self._transicion or "inactive"
        else:
            self.visibles = set(self.activas)
            self._transicion = None
            active_state = "active" if unidad in self.visibles else "inactive"
            if active_state == "active":
                self._quizas_publicar_testigo()

        self.reportados.append(active_state)
        return EstadoUnidad(
            unidad=unidad,
            active_state=active_state,
            sub_state="running" if active_state == "active" else "dead",
            resultado="success",
        )

    def _quizas_publicar_testigo(self) -> None:
        """Simula al agente escribiendo su `estado_arranque.json` con retraso.

        La mtime se fija en el futuro a propósito: el código exige que el testigo
        sea *más nuevo* que el instante en que se pidió la orden, y dos escrituras
        dentro del mismo tick del reloj podrían no distinguirse.
        """
        if self.testigo is None:
            return
        if self._quedan_testigo > 0:
            self._quedan_testigo -= 1
            return
        self.testigo.parent.mkdir(parents=True, exist_ok=True)
        self.testigo.touch()
        futuro = time.time() + 1000
        os.utime(self.testigo, (futuro, futuro))

    def _ordenar(self, accion: str, unidad: str) -> None:
        self.ordenes.append((accion, unidad))
        self._quedan = self.retardo
        self._quedan_testigo = self.retardo_testigo
        self._transicion = "deactivating" if accion == "parar" else "activating"

    def arrancar(self, unidad: str) -> str:
        self._ordenar("arrancar", unidad)
        self.activas.add(unidad)
        return "/job/1"

    def parar(self, unidad: str) -> str:
        self._ordenar("parar", unidad)
        self.activas.discard(unidad)
        return "/job/2"

    def reiniciar(self, unidad: str) -> str:
        self._ordenar("reiniciar", unidad)
        self.activas.add(unidad)
        return "/job/3"


class Reloj:
    """Reloj monótono manual para la espera de unidades."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 0.5
        return self.t


def _demonio(
    tmp_path: Path,
    control: ControlFalso,
    *,
    respuestas: list[str] | None = None,
    **kwargs: Any,
) -> tuple[Demonio, PitidosFalsos]:
    ajustes = Ajustes(_env_file=None, directorio_datos=tmp_path, **kwargs)  # type: ignore[call-arg]
    # El demonio exige ver el testigo del agente renovado antes de dar el arranque
    # por bueno, así que el doble tiene que saber dónde escribirlo.
    if control.testigo is None:
        control.testigo = ruta_estado(ajustes.directorio_datos)
    pitidos = PitidosFalsos(tmp_path / "pitidos")
    demonio = Demonio(
        ajustes,
        mezclador=Mezclador(ajustes, ejecutor=EjecutorFalso(respuestas or [])),
        pitidos=pitidos,
        servicios=Servicios(ajustes, control=control, reloj=Reloj(), intervalo=0.0),
    )
    return demonio, pitidos


async def _reposar(veces: int = 40) -> None:
    """Deja correr las tareas de fondo sin dormir de verdad."""
    for _ in range(veces):
        await asyncio.sleep(0)


async def _hasta(condicion: Callable[[], bool], timeout: float = 5.0) -> None:
    """Espera a que se cumpla una condición, o falla el test.

    Sustituye a «dar N turnos al bucle y comprobar»: el gobierno de servicios pasa
    por `asyncio.to_thread`, así que el número de turnos necesarios depende del
    planificador de hilos y de la carga de la máquina. Contar turnos hacía que los
    tests pasaran en aislado y fallaran en la batería completa.
    """
    limite = time.monotonic() + timeout
    while not condicion():
        if time.monotonic() > limite:
            raise AssertionError(f"La condición no se cumplió en {timeout} s")
        await asyncio.sleep(0.005)


# --- Arrancar es inocuo -------------------------------------------------------


async def test_arrancar_el_agente_no_pide_confirmacion(tmp_path: Path) -> None:
    """Asimetría deliberada: arrancar no interrumpe nada, parar sí."""
    control = ControlFalso(activas=set())
    demonio, pitidos = _demonio(tmp_path, control)

    await demonio.atender(PARAR_O_ARRANCAR_AGENTE)
    await _reposar()

    assert control.ordenes == [("arrancar", AGENTE)]
    assert "pregunta" not in pitidos.sonados


# --- Parar sí ------------------------------------------------------------------


async def test_parar_el_agente_pide_confirmacion(tmp_path: Path) -> None:
    control = ControlFalso(activas={AGENTE})
    demonio, pitidos = _demonio(tmp_path, control)

    await demonio.atender(PARAR_O_ARRANCAR_AGENTE)

    assert control.ordenes == []
    assert pitidos.sonados == ["pregunta"]


async def test_un_clic_de_mute_confirma(tmp_path: Path) -> None:
    control = ControlFalso(activas={AGENTE})
    demonio, pitidos = _demonio(tmp_path, control)

    await demonio.atender(PARAR_O_ARRANCAR_AGENTE)
    await demonio.atender(CONFIRMAR)
    # El «no» de «queda parada» es diferido (asyncio.to_thread); contar turnos
    # del bucle no basta, ver `_hasta`.
    await _hasta(lambda: "no" in pitidos.sonados)

    assert control.ordenes == [("parar", AGENTE)]
    # `si` de recibido y `no` de «queda parada».
    assert pitidos.sonados == ["pregunta", "si", "no"]


async def test_otro_gesto_cancela_y_no_se_ejecuta(tmp_path: Path) -> None:
    """Quien cancela no quiere además otra cosa."""
    control = ControlFalso(activas={AGENTE})
    demonio, pitidos = _demonio(tmp_path, control, respuestas=[MIC_ABIERTO, MIC_SILENCIADO])

    await demonio.atender(PARAR_O_ARRANCAR_AGENTE)
    await demonio.atender(Gesto(KEY_VOLUMEUP, Nivel.CORTO, 90))
    await _reposar()

    assert control.ordenes == []
    assert pitidos.sonados == ["pregunta", "cancelado"]


async def test_la_confirmacion_caduca_sola_y_se_anuncia(tmp_path: Path) -> None:
    """Sin esto, la caducidad solo se notaría al llegar el gesto siguiente."""
    control = ControlFalso(activas={AGENTE})
    demonio, pitidos = _demonio(tmp_path, control, confirmacion_segundos=0.01)

    await demonio.atender(PARAR_O_ARRANCAR_AGENTE)
    await asyncio.sleep(0.05)

    assert control.ordenes == []
    assert pitidos.sonados == ["pregunta", "cancelado"]


async def test_confirmar_despues_de_caducar_no_ejecuta(tmp_path: Path) -> None:
    control = ControlFalso(activas={AGENTE})
    demonio, pitidos = _demonio(
        tmp_path,
        control,
        respuestas=[MIC_ABIERTO, MIC_SILENCIADO],
        confirmacion_segundos=0.01,
    )

    await demonio.atender(PARAR_O_ARRANCAR_AGENTE)
    await asyncio.sleep(0.05)
    # Ya caducada, el clic de MUTE vuelve a significar «micrófono».
    await demonio.atender(CONFIRMAR)
    await _reposar()

    assert control.ordenes == []
    assert pitidos.sonados == ["pregunta", "cancelado", "no"]


async def test_reiniciar_siempre_pide_confirmacion(tmp_path: Path) -> None:
    control = ControlFalso(activas={AGENTE})
    demonio, pitidos = _demonio(tmp_path, control)

    await demonio.atender(REINICIAR_AGENTE)
    assert control.ordenes == []
    await demonio.atender(CONFIRMAR)
    # El «listo» es diferido: llega tras vigilar la unidad en `asyncio.to_thread`,
    # así que darle N turnos al bucle no basta (ver `_hasta`).
    await _hasta(lambda: "listo" in pitidos.sonados)

    assert control.ordenes == [("reiniciar", AGENTE)]
    assert pitidos.sonados == ["pregunta", "si", "listo"]


# --- El modo «solo tarjeta de sonido» -----------------------------------------


async def test_apagar_la_telefonia_pide_confirmacion(tmp_path: Path) -> None:
    control = ControlFalso(activas={TELEFONIA})
    demonio, _pitidos = _demonio(tmp_path, control)

    await demonio.atender(SOLO_TARJETA)
    assert control.ordenes == []
    await demonio.atender(CONFIRMAR)
    await _reposar()

    assert control.ordenes == [("parar", TELEFONIA)]


async def test_volver_a_encender_la_telefonia_es_inmediato(tmp_path: Path) -> None:
    control = ControlFalso(activas=set())
    demonio, pitidos = _demonio(tmp_path, control)

    await demonio.atender(SOLO_TARJETA)
    await _reposar()

    assert control.ordenes == [("arrancar", TELEFONIA)]
    assert "pregunta" not in pitidos.sonados


async def test_el_modo_solo_tarjeta_no_toca_al_agente(tmp_path: Path) -> None:
    control = ControlFalso(activas={AGENTE, TELEFONIA})
    demonio, _ = _demonio(tmp_path, control)

    await demonio.atender(SOLO_TARJETA)
    await demonio.atender(CONFIRMAR)
    await _reposar()

    assert AGENTE in control.activas
    assert control.ordenes == [("parar", TELEFONIA)]


# --- Orden aceptada no es orden completada ------------------------------------


async def test_el_pitido_de_hecho_espera_a_que_la_unidad_llegue(tmp_path: Path) -> None:
    """`StartUnit` vuelve en centésimas; el agente tarda unos doce segundos.

    El `si` es «recibido» y el `listo` es «ya está arriba». Aquí se simula que la
    unidad tarda tres consultas en reportarse activa.
    """
    control = ControlFalso(activas=set(), retardo=3)
    demonio, pitidos = _demonio(tmp_path, control)

    await demonio.atender(PARAR_O_ARRANCAR_AGENTE)
    # El `si` es sincrónico con la orden; el `listo` llega cuando la unidad llega.
    assert pitidos.sonados == ["si"]

    await _hasta(lambda: "listo" in pitidos.sonados)
    assert pitidos.sonados == ["si", "listo"]
    # Hubo que atravesar los `activating`: no se dio por hecho al primer intento.
    assert "activating" in control.reportados


async def test_la_parada_no_se_da_por_hecha_en_deactivating(tmp_path: Path) -> None:
    """Regresión medida: `no active` no es `parado`.

    En la placa, el pitido de «parada» sonó **40 ms** después de la orden, porque
    `deactivating` no es `active` y se estaba contando como parado — con el
    contenedor todavía muriéndose. Aquí el doble reporta `deactivating` durante
    tres consultas y no puede bastar.
    """
    control = ControlFalso(activas={AGENTE}, retardo=3)
    demonio, pitidos = _demonio(tmp_path, control)

    await demonio.atender(PARAR_O_ARRANCAR_AGENTE)
    await demonio.atender(CONFIRMAR)
    await _hasta(lambda: "no" in pitidos.sonados)

    assert pitidos.sonados == ["pregunta", "si", "no"]
    # La prueba de que no se rindió antes de tiempo: hubo que atravesar los
    # `deactivating`, y el último estado visto fue `inactive`. Con el fallo, el
    # bucle habría terminado en el primer `deactivating`.
    assert "deactivating" in control.reportados
    assert control.reportados[-1] == "inactive"


async def test_parar_el_agente_acaba_en_fallido_y_eso_es_exito(tmp_path: Path) -> None:
    """Regresión medida: parar el agente deja la unidad en `failed`, no en `inactive`.

    Su unidad es `Type=notify` con `KillMode=mixed` y el agente **no maneja
    SIGTERM** —está documentado que su `finally` no corre cuando systemd lo para—,
    así que el contenedor sale con código distinto de cero y systemd marca la unidad
    `failed`. Medido en la placa: once segundos de parada limpia que acaban en
    `failed: exit-code`.

    La unidad no está corriendo, que es lo que se pidió. Pitar `error` ahí sería
    mentir en el otro sentido.
    """

    class ControlQueMuereAlParar(ControlFalso):
        def estado(self, unidad: str) -> EstadoUnidad:
            base = super().estado(unidad)
            if ("parar", unidad) in self.ordenes and not base.activo:
                return EstadoUnidad(
                    unidad=unidad,
                    active_state="failed",
                    sub_state="failed",
                    resultado="exit-code",
                )
            return base

    control = ControlQueMuereAlParar(activas={AGENTE})
    demonio, pitidos = _demonio(tmp_path, control)

    await demonio.atender(PARAR_O_ARRANCAR_AGENTE)
    await demonio.atender(CONFIRMAR)
    await _hasta(lambda: len(pitidos.sonados) >= 3)

    assert pitidos.sonados == ["pregunta", "si", "no"]


async def test_arrancando_un_fallido_si_es_un_error(tmp_path: Path) -> None:
    """La asimetría del test anterior no puede volverse ceguera al arrancar."""

    class ControlQueNoArranca(ControlFalso):
        def estado(self, unidad: str) -> EstadoUnidad:
            super().estado(unidad)
            return EstadoUnidad(
                unidad=unidad, active_state="failed", sub_state="failed", resultado="exit-code"
            )

    control = ControlQueNoArranca(activas=set())
    demonio, pitidos = _demonio(tmp_path, control)

    await demonio.atender(PARAR_O_ARRANCAR_AGENTE)
    await _hasta(lambda: "error" in pitidos.sonados)

    assert pitidos.sonados == ["si", "error"]


async def test_el_arpegio_espera_al_testigo_y_no_solo_a_systemd(tmp_path: Path) -> None:
    """Regresión medida: para el agente, `active` de systemd NO significa listo.

    Su unidad la genera Quadlet con `--sdnotify=conmon`, así que systemd la da por
    activa en cuanto arranca el contenedor. Medido en la placa: `active` a las
    00:54:50 y el `estado_arranque.json` —que el agente escribe tras montar el
    pipeline— a las 00:55:14. **Veinticuatro segundos.**

    Aquí el doble se pone activo enseguida y tarda cinco consultas más en publicar
    el testigo; el arpegio no puede sonar hasta entonces.
    """
    testigo = ruta_estado(tmp_path)
    testigo.parent.mkdir(parents=True, exist_ok=True)
    # El fichero YA existe de un arranque anterior: lo que importa es que se
    # renueve, no que aparezca. Es el caso realista y el que un `exists()` fallaría.
    testigo.write_text("{}")
    viejo = testigo.stat().st_mtime

    control = ControlFalso(activas=set(), testigo=testigo, retardo_testigo=5)
    demonio, pitidos = _demonio(tmp_path, control)

    await demonio.atender(PARAR_O_ARRANCAR_AGENTE)
    await _hasta(lambda: "listo" in pitidos.sonados)

    assert pitidos.sonados == ["si", "listo"]
    assert testigo.stat().st_mtime > viejo
    # La prueba de que esperó al testigo y no a systemd: la unidad se reportó
    # `active` desde la primera consulta de la espera, así que sin la comprobación
    # del testigo el arpegio habría sonado con dos consultas. Hicieron falta seis
    # más para que el testigo apareciera.
    assert control.reportados.count("active") > 5
    assert control.consultas >= 7


async def test_el_puente_de_telefonia_no_lleva_testigo(tmp_path: Path) -> None:
    """Es nativo: ahí `active` sí significa listo, y exigir un testigo lo colgaría."""
    demonio, _ = _demonio(tmp_path, ControlFalso())
    assert demonio._testigo_de(TELEFONIA) is None
    assert demonio._testigo_de(AGENTE) is not None


async def test_una_unidad_que_no_llega_pita_error(tmp_path: Path) -> None:
    # Nunca se pondrá visible como activa: el retardo agota la ventana de espera.
    control = ControlFalso(activas=set(), retardo=10_000)
    demonio, pitidos = _demonio(tmp_path, control, espera_unidad_segundos=1.0)

    await demonio.atender(PARAR_O_ARRANCAR_AGENTE)
    await _hasta(lambda: "error" in pitidos.sonados)

    assert pitidos.sonados == ["si", "error"]


async def test_una_segunda_orden_en_vuelo_se_rechaza(tmp_path: Path) -> None:
    """Lo que evita encolar tres reinicios por nerviosismo."""
    control = ControlFalso(activas=set(), retardo=5)
    demonio, pitidos = _demonio(tmp_path, control)

    await demonio.atender(PARAR_O_ARRANCAR_AGENTE)
    await demonio.atender(PARAR_O_ARRANCAR_AGENTE)

    assert control.ordenes == [("arrancar", AGENTE)]
    assert pitidos.sonados == ["si", "error"]


async def test_al_terminar_la_unidad_vuelve_a_aceptar_ordenes(tmp_path: Path) -> None:
    control = ControlFalso(activas=set())
    demonio, pitidos = _demonio(tmp_path, control)

    await demonio.atender(PARAR_O_ARRANCAR_AGENTE)
    await _hasta(lambda: "listo" in pitidos.sonados)
    # Ahora está activa: el mismo gesto la para, con confirmación.
    await demonio.atender(PARAR_O_ARRANCAR_AGENTE)
    await demonio.atender(CONFIRMAR)
    await _hasta(lambda: "no" in pitidos.sonados)

    assert control.ordenes == [("arrancar", AGENTE), ("parar", AGENTE)]


# --- Nunca lanza --------------------------------------------------------------


async def test_un_systemd_que_no_responde_pita_error(tmp_path: Path) -> None:
    control = ControlFalso(revienta=True)
    demonio, pitidos = _demonio(tmp_path, control)

    await demonio.atender(PARAR_O_ARRANCAR_AGENTE)
    await _reposar()

    assert control.ordenes == []
    assert pitidos.sonados == ["error"]


async def test_una_unidad_fallida_no_espera_hasta_el_final(tmp_path: Path) -> None:
    """Un `failed` no se arregla esperando más."""

    class ControlFallido(ControlFalso):
        def estado(self, unidad: str) -> EstadoUnidad:
            return EstadoUnidad(
                unidad=unidad, active_state="failed", sub_state="failed", resultado="exit-code"
            )

    control = ControlFallido(activas=set())
    demonio, pitidos = _demonio(tmp_path, control, espera_unidad_segundos=300.0)

    await demonio.atender(PARAR_O_ARRANCAR_AGENTE)
    await _hasta(lambda: "error" in pitidos.sonados)

    assert pitidos.sonados == ["si", "error"]


async def test_recoger_tareas_no_deja_nada_colgando(tmp_path: Path) -> None:
    control = ControlFalso(activas=set(), retardo=10_000)
    demonio, _ = _demonio(tmp_path, control, espera_unidad_segundos=300.0)

    await demonio.atender(PARAR_O_ARRANCAR_AGENTE)
    await _reposar(10)
    await demonio._recoger_tareas()

    assert demonio._seguimientos == set()


@pytest.mark.parametrize(
    "verbo", [Verbo.AGENTE_ALTERNAR, Verbo.AGENTE_REINICIAR, Verbo.SOLO_TARJETA]
)
def test_cada_verbo_destructivo_apunta_a_una_unidad_gobernable(
    tmp_path: Path, verbo: Verbo
) -> None:
    demonio, _ = _demonio(tmp_path, ControlFalso())
    assert demonio._unidad_de(verbo) in demonio._ajustes.unidades_gobernadas
