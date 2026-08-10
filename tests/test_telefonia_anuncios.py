"""El anuncio de llamada entrante que entra en el pipeline desde fuera.

Lo que se fija aquí es la forma exacta de los dos frames y su orden. No es
cosmética: el `TTSSpeakFrame` con texto fijo es lo que garantiza que el aviso
suene dentro de los ~25 segundos que dura el timbre, y el
`LLMMessagesAppendFrame` **sin** `LLMRunFrame` detrás es lo que hace que el
modelo entienda el "sí" siguiente sin ponerse a hablar dos veces.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from pipecat.frames.frames import (
    DataFrame,
    LLMMessagesAppendFrame,
    LLMRunFrame,
    SystemFrame,
    TTSSpeakFrame,
)

from voice_agent.telefonia_anuncios import (
    _anunciar,
    _avisar_fin_de_llamada,
    _saludar_en_llamada,
)
from voice_agent_core.telefonia import (
    EstadoLlamada,
    EventoTelefonia,
    Llamada,
    TipoEvento,
)


class WorkerFalso:
    """Solo apunta lo que le encolan."""

    def __init__(self) -> None:
        self.frames: list[Any] = []

    async def queue_frames(self, frames: list[Any]) -> None:
        self.frames.extend(frames)


def evento_entrante(quien: str = "Ana Pérez") -> EventoTelefonia:
    return EventoTelefonia(
        tipo=TipoEvento.LLAMADA_ENTRANTE,
        momento=datetime.now(UTC),
        llamada=Llamada(
            id="voicecall01",
            estado=EstadoLlamada.ENTRANTE,
            numero="+573001111111",
            nombre_agenda=quien,
            entrante=True,
        ),
    )


class TestFormaDelAnuncio:
    async def test_encola_exactamente_dos_frames_en_orden(self) -> None:
        worker = WorkerFalso()
        await _anunciar(cast(Any, worker), evento_entrante(), "Te llama {quien}. ¿Respondo?")

        assert len(worker.frames) == 2
        assert isinstance(worker.frames[0], TTSSpeakFrame)
        assert isinstance(worker.frames[1], LLMMessagesAppendFrame)

    async def test_el_texto_usa_el_nombre_de_la_agenda(self) -> None:
        worker = WorkerFalso()
        await _anunciar(cast(Any, worker), evento_entrante("Mamá"), "Te llama {quien}. ¿Respondo?")
        assert worker.frames[0].text == "Te llama Mamá. ¿Respondo?"

    async def test_queda_constancia_en_el_historial(self) -> None:
        """Sin `append_to_context`, el modelo no sabe que el agente ha hablado
        y la conversación se descoloca."""
        worker = WorkerFalso()
        await _anunciar(cast(Any, worker), evento_entrante(), "Te llama {quien}. ¿Respondo?")
        assert worker.frames[0].append_to_context is True

    async def test_el_aviso_al_modelo_nombra_las_herramientas(self) -> None:
        worker = WorkerFalso()
        await _anunciar(cast(Any, worker), evento_entrante(), "Te llama {quien}. ¿Respondo?")
        contenido = worker.frames[1].messages[0]["content"]
        assert "contestar_llamada" in contenido
        assert "colgar_llamada" in contenido

    async def test_no_se_dispara_un_turno_del_modelo(self) -> None:
        """Un `LLMRunFrame` haría que el agente hablara dos veces seguidas."""
        worker = WorkerFalso()
        await _anunciar(cast(Any, worker), evento_entrante(), "Te llama {quien}. ¿Respondo?")
        assert not any(isinstance(f, LLMRunFrame) for f in worker.frames)

    async def test_solo_frames_de_datos(self) -> None:
        """Desde fuera del pipeline, un `SystemFrame` se adelanta a la cola y
        descoloca el estado. Es la lección que ya está en hooks.py."""
        worker = WorkerFalso()
        await _anunciar(cast(Any, worker), evento_entrante(), "Te llama {quien}. ¿Respondo?")
        for frame in worker.frames:
            assert isinstance(frame, DataFrame)
            assert not isinstance(frame, SystemFrame)


class TestRobustez:
    async def test_una_plantilla_rota_no_deja_sin_avisar(self) -> None:
        """Se puede escribir desde el panel; un error ahí no puede hacer que se
        pierda una llamada."""
        worker = WorkerFalso()
        await _anunciar(cast(Any, worker), evento_entrante("Ana"), "Te llama {nombre_malo}")

        assert len(worker.frames) == 2
        assert "Ana" in worker.frames[0].text

    async def test_un_evento_sin_llamada_no_encola_nada(self) -> None:
        worker = WorkerFalso()
        evento = EventoTelefonia(tipo=TipoEvento.LLAMADA_ENTRANTE, momento=datetime.now(UTC))
        await _anunciar(cast(Any, worker), evento, "Te llama {quien}")
        assert worker.frames == []

    async def test_numero_desconocido(self) -> None:
        """Sin nombre en la agenda se dice el número, no una cadena vacía."""
        worker = WorkerFalso()
        evento = EventoTelefonia(
            tipo=TipoEvento.LLAMADA_ENTRANTE,
            momento=datetime.now(UTC),
            llamada=Llamada(
                id="voicecall01",
                estado=EstadoLlamada.ENTRANTE,
                numero="+573009999999",
                entrante=True,
            ),
        )
        await _anunciar(cast(Any, worker), evento, "Te llama {quien}. ¿Respondo?")
        assert "+573009999999" in worker.frames[0].text


# --- El saludo al quedar contestada la llamada --------------------------------
#
# Existe porque, sin transporte SCO, la única forma de que el agente participe en
# una llamada es el acoplamiento acústico: móvil en altavoz junto a la placa, el
# micro de la sala oyendo a quien llama y el altavoz del agente llegándole al
# micrófono del móvil. Ver el docstring de `telefonia_anuncios`.


def evento_contestada(quien: str = "Ana Pérez") -> EventoTelefonia:
    return EventoTelefonia(
        tipo=TipoEvento.LLAMADA_CONTESTADA,
        momento=datetime.now(UTC),
        llamada=Llamada(
            id="voicecall01",
            estado=EstadoLlamada.EN_CURSO,
            numero="3001234567",
            nombre_agenda=quien,
            entrante=True,
        ),
    )


def evento_terminada(quien: str = "Ana Pérez") -> EventoTelefonia:
    return EventoTelefonia(
        tipo=TipoEvento.LLAMADA_TERMINADA,
        momento=datetime.now(UTC),
        llamada=Llamada(
            id="voicecall01",
            estado=EstadoLlamada.TERMINADA,
            numero="3001234567",
            nombre_agenda=quien,
            entrante=True,
        ),
    )


class TestSaludoEnLlamada:
    async def test_saluda_y_avisa_al_modelo(self) -> None:
        worker = WorkerFalso()
        await _saludar_en_llamada(cast(Any, worker), evento_contestada(), "Hola, dime.")

        assert len(worker.frames) == 2
        assert isinstance(worker.frames[0], TTSSpeakFrame)
        assert worker.frames[0].text == "Hola, dime."
        assert isinstance(worker.frames[1], LLMMessagesAppendFrame)

    async def test_el_saludo_admite_el_nombre_de_quien_llama(self) -> None:
        worker = WorkerFalso()
        await _saludar_en_llamada(cast(Any, worker), evento_contestada("Mamá"), "Hola {quien}.")
        assert worker.frames[0].text == "Hola Mamá."

    async def test_una_plantilla_invalida_no_deja_mudo_al_agente(self) -> None:
        """Alguien acaba de descolgar: el silencio es lo peor que puede pasar."""
        worker = WorkerFalso()
        await _saludar_en_llamada(cast(Any, worker), evento_contestada(), "Hola {no_existe}.")
        assert isinstance(worker.frames[0], TTSSpeakFrame)
        assert worker.frames[0].text

    async def test_queda_constancia_en_el_historial(self) -> None:
        worker = WorkerFalso()
        await _saludar_en_llamada(cast(Any, worker), evento_contestada(), "Hola.")
        assert worker.frames[0].append_to_context is True

    async def test_un_saludo_vacio_no_habla_pero_si_avisa(self) -> None:
        """Que el modelo sepa dónde está es útil aunque no se le pida saludar."""
        worker = WorkerFalso()
        await _saludar_en_llamada(cast(Any, worker), evento_contestada(), "")

        assert len(worker.frames) == 1
        assert isinstance(worker.frames[0], LLMMessagesAppendFrame)

    async def test_el_aviso_explica_por_donde_le_oyen(self) -> None:
        """El modelo tiene que saber que le oyen por el altavoz del móvil."""
        worker = WorkerFalso()
        await _saludar_en_llamada(cast(Any, worker), evento_contestada(), "Hola.")
        contenido = worker.frames[-1].messages[0]["content"]
        assert "altavoz" in contenido
        assert "colgar_llamada" in contenido


class TestNoSaludaEnLlamadasDeApp:
    """Su «contestada» es mentira, así que saludar es hablarle al vacío.

    Medido con dos llamadas de WhatsApp que se dejaron sonar sin cogerlas: el
    móvil las declaró `active` por HFP a los ~140 ms, el agente saludó las dos
    veces, y quien llamaba no oyó nada porque no había llamada. Ver
    `Llamada.es_de_app`.
    """

    async def test_una_llamada_de_app_no_recibe_saludo_ni_aviso(self) -> None:
        worker = WorkerFalso()
        evento = EventoTelefonia(
            tipo=TipoEvento.LLAMADA_CONTESTADA,
            momento=datetime.now(UTC),
            llamada=Llamada(
                id="voicecall01",
                estado=EstadoLlamada.EN_CURSO,
                numero="10000000",
                entrante=True,
            ),
        )

        await _saludar_en_llamada(cast(Any, worker), evento, "Hola, dime.")

        assert worker.frames == []

    async def test_una_del_operador_sigue_recibiendo_el_saludo(self) -> None:
        """El contrapeso: con número de verdad el «contestada» sí es fiable."""
        worker = WorkerFalso()

        await _saludar_en_llamada(cast(Any, worker), evento_contestada(), "Hola, dime.")

        assert len(worker.frames) == 2

    async def test_el_aviso_fija_a_quien_le_habla(self) -> None:
        """Regresión de la primera llamada real.

        El texto original decía que podía hablarle tanto quien llama como quien está
        en la habitación, y el modelo eligió lo segundo: contestó «Supongo que ya
        estás hablando por el móvil con Mamá Nora, ¿verdad?», tratando la llamada
        como algo que le estaban contando en vez de algo que estaba atendiendo.

        El interlocutor por defecto tiene que quedar fijado, y por nombre.
        """
        worker = WorkerFalso()
        await _saludar_en_llamada(cast(Any, worker), evento_contestada("Mamá Nora"), "Hola.")
        contenido = worker.frames[-1].messages[0]["content"]

        assert "háblale a Mamá Nora directamente" in contenido
        assert "asume que quien te habla es Mamá Nora" in contenido
        # Y la prohibición explícita de lo que hizo la primera vez.
        assert "tercera persona" in contenido

    async def test_no_se_dispara_un_turno_del_modelo(self) -> None:
        worker = WorkerFalso()
        await _saludar_en_llamada(cast(Any, worker), evento_contestada(), "Hola.")
        assert not any(isinstance(f, LLMRunFrame) for f in worker.frames)

    async def test_solo_frames_de_datos(self) -> None:
        worker = WorkerFalso()
        await _saludar_en_llamada(cast(Any, worker), evento_contestada(), "Hola.")
        assert all(isinstance(f, DataFrame) for f in worker.frames)
        assert not any(isinstance(f, SystemFrame) for f in worker.frames)


class TestFinDeLlamada:
    async def test_avisa_al_modelo_sin_decir_nada_en_voz_alta(self) -> None:
        """Anunciar cada cuelgue molesta, y quien colgó ya lo sabe."""
        worker = WorkerFalso()
        await _avisar_fin_de_llamada(cast(Any, worker), evento_terminada())

        assert len(worker.frames) == 1
        assert isinstance(worker.frames[0], LLMMessagesAppendFrame)
        assert not any(isinstance(f, TTSSpeakFrame) for f in worker.frames)

    async def test_le_dice_que_vuelve_a_hablar_con_la_habitacion(self) -> None:
        """Sin esto seguiría creyéndose al teléfono el resto de la conversación."""
        worker = WorkerFalso()
        await _avisar_fin_de_llamada(cast(Any, worker), evento_terminada())
        contenido = worker.frames[0].messages[0]["content"]
        assert "terminado" in contenido
        assert "habitación" in contenido

    async def test_no_dispara_un_turno(self) -> None:
        worker = WorkerFalso()
        await _avisar_fin_de_llamada(cast(Any, worker), evento_terminada())
        assert not any(isinstance(f, LLMRunFrame) for f in worker.frames)
