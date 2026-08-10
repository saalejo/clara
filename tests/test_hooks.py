"""Tests del sistema de hooks.

El más importante de todos es `test_los_frames_de_control_siempre_pasan`:
tragarse un `StartFrame` o un `InterruptionFrame` no da un error, cuelga el
pipeline entero y lo único que se ve es un "timeout waiting for..." que no
señala a ningún sitio. Es el fallo que este módulo tiene que hacer imposible.

Nada de esto toca la red ni carga modelos: se construyen frames a mano y se
comprueba qué sale por el otro lado.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, cast

import pytest
from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InterruptionFrame,
    LLMTextFrame,
    StartFrame,
    TranscriptionFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import (
    FrameDirection,
    FrameProcessorSetup,
)
from pipecat.utils.asyncio.task_manager import TaskManager

from voice_agent.hooks import HookProcessor, construir_procesadores
from voice_agent_core.runtime import AccionHook, EventoHook, HookConfig, RuntimeConfig


def _hook(**campos: Any) -> HookConfig:
    """Un hook activo con los campos que se le pasen."""
    base: dict[str, Any] = {
        "nombre": "prueba",
        "habilitado": True,
        "evento": EventoHook.TRANSCRIPCION_LISTA,
        "accion": AccionHook.REESCRIBIR,
        "patron": "nanopi",
        "reemplazo": "NanoPi",
    }
    base.update(campos)
    return HookConfig(**base)


def _transcripcion(texto: str) -> TranscriptionFrame:
    return TranscriptionFrame(text=texto, user_id="u", timestamp="2026-07-28T00:00:00Z")


async def _arrancar(procesador: HookProcessor) -> HookProcessor:
    """Deja el procesador en condiciones de funcionar fuera de un pipeline.

    `FrameProcessor.process_frame` tramita por su cuenta los frames de sistema
    —`StartFrame`, `InterruptionFrame`, `CancelFrame`— y `create_task` necesita
    un gestor de tareas. Sin este montaje, un test que pase un `StartFrame`
    falla con un "TaskManager is not initialized" que no tiene nada que ver con
    lo que se está probando.
    """
    await procesador.setup(
        FrameProcessorSetup(
            clock=SystemClock(),  # type: ignore[no-untyped-call]
            task_manager=TaskManager(loop=asyncio.get_running_loop()),
            # Los hooks no lo usan; en un pipeline de verdad lo pone Pipecat.
            pipeline_worker=cast(Any, None),
        )
    )
    return procesador


async def _pasar(procesador: HookProcessor, frame: Frame) -> list[Frame]:
    """Pasa un frame por el procesador y devuelve lo que empujó hacia abajo.

    Se sustituye `push_frame` en lugar de encadenar procesadores de verdad
    porque lo que se quiere observar es exactamente eso: qué sale y qué no.
    """
    await _arrancar(procesador)
    empujados: list[Frame] = []

    async def _capturar(
        frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        empujados.append(frame)

    procesador.push_frame = _capturar  # type: ignore[method-assign]
    await procesador.process_frame(frame, FrameDirection.DOWNSTREAM)
    return empujados


# --- La garantía que sostiene todo lo demás ---------------------------------


@pytest.mark.parametrize(
    "frame",
    [
        StartFrame(),
        EndFrame(),
        CancelFrame(),
        InterruptionFrame(),
        UserStoppedSpeakingFrame(),
        ErrorFrame(error="algo se rompió"),
    ],
)
async def test_los_frames_de_control_siempre_pasan(frame: Frame) -> None:
    # Un hook que veta TODO. Ni así puede tragarse un frame que no sea DataFrame:
    # hacerlo colgaría el pipeline sin dejar rastro útil en los logs.
    procesador = HookProcessor(
        [
            _hook(accion=AccionHook.VETAR, patron=".*", evento=EventoHook.TRANSCRIPCION_LISTA),
            _hook(
                nombre="v2", accion=AccionHook.VETAR, patron=".*", evento=EventoHook.RESPUESTA_TEXTO
            ),
        ],
        nombre="prueba",
    )
    assert await _pasar(procesador, frame) == [frame]


# --- Reescritura y veto ------------------------------------------------------


async def test_la_reescritura_cambia_el_texto_en_el_sitio() -> None:
    procesador = HookProcessor([_hook()], nombre="prueba")
    frame = _transcripcion("enciende la nanopi ahora")

    empujados = await _pasar(procesador, frame)

    assert len(empujados) == 1
    assert empujados[0].text == "enciende la NanoPi ahora"  # type: ignore[attr-defined]


async def test_el_veto_descarta_el_frame() -> None:
    procesador = HookProcessor(
        [_hook(accion=AccionHook.VETAR, patron=r"\btarjeta de crédito\b")], nombre="prueba"
    )

    assert await _pasar(procesador, _transcripcion("mi tarjeta de crédito es 4111")) == []


async def test_el_veto_deja_pasar_lo_que_no_casa() -> None:
    procesador = HookProcessor(
        [_hook(accion=AccionHook.VETAR, patron=r"\bsecreto\b")], nombre="prueba"
    )

    assert len(await _pasar(procesador, _transcripcion("hola qué tal"))) == 1


async def test_los_hooks_se_aplican_en_orden() -> None:
    procesador = HookProcessor(
        [
            _hook(nombre="primero", orden=1, patron="uno", reemplazo="dos"),
            _hook(nombre="segundo", orden=2, patron="dos", reemplazo="tres"),
        ],
        nombre="prueba",
    )

    empujados = await _pasar(procesador, _transcripcion("uno"))

    assert empujados[0].text == "tres"  # type: ignore[attr-defined]


async def test_un_hook_de_otro_evento_no_toca_el_frame() -> None:
    procesador = HookProcessor(
        [_hook(evento=EventoHook.RESPUESTA_TEXTO, patron="hola", reemplazo="adiós")],
        nombre="prueba",
    )

    empujados = await _pasar(procesador, _transcripcion("hola"))

    assert empujados[0].text == "hola"  # type: ignore[attr-defined]


async def test_un_patron_que_no_compila_se_ignora_sin_romper() -> None:
    # El panel valida al guardar, así que llegar aquí con esto significa que
    # alguien editó el JSON a mano. Aun así no puede tumbar el arranque.
    procesador = HookProcessor([_hook(patron="(sin cerrar")], nombre="prueba")

    assert len(await _pasar(procesador, _transcripcion("da igual"))) == 1


# --- Comandos ----------------------------------------------------------------


async def test_un_comando_no_bloqueante_no_retrasa_el_turno(tmp_path: Path) -> None:
    # Es la razón de que 'bloqueante' sea opt-in: si se esperara, el retardo se
    # sumaría a cada turno de la conversación.
    #
    # El comando tarda medio segundo y deja un testigo al terminar, lo que
    # permite comprobar las dos mitades del asunto: que el turno NO lo esperó, y
    # que aun así se ejecutó de verdad. Se le deja acabar dentro del test en vez
    # de cancelarlo: un hijo cancelado a medias deja el vigilante de procesos de
    # asyncio en un estado que bloquea al siguiente test que lance un proceso.
    testigo = tmp_path / "termine"
    procesador = HookProcessor(
        [
            _hook(
                accion=AccionHook.EJECUTAR_COMANDO,
                comando=["sh", "-c", f"sleep 0.5; touch {testigo}"],
                timeout_secs=5.0,
                bloqueante=False,
            )
        ],
        nombre="prueba",
    )

    inicio = time.monotonic()
    empujados = await _pasar(procesador, _transcripcion("hola"))
    transcurrido = time.monotonic() - inicio

    assert len(empujados) == 1
    assert transcurrido < 0.3, (
        f"el turno esperó {transcurrido:.2f}s a un hook que no debía bloquear"
    )
    assert not testigo.exists(), "el comando ya había terminado: el test no prueba nada"

    await asyncio.sleep(1.0)
    assert testigo.exists(), "el comando no llegó a ejecutarse"


async def test_un_comando_bloqueante_se_espera(tmp_path: Path) -> None:
    testigo = tmp_path / "paso-por-aqui"
    procesador = HookProcessor(
        [
            _hook(
                accion=AccionHook.EJECUTAR_COMANDO,
                comando=["touch", str(testigo)],
                bloqueante=True,
                timeout_secs=2.0,
            )
        ],
        nombre="prueba",
    )

    await _pasar(procesador, _transcripcion("hola"))

    assert testigo.exists()


async def test_el_comando_recibe_el_contexto_por_stdin(tmp_path: Path) -> None:
    destino = tmp_path / "carga.json"
    procesador = HookProcessor(
        [
            _hook(
                accion=AccionHook.EJECUTAR_COMANDO,
                comando=["sh", "-c", f"cat > {destino}"],
                bloqueante=True,
                timeout_secs=2.0,
            )
        ],
        nombre="prueba",
    )

    await _pasar(procesador, _transcripcion("hola mundo"))

    assert "hola mundo" in destino.read_text()
    assert "transcripcion_lista" in destino.read_text()


async def test_un_comando_que_se_pasa_de_tiempo_se_mata() -> None:
    procesador = HookProcessor(
        [
            _hook(
                accion=AccionHook.EJECUTAR_COMANDO,
                comando=["sleep", "30"],
                bloqueante=True,
                timeout_secs=0.3,
            )
        ],
        nombre="prueba",
    )

    inicio = time.monotonic()
    await _pasar(procesador, _transcripcion("hola"))
    transcurrido = time.monotonic() - inicio

    assert transcurrido < 5.0, "el timeout no cortó el proceso"


async def test_un_comando_inexistente_no_rompe_el_turno() -> None:
    procesador = HookProcessor(
        [
            _hook(
                accion=AccionHook.EJECUTAR_COMANDO,
                comando=["/no/existe/este/binario"],
                bloqueante=True,
                timeout_secs=2.0,
            )
        ],
        nombre="prueba",
    )

    assert len(await _pasar(procesador, _transcripcion("hola"))) == 1


# --- Construcción de los dos procesadores ------------------------------------


def test_sin_hooks_no_se_crea_ningun_procesador() -> None:
    # Coste cero para la instalación por defecto.
    assert construir_procesadores(RuntimeConfig()) == (None, None)


def test_los_hooks_desactivados_no_cuentan() -> None:
    runtime = RuntimeConfig(hooks=[_hook(habilitado=False)])
    assert construir_procesadores(runtime) == (None, None)


def test_cada_hook_va_a_su_lado_del_pipeline() -> None:
    runtime = RuntimeConfig(
        hooks=[
            _hook(nombre="entrada", evento=EventoHook.TRANSCRIPCION_LISTA),
            _hook(nombre="salida", evento=EventoHook.RESPUESTA_TEXTO),
        ]
    )
    entrada, salida = construir_procesadores(runtime)

    assert entrada is not None and salida is not None


async def test_el_procesador_de_salida_reescribe_la_respuesta() -> None:
    runtime = RuntimeConfig(
        hooks=[
            _hook(
                nombre="salida", evento=EventoHook.RESPUESTA_TEXTO, patron="euro", reemplazo="dólar"
            )
        ]
    )
    _, salida = construir_procesadores(runtime)
    assert salida is not None

    empujados = await _pasar(salida, LLMTextFrame(text="cuesta un euro"))

    assert empujados[0].text == "cuesta un dólar"  # type: ignore[attr-defined]
