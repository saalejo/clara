"""Tests de las muletillas: la caché y el disparo.

De la caché se prueba la invalidación: que cambiar de voz o de frecuencia
genere ficheros distintos. Es una propiedad silenciosa —si se rompe, el agente
sigue arrancando y funcionando, pero suelta muletillas con la voz antigua
mezcladas con respuestas en la nueva— y por eso conviene fijarla en un test.

Del disparo se prueba, con un pipeline de Pipecat de verdad, que la muletilla
de espera suena aunque la petición al LLM ya esté lanzada y que solo el primer
token la cancela. Es EXACTAMENTE el bug que mató la funcionalidad durante
meses sin que nadie lo notara: `LLMFullResponseStartFrame` se emite al lanzar
la petición —milisegundos después de cerrarse el turno— y cancelando con él
la muletilla no sonaba jamás.
"""

from __future__ import annotations

import asyncio
from itertools import pairwise
from pathlib import Path

from pipecat.frames.frames import (
    Frame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

from voice_agent.fillers import FillerBank, FillerProcessor
from voice_agent_core.config import Settings


def _bank(tmp_path: Path, sample_rate: int | None = None, **kwargs: object) -> FillerBank:
    ajustes = {"data_dir": tmp_path, "tts_voice": "es_ES-davefx-medium", "audio_sample_rate": 16000}
    ajustes.update(kwargs)
    settings = Settings(_env_file=None, **ajustes)  # type: ignore[arg-type, call-arg]
    return FillerBank(settings, sample_rate=sample_rate)


def test_la_misma_frase_y_voz_dan_la_misma_ruta(tmp_path: Path) -> None:
    a, b = _bank(tmp_path), _bank(tmp_path)
    assert a._ruta("Déjame consultarlo.") == b._ruta("Déjame consultarlo.")


def test_cambiar_de_voz_invalida_la_cache(tmp_path: Path) -> None:
    """Cambiar TTS_VOICE debe resintetizar, no reutilizar el audio anterior."""
    original = _bank(tmp_path)
    otra = _bank(tmp_path, tts_voice="es_MX-claude-high")
    assert original._ruta("Un momento.") != otra._ruta("Un momento.")


def test_cambiar_la_frecuencia_invalida_la_cache(tmp_path: Path) -> None:
    """El audio se guarda ya remuestreado, así que la frecuencia forma parte de la clave."""
    a = _bank(tmp_path)
    b = _bank(tmp_path, audio_sample_rate=8000)
    assert a._ruta("Un momento.") != b._ruta("Un momento.")


def test_la_frecuencia_explicita_manda_sobre_la_de_settings(tmp_path: Path) -> None:
    """El pipeline de llamada va a 16 kHz aunque la sala esté a 8 kHz."""
    sala = _bank(tmp_path, audio_sample_rate=8000)
    llamada = _bank(tmp_path, sample_rate=16000, audio_sample_rate=8000)
    assert sala._ruta("Un momento.") != llamada._ruta("Un momento.")


def test_la_frecuencia_explicita_comparte_cache_con_la_implicita(tmp_path: Path) -> None:
    """A la misma frecuencia, el banco de llamada reutiliza el audio de la sala."""
    sala = _bank(tmp_path)  # audio_sample_rate=16000
    llamada = _bank(tmp_path, sample_rate=16000, audio_sample_rate=8000)
    assert sala._ruta("Un momento.") == llamada._ruta("Un momento.")


def test_frases_distintas_dan_rutas_distintas(tmp_path: Path) -> None:
    banco = _bank(tmp_path)
    assert banco._ruta("Déjame consultarlo.") != banco._ruta("Un momento.")


def test_las_muletillas_de_varias_voces_conviven(tmp_path: Path) -> None:
    """Volver a una voz usada antes debe servirse de caché, no resintetizar."""
    frase = "Déjame consultarlo."
    una = _bank(tmp_path)._ruta(frase)
    otra = _bank(tmp_path, tts_voice="es_AR-daniela-high")._ruta(frase)
    assert una.parent == otra.parent  # misma carpeta
    assert una != otra  # ficheros distintos, así que ninguno pisa al otro


def test_no_repite_la_misma_muletilla_seguida(tmp_path: Path) -> None:
    """Oír dos veces la misma frase delata al instante que está enlatada."""
    banco = _bank(tmp_path)
    banco._audio = {"consulta": [b"\x01", b"\x02", b"\x03"]}
    elegidas = [banco.siguiente("consulta") for _ in range(30)]
    for anterior, siguiente in pairwise(elegidas):
        assert anterior != siguiente


def test_categoria_vacia_no_revienta(tmp_path: Path) -> None:
    assert _bank(tmp_path).siguiente("inexistente") is None


class _Colector(FrameProcessor):
    """Registra los tipos de frame que le llegan y los deja pasar."""

    def __init__(self) -> None:
        super().__init__()
        self.tipos: list[str] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        self.tipos.append(type(frame).__name__)
        await self.push_frame(frame, direction)


async def _correr_guion(
    tmp_path: Path, guion: list[tuple[float, Frame]], espera: float, retardo_muletilla: float = 0.2
) -> list[str]:
    """Corre un pipeline [FillerProcessor → colector] inyectando frames con retardos."""
    ajustes = Settings(  # type: ignore[call-arg]
        _env_file=None,
        data_dir=tmp_path,
        filler_delay_secs=retardo_muletilla,
        filler_min_gap_secs=0.0,
    )
    banco = FillerBank(ajustes)
    banco._audio = {"pensando": [b"\x00\x00"]}
    colector = _Colector()
    pipeline = Pipeline([FillerProcessor(banco, ajustes), colector])
    worker = PipelineWorker(pipeline, idle_timeout_secs=None, params=PipelineParams())

    @worker.event_handler("on_pipeline_started")  # type: ignore[untyped-decorator]
    async def _inyectar(w: PipelineWorker, _frame: Frame) -> None:
        async def correr() -> None:
            for retardo, frame in guion:
                await asyncio.sleep(retardo)
                await w.queue_frames([frame])
            await asyncio.sleep(espera)
            await w.cancel()

        asyncio.get_running_loop().create_task(correr())

    runner = WorkerRunner()
    await runner.add_workers(worker)
    await runner.run()
    return colector.tipos


async def test_la_peticion_en_marcha_no_cancela_la_muletilla(tmp_path: Path) -> None:
    """`LLMFullResponseStartFrame` llega milisegundos después del turno; hay que sobrevivirle."""
    tipos = await _correr_guion(
        tmp_path,
        [(0.1, UserStoppedSpeakingFrame()), (0.0, LLMFullResponseStartFrame())],
        espera=0.6,
    )
    assert "TTSAudioRawFrame" in tipos


async def test_el_primer_token_cancela_la_muletilla(tmp_path: Path) -> None:
    # Margen holgado entre el token (a +0.05 s) y la muletilla (a +1 s): con
    # 0.2 s el test flaqueaba en la placa cargada, donde un sleep de 50 ms
    # puede estirarse más que la diferencia, y con 0.5 s volvió a flaquear
    # dentro de la batería completa (dos corridas seguidas el 10-08).
    tipos = await _correr_guion(
        tmp_path,
        [(0.1, UserStoppedSpeakingFrame()), (0.05, LLMTextFrame("Hola"))],
        espera=1.5,
        retardo_muletilla=1.0,
    )
    assert "TTSAudioRawFrame" not in tipos


async def test_reproducir_estampa_la_frecuencia_explicita(tmp_path: Path) -> None:
    """El audio de llamada sale marcado a la frecuencia del pipeline, no a la de settings.

    Con la frecuencia equivocada en el frame el transporte remuestrearía y la
    muletilla sonaría a media velocidad o al doble.
    """
    from pipecat.frames.frames import TTSAudioRawFrame, TTSStartedFrame, TTSStoppedFrame

    ajustes = Settings(_env_file=None, data_dir=tmp_path, audio_sample_rate=8000)  # type: ignore[call-arg]
    banco = FillerBank(ajustes, sample_rate=16000)
    banco._audio = {"pensando": [b"\x00\x00"]}
    proc = FillerProcessor(banco, ajustes, sample_rate=16000)

    emitidos: list[Frame] = []

    async def recoger(frame: Frame, direction: object = None) -> None:
        emitidos.append(frame)

    proc.push_frame = recoger  # type: ignore[method-assign]
    await proc._reproducir("pensando")

    assert [type(f) for f in emitidos] == [TTSStartedFrame, TTSAudioRawFrame, TTSStoppedFrame]
    audio = emitidos[1]
    assert isinstance(audio, TTSAudioRawFrame)
    assert audio.sample_rate == 16000


class TestCuandoNoDebeSonar:
    """El procesador solo debe armar el temporizador tras un turno del usuario."""

    def _procesador(self, tmp_path: Path) -> FillerProcessor:
        from voice_agent.fillers import FillerProcessor

        ajustes = Settings(_env_file=None, data_dir=tmp_path)  # type: ignore[call-arg]
        return FillerProcessor(FillerBank(ajustes), ajustes)

    def test_recien_creado_no_hay_temporizador(self, tmp_path: Path) -> None:
        """El saludo inicial no debe llevar muletilla.

        El saludo es texto fijo y suena al instante: no hay espera que tapar. Y
        como el temporizador solo se arma cuando el usuario deja de hablar, al
        arrancar no puede haber ninguno pendiente.
        """
        assert self._procesador(tmp_path)._temporizador is None

    def test_no_suena_mientras_el_agente_habla(self, tmp_path: Path) -> None:
        proc = self._procesador(tmp_path)
        proc._bot_hablando = True
        assert proc._puede_sonar() is False

    def test_respeta_el_intervalo_minimo(self, tmp_path: Path) -> None:
        import time

        proc = self._procesador(tmp_path)
        proc._ultima_muletilla = time.monotonic()
        assert proc._puede_sonar() is False
