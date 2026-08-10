"""Tests de la configuración.

Comprueban sobre todo las validaciones y las propiedades derivadas: son la
primera línea de defensa contra errores de configuración que, sin ellas, se
manifestarían como comportamientos raros a mitad de una conversación.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from voice_agent_core.config import AudioProfile, Settings


def _settings(**kwargs: Any) -> Settings:
    """Construye una configuración sin leer el fichero .env del proyecto."""
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


class TestValidaciones:
    def test_rechaza_modelos_whisper_solo_ingles(self) -> None:
        """El agente habla español; un modelo `.en` transcribiría fonéticamente."""
        for modelo in ("distil-medium.en", "medium.en", "base.en"):
            with pytest.raises(ValidationError, match="solo-inglés"):
                _settings(whisper_model=modelo)

    def test_acepta_modelos_multilingues(self) -> None:
        for modelo in ("tiny", "base", "small", "medium"):
            assert _settings(whisper_model=modelo).whisper_model == modelo

    def test_rechaza_frecuencias_que_silero_no_soporta(self) -> None:
        for rate in (22050, 44100, 48000):
            with pytest.raises(ValidationError, match="Silero"):
                _settings(audio_sample_rate=rate)

    def test_acepta_las_frecuencias_validas(self) -> None:
        assert _settings(audio_sample_rate=16000).audio_sample_rate == 16000
        assert _settings(audio_sample_rate=8000).audio_sample_rate == 8000

    def test_la_clave_de_gemini_falla_con_un_mensaje_util(self) -> None:
        s = _settings()
        with pytest.raises(ValueError, match=r"aistudio\.google\.com"):
            s.require_llm_api_key()

    def test_la_clave_de_groq_falla_con_un_mensaje_util(self) -> None:
        s = _settings(llm_backend="groq")
        with pytest.raises(ValueError, match=r"console\.groq\.com"):
            s.require_llm_api_key()

    def test_devuelve_la_clave_cuando_esta_configurada(self) -> None:
        s = _settings(gemini_api_key="AIza-ejemplo")
        assert s.require_llm_api_key() == "AIza-ejemplo"

    def test_devuelve_la_clave_de_groq_segun_el_backend(self) -> None:
        s = _settings(llm_backend="groq", groq_api_key="gsk-ejemplo")
        assert s.require_llm_api_key() == "gsk-ejemplo"

    def test_el_modelo_efectivo_sigue_al_backend(self) -> None:
        assert _settings().llm_model_efectivo == _settings().gemini_model
        s = _settings(llm_backend="groq")
        assert s.llm_model_efectivo == s.groq_model

    def test_la_clave_no_se_filtra_al_representar_la_configuracion(self) -> None:
        """`SecretStr` evita que la clave aparezca en logs o trazas."""
        s = _settings(gemini_api_key="AIza-secretisima")
        assert "secretisima" not in repr(s)
        assert "secretisima" not in str(s)


class TestPerfilesDeAudio:
    def test_headset_permite_interrumpir(self) -> None:
        assert _settings(audio_profile=AudioProfile.HEADSET).allow_interruptions is True

    def test_altavoz_no_permite_interrumpir(self) -> None:
        """Con altavoz abierto el agente se interrumpiría a sí mismo."""
        assert _settings(audio_profile=AudioProfile.SPEAKER).allow_interruptions is False

    def test_el_altavoz_usa_un_vad_mas_conservador(self) -> None:
        """Umbrales más altos para que el eco de la sala no dispare el detector."""
        headset = _settings(audio_profile=AudioProfile.HEADSET)
        altavoz = _settings(audio_profile=AudioProfile.SPEAKER)

        assert altavoz.effective_vad_confidence > headset.effective_vad_confidence
        assert altavoz.effective_vad_start_secs > headset.effective_vad_start_secs
        assert altavoz.effective_vad_stop_secs > headset.effective_vad_stop_secs

    def test_los_valores_explicitos_ganan_al_perfil(self) -> None:
        s = _settings(audio_profile=AudioProfile.SPEAKER, vad_confidence=0.5)
        assert s.effective_vad_confidence == 0.5


class TestRutasDerivadas:
    def test_las_rutas_cuelgan_de_data_dir(self, tmp_path: Path) -> None:
        s = _settings(data_dir=tmp_path / "datos")
        assert s.models_dir == tmp_path / "datos" / "models"
        assert s.chroma_dir == tmp_path / "datos" / "chroma"
        assert s.piper_dir == tmp_path / "datos" / "models" / "piper"

    def test_ensure_directories_las_crea(self, tmp_path: Path) -> None:
        s = _settings(data_dir=tmp_path / "datos")
        s.ensure_directories()
        assert s.models_dir.is_dir()
        assert s.chroma_dir.is_dir()
        assert s.piper_dir.is_dir()
