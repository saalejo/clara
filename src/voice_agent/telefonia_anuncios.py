"""Anuncio hablado de las llamadas entrantes.

Traduce los eventos del puente de telefonía en frames del pipeline, para que el
agente diga "te llama Ana, ¿respondo?" **sin que nadie le haya hablado antes**.
Es el único sitio del proyecto donde algo entra en la conversación desde fuera.

## Por qué dos frames, y en ese orden

```python
TTSSpeakFrame(text="Te llama Ana. ¿Respondo?", append_to_context=True)
LLMMessagesAppendFrame(messages=[{"role": "system", "content": "..."}])
```

**El primero es lo que se oye, y lleva texto fijo.** No se le pide al modelo que
redacte el aviso porque el anuncio es lo único del sistema con una restricción
dura de tiempo: un móvil suena unos veinticinco segundos. Una ida y vuelta a
el LLM se come un segundo largo, y además el modelo podría decidir hacer una
pregunta aclaratoria en vez de avisar — que es exactamente el bucle de saludos
que `bot.py` ya documenta para el saludo inicial.

`append_to_context=True` por el mismo motivo que el saludo: si no, el modelo no
se entera de que el agente ha hablado y la conversación se descoloca.

**El segundo es lo que el modelo necesita SABER**, y no lleva un `LLMRunFrame`
detrás. Ese detalle es la mitad menos evidente y la más valiosa: mete el aviso
en el historial *sin disparar un turno*. Así el agente no habla dos veces, pero
cuando la persona conteste "sí", el modelo ya sabe qué significa ese sí y qué
herramienta llamar.

## Solo frames de datos

Desde fuera del pipeline solo se encolan `DataFrame`s, y siempre con
`queue_frames`. Es la generalización de la lección que ya está en `hooks.py`:
un `SystemFrame` inyectado desde otra tarea se adelanta a la cola y descoloca
el estado del pipeline.

## El saludo al contestar, y lo que hay que entender de él

Cuando una llamada queda contestada, el agente saluda y sigue conversando **por
el micrófono de la sala, como siempre**. No hay ningún transporte nuevo: el
audio de la llamada no pasa por la tarjeta de sonido y no va a pasar hasta la
fase 2 de `docs/telefonia.md`.

Lo que hace que esto sirva de algo es **acoplamiento acústico**: con el móvil en
altavoz y cerca de la placa, el micro de la sala oye a quien llama y el altavoz
del agente le llega al micrófono del móvil. Es un puente por el aire.

Dos consecuencias que no son opcionales:

* **Sin altavoz en el móvil, el agente saluda a una habitación vacía** y quien
  llama no oye nada. Ponerlo en altavoz es parte del montaje, no un detalle.
* **El eco importa aquí más que en ningún otro sitio.** `docs/audio.md` ya midió
  que la propia voz del agente vuelve por el micro con confianza de VAD 0.956, y
  que `AlwaysUserMuteStrategy` no basta. Ahora se le suma el altavoz del móvil
  como segundo camino. La mitigación que el proyecto ya tiene es
  `AUDIO_PROFILE=speaker`, que es lo único que construye la compuerta de
  micrófono (`build_microphone_gate` devuelve `None` en perfil `headset`).

Y por qué el saludo no lo redacta el modelo: el mismo motivo que el anuncio.
Quien está al otro lado acaba de descolgar y espera oír algo **ya**; una ida y
vuelta al LLM son más de un segundo de silencio con alguien escuchando.
"""

from __future__ import annotations

import asyncio

from loguru import logger
from pipecat.frames.frames import LLMMessagesAppendFrame, TTSSpeakFrame
from pipecat.pipeline.worker import PipelineWorker

from voice_agent.audio_gate import MicrophoneGate
from voice_agent.telefonia import ClienteTelefonia, ErrorTelefonia
from voice_agent_core.telefonia import EventoTelefonia, TipoEvento

#: Espera inicial entre reintentos, en segundos. Se dobla en cada fallo hasta
#: el tope, para no llenar el log cuando el puente sencillamente no está.
ESPERA_INICIAL = 1.0
ESPERA_MAXIMA = 30.0


def _instruccion(evento: EventoTelefonia) -> str:
    """Redacta lo que el modelo necesita saber sobre la llamada entrante."""
    llamada = evento.llamada
    assert llamada is not None
    return (
        f"Está entrando una llamada de {llamada.quien}. Acabas de anunciarlo en voz alta. "
        "Si la persona te dice que la coja, usa contestar_llamada; si te dice que no, "
        "usa colgar_llamada. Si no dice nada de la llamada, sigue la conversación con "
        "normalidad y no vuelvas a mencionarla."
    )


def _instruccion_en_llamada(evento: EventoTelefonia) -> str:
    """Redacta lo que el modelo necesita saber al quedar contestada la llamada.

    Lo importante es **a quién le habla**, y la primera versión de este texto lo
    dejaba ambiguo. Decía que podía hablarle tanto quien llama como quien está en la
    habitación, y en la primera llamada real el modelo eligió lo segundo: contestó
    «Supongo que ya estás hablando por el móvil con Mamá Nora, ¿verdad?», tratando la
    llamada como algo que le estaban contando en vez de algo que estaba atendiendo.

    Así que ahora se fija el interlocutor por defecto —quien llama— y se prohíbe
    explícitamente ese comentario de más. Que alguien de la sala pueda colarse sigue
    siendo verdad, pero es la excepción y se cuenta como tal.
    """
    llamada = evento.llamada
    quien = llamada.quien if llamada is not None else "quien llama"
    return (
        f"Estás atendiendo una llamada telefónica de {quien} y acabas de saludar en voz "
        f"alta. A partir de ahora háblale a {quien} directamente, como si fueras tú quien "
        "sostiene el teléfono. NO comentes la llamada en tercera persona ni preguntes si "
        f"alguien más está hablando con {quien}: eres tú quien está en la llamada. "
        "Te oye por el altavoz del móvil, así que habla claro y con frases cortas. "
        f"Por defecto, asume que quien te habla es {quien}. Puede colarse alguien de la "
        "habitación; si lo que te dicen es una instrucción sobre la llamada —colgar, por "
        "ejemplo— atiéndela con colgar_llamada."
    )


def _instruccion_llamada_terminada(evento: EventoTelefonia) -> str:
    """Le dice al modelo que ya no está en una llamada.

    Sin esto seguiría creyéndose al teléfono el resto de la conversación, y
    trataría a quien está en la habitación como si fuera quien llamaba.
    """
    llamada = evento.llamada
    quien = llamada.quien if llamada is not None else "la otra persona"
    return (
        f"La llamada con {quien} ha terminado. Vuelves a hablar solo con quien está "
        "en la habitación. No lo anuncies si nadie pregunta."
    )


def _texto_con_quien(plantilla: str, evento: EventoTelefonia, respaldo: str) -> str:
    """Rellena `{quien}` en una plantilla, con red por si viene mal escrita.

    Una plantilla inválida escrita desde el panel no puede dejar mudo al agente en
    el momento en que más importa hablar.
    """
    llamada = evento.llamada
    quien = llamada.quien if llamada is not None else "alguien"
    try:
        return plantilla.format(quien=quien)
    except (KeyError, IndexError):
        logger.warning(f"La plantilla «{plantilla}» no es válida; se usa la de serie")
        return respaldo.format(quien=quien)


async def _saludar_en_llamada(worker: PipelineWorker, evento: EventoTelefonia, saludo: str) -> None:
    """Saluda al quedar contestada la llamada y avisa al modelo de dónde está.

    El saludo vacío desactiva la voz pero **no** el aviso al modelo: que sepa que
    está en una llamada es útil aunque no se le pida saludar.

    **Con las llamadas de app no se hace nada de esto**, y conviene saber por qué
    antes de "arreglarlo". El móvil declara esas llamadas `active` por HFP a los
    ~140 ms mientras la aplicación sigue timbrando, así que el evento de
    contestada es falso. Verificado con dos llamadas reales que nadie cogió: el
    agente saludó las dos veces, y quien llamaba no oyó nada porque la llamada
    aún no existía. Saludar ahí no es solo inútil —gasta el saludo antes de que
    haya nadie, y si luego alguien descuelga se encuentra el silencio—.
    """
    llamada = evento.llamada
    if llamada is not None and llamada.es_de_app:
        logger.info(
            "[telefonía] no saludo: es una llamada de aplicación y su estado "
            "'contestada' no es de fiar; ver `Llamada.es_de_app`"
        )
        return

    frames: list[LLMMessagesAppendFrame | TTSSpeakFrame] = []
    if saludo.strip():
        texto = _texto_con_quien(saludo, evento, "Hola, soy el asistente. ¿En qué puedo ayudarte?")
        logger.info(f"[telefonía] saludando en la llamada: {texto!r}")
        frames.append(TTSSpeakFrame(text=texto, append_to_context=True))
    frames.append(
        LLMMessagesAppendFrame(
            messages=[{"role": "system", "content": _instruccion_en_llamada(evento)}]
        )
    )
    await worker.queue_frames(frames)


async def _avisar_fin_de_llamada(worker: PipelineWorker, evento: EventoTelefonia) -> None:
    """Informa al modelo de que la llamada acabó, sin decir nada en voz alta.

    No lleva `TTSSpeakFrame` a propósito: anunciar cada cuelgue en voz alta molesta,
    y quien estaba en la llamada ya sabe que ha terminado.

    Sí lleva log, y por eso: al no decir nada en voz alta, sin una línea en el log no
    hay forma de saber si esto ha corrido. La primera prueba con una llamada real se
    quedó sin poder responder a esa pregunta.
    """
    logger.info("[telefonía] llamada terminada; el modelo vuelve a hablar con la sala")
    await worker.queue_frames(
        [
            LLMMessagesAppendFrame(
                messages=[{"role": "system", "content": _instruccion_llamada_terminada(evento)}]
            )
        ]
    )


async def _anunciar(worker: PipelineWorker, evento: EventoTelefonia, plantilla: str) -> None:
    """Mete el anuncio en el pipeline."""
    llamada = evento.llamada
    if llamada is None:
        return
    try:
        texto = plantilla.format(quien=llamada.quien)
    except (KeyError, IndexError):
        # Una plantilla mal escrita desde el panel no puede dejar sin avisar de
        # una llamada: se cae a un texto que siempre funciona.
        logger.warning(f"La plantilla de anuncio «{plantilla}» no es válida; se usa la de serie")
        texto = f"Te llama {llamada.quien}. ¿Respondo?"

    logger.info(f"[telefonía] anunciando llamada de {llamada.quien}")
    await worker.queue_frames(
        [
            TTSSpeakFrame(text=texto, append_to_context=True),
            LLMMessagesAppendFrame(messages=[{"role": "system", "content": _instruccion(evento)}]),
        ]
    )


async def escuchar_telefonia(
    worker: PipelineWorker,
    cliente: ClienteTelefonia,
    plantilla: str,
    saludo: str = "",
    gate: MicrophoneGate | None = None,
) -> None:
    """Sigue los eventos del puente, anuncia las entrantes y saluda al contestar.

    Args:
        worker: El worker de la sala, al que se encolan anuncios y avisos.
        cliente: El cliente del puente de telefonía.
        plantilla: El texto del anuncio de llamada entrante.
        saludo: Lo que se dice al contestarse una llamada; vacío calla la voz.
        gate: La compuerta del micrófono de la sala, si este perfil la tiene;
            se retiene mientras dura una llamada y se suelta al terminar.

    No deja escapar nunca una excepción hacia el pipeline: si el puente está
    caído, reintenta con retroceso exponencial y lo anota. Un agente sin
    teléfono tiene que seguir conversando, igual que un servidor MCP roto no
    impide arrancar.

    Args:
        worker: El worker en marcha, al que se le encolan los frames.
        cliente: Cliente del puente.
        plantilla: Texto del anuncio de llamada entrante, con el marcador `{quien}`.
        saludo: Texto que se dice al quedar contestada la llamada. Vacío no dice
            nada, pero el modelo se entera igual de que está en una llamada. Ver el
            docstring del módulo para lo que hace falta de montaje: el móvil tiene
            que estar en altavoz y cerca de la placa.
    """
    espera = ESPERA_INICIAL
    while True:
        try:
            async for evento in cliente.eventos():
                espera = ESPERA_INICIAL  # el canal va bien
                if evento.tipo is TipoEvento.LLAMADA_ENTRANTE:
                    await _anunciar(worker, evento, plantilla)
                elif evento.tipo is TipoEvento.LLAMADA_CONTESTADA:
                    # Con una llamada en curso, la voz de la sala es media
                    # conversación ajena: se retiene el micrófono hasta que
                    # termine. Vale también para las llamadas de app — su
                    # "contestada" es mentira, pero la persona está igual de
                    # ocupada con el teléfono.
                    if gate is not None:
                        gate.retener()
                    await _saludar_en_llamada(worker, evento, saludo)
                elif evento.tipo is TipoEvento.LLAMADA_TERMINADA:
                    if gate is not None:
                        gate.soltar()
                    await _avisar_fin_de_llamada(worker, evento)
                elif evento.tipo is TipoEvento.TELEFONO_CONECTADO:
                    logger.info(f"[telefonía] móvil conectado: {evento.motivo}")
                elif evento.tipo is TipoEvento.TELEFONO_DESCONECTADO:
                    # Sin móvil no hay llamada que retener; y si el retén se
                    # puso, soltarlo aquí evita una sala muda para siempre si
                    # el fin de la llamada se perdió con la conexión.
                    if gate is not None:
                        gate.soltar()
                    logger.info("[telefonía] móvil desconectado")
        except asyncio.CancelledError:
            raise
        except (ErrorTelefonia, Exception) as e:
            # El canal caído también suelta el retén: mejor un micrófono
            # abierto de más que una sala sorda sin nadie que la reabra.
            if gate is not None:
                gate.soltar()
            logger.info(f"[telefonía] canal de eventos caído ({e}); reintento en {espera:.0f} s")

        await asyncio.sleep(espera)
        espera = min(espera * 2, ESPERA_MAXIMA)


__all__ = ["ESPERA_INICIAL", "ESPERA_MAXIMA", "escuchar_telefonia"]
