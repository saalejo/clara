"""La API del puente, servida por un socket unix.

## Por qué un socket unix y no un puerto

El contenedor del agente **ya monta** `<PROYECTO>/data` en `/data`, así que un
socket en `data/run/telefonia.sock` se ve dentro sin tocar una sola línea de
`deploy/voice-agent.container`. Comprobado en la placa antes de escribir nada.

Además, un socket unix puede transportar un **descriptor de fichero** por
`SCM_RIGHTS`. La fase 2 (audio SCO) recibe el socket de audio de oFono como
descriptor, así que esta elección convierte esa fase en "añadir un endpoint" en
vez de "cambiar de transporte". HTTP sobre loopback no puede hacer eso.

Y no hay trampa de uid: el agente corre **sin** `--userns=keep-id`, de modo que
dentro es uid 0, que en rootless mapea al 1000 del anfitrión; un socket de
`ember` se ve como de root ahí dentro y `connect()` funciona. Aquí no hay
autenticación EXTERNAL que contrastar, que es justo la trampa que documenta
`packages/panel/src/voice_agent_panel/control.py`.

## Los códigos de estado, y por qué importan

Una llamada puede desaparecer entre que el modelo decide colgarla y que la
petición llega: el otro ha colgado antes. Eso es **normal**, no un error del
servidor, y por eso da `404` y nunca `500`. La regla general es que el agente
debe poder distinguir "no hay teléfono" (409) de "esa llamada ya no existe"
(404) de "el puente aún está arrancando" (503), porque cada una se le cuenta al
usuario de una manera distinta.

## El id `actual`

Las rutas de una llamada aceptan el id literal **`actual`**, que significa "la
llamada de ahora, la que sea". Es el contrato que los clientes ya daban por
supuesto —`src/voice_agent/telefonia.py` lo escribe a fuego— pero que **no estaba
implementado**: `Telefono._buscar` busca por id exacto, `actual` no es el id de
ninguna llamada y la respuesta era siempre un 404.

Se descubrió probando el mando físico con una llamada de verdad: el botón de
contestar pedía `POST /llamadas/actual/contestar` y el puente lo rechazaba con un
404. Y con ello estaba roto también **`contestar_llamada` del agente**, que usa la
misma ruta: pedirle en voz alta que cogiera el teléfono no podía funcionar.

Los otros dos se salvaban por caminos distintos, y merece la pena saber por qué
para no "arreglarlos" de más: **colgar** porque su cliente sin id llama a
`/llamadas/colgar-todas`, y **los tonos** porque su manejador no mira el id de la
ruta — `enviar_tonos` va al módem, no a una llamada concreta.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from voice_agent_telefonia import __version__
from voice_agent_telefonia.contactos import hay_ganador_claro, redactar_pregunta
from voice_agent_telefonia.llamadas import ErrorTelefono, SinTelefono
from voice_agent_telefonia.servicio import Servicio

#: Cada cuánto se manda un comentario por el canal de eventos. Sirve para que
#: un agente detecte pronto que el puente se ha caído, en vez de quedarse
#: esperando en una conexión muerta.
LATIDO_SEGUNDOS = 15.0


#: Id de llamada que significa «la de ahora, la que sea». Ver el docstring del
#: módulo: es el contrato que los clientes ya asumían y que faltaba implementar.
ID_ACTUAL = "actual"


def _id_de(peticion: Request) -> str | None:
    """Saca el id de llamada de la ruta, traduciendo `actual` a «la que haya».

    `Telefono._buscar` interpreta `None` como «la única llamada viva», que es
    exactamente lo que quiere decir `actual`. Sin esta traducción buscaría una
    llamada cuyo id fuese la cadena "actual" y devolvería 404 siempre.
    """
    crudo = peticion.path_params.get("id_llamada") or None
    return None if crudo == ID_ACTUAL else crudo


def _error(mensaje: str, codigo: int) -> JSONResponse:
    return JSONResponse({"error": mensaje}, status_code=codigo)


def crear_app(servicio: Servicio) -> Starlette:
    """Monta la aplicación sobre un servicio ya construido.

    Recibe el servicio en vez de crearlo para que los tests puedan pasar uno
    falso y ejercitar la API entera sin bus, sin móvil y sin red.
    """

    async def salud(_: Request) -> JSONResponse:
        """Comprobación barata: es lo que el agente sondea al arrancar."""
        return JSONResponse({"ok": True, "version": __version__})

    async def estado(_: Request) -> JSONResponse:
        return JSONResponse(json.loads((await servicio.estado()).model_dump_json()))

    async def llamadas(_: Request) -> JSONResponse:
        if servicio.telefono is None or not servicio.telefono.listo:
            return _error("no hay ningún teléfono conectado", 409)
        try:
            actuales = [ll for ll in await servicio.telefono.listar() if ll.viva]
        except SinTelefono as e:
            return _error(str(e), 409)
        except ErrorTelefono as e:
            return _error(str(e), 502)
        for llamada in actuales:
            servicio.poner_nombre(llamada)
        return JSONResponse({"llamadas": [json.loads(ll.model_dump_json()) for ll in actuales]})

    async def marcar(peticion: Request) -> JSONResponse:
        try:
            datos = await peticion.json()
        except (ValueError, TypeError):
            return _error("el cuerpo tiene que ser JSON", 422)
        numero = str(datos.get("numero", "")).strip()
        if not numero:
            return _error("falta el número", 422)
        if servicio.telefono is None:
            return _error("no hay ningún teléfono conectado", 409)
        try:
            llamada = await servicio.telefono.marcar(numero)
        except SinTelefono as e:
            return _error(str(e), 409)
        except ErrorTelefono as e:
            return _error(str(e), 502)
        return JSONResponse(json.loads(llamada.model_dump_json()))

    async def autocontestar(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "activo": servicio.preferencias.autocontestar,
                "segundos": servicio.preferencias.autocontestar_segundos,
            }
        )

    async def fijar_autocontestar(peticion: Request) -> JSONResponse:
        """Cambia el autocontestar. Acepta `{"activo": bool}` o `{"alternar": true}`.

        No devuelve 409 sin teléfono conectado, y es deliberado: la preferencia
        tiene sentido sin móvil delante —la dejas puesta y luego llega—, y un 409
        obligaría al mando físico a comprobar antes si hay teléfono para poder
        pulsar un botón que no depende de eso.
        """
        try:
            datos = await peticion.json()
        except (ValueError, TypeError):
            return _error("el cuerpo tiene que ser JSON", 422)

        if datos.get("alternar"):
            activo = servicio.alternar_autocontestar()
        elif isinstance(datos.get("activo"), bool):
            activo = servicio.fijar_autocontestar(bool(datos["activo"]))
        else:
            return _error("manda {'activo': true|false} o {'alternar': true}", 422)

        return JSONResponse(
            {"activo": activo, "segundos": servicio.preferencias.autocontestar_segundos}
        )

    async def contestar(peticion: Request) -> JSONResponse:
        if servicio.telefono is None:
            return _error("no hay ningún teléfono conectado", 409)
        id_llamada = _id_de(peticion)
        try:
            llamada = await servicio.telefono.contestar(id_llamada)
        except SinTelefono as e:
            return _error(str(e), 409)
        except ErrorTelefono as e:
            # "ya no existe" es una carrera normal, no un fallo del servidor.
            return _error(str(e), 404 if "ya no existe" in str(e) else 502)
        return JSONResponse(json.loads(llamada.model_dump_json()))

    async def colgar(peticion: Request) -> JSONResponse:
        if servicio.telefono is None:
            return _error("no hay ningún teléfono conectado", 409)
        id_llamada = _id_de(peticion)
        try:
            await servicio.telefono.colgar(id_llamada)
        except SinTelefono as e:
            return _error(str(e), 409)
        except ErrorTelefono as e:
            return _error(str(e), 404 if "ya no existe" in str(e) else 502)
        return JSONResponse({"ok": True})

    async def colgar_todas(_: Request) -> JSONResponse:
        if servicio.telefono is None:
            return _error("no hay ningún teléfono conectado", 409)
        try:
            await servicio.telefono.colgar_todas()
        except SinTelefono as e:
            return _error(str(e), 409)
        except ErrorTelefono as e:
            return _error(str(e), 502)
        return JSONResponse({"ok": True})

    async def tonos(peticion: Request) -> JSONResponse:
        if servicio.telefono is None:
            return _error("no hay ningún teléfono conectado", 409)
        try:
            datos = await peticion.json()
        except (ValueError, TypeError):
            return _error("el cuerpo tiene que ser JSON", 422)
        try:
            await servicio.telefono.enviar_tonos(str(datos.get("tonos", "")))
        except SinTelefono as e:
            return _error(str(e), 409)
        except ErrorTelefono as e:
            return _error(str(e), 422)
        return JSONResponse({"ok": True})

    async def contactos(peticion: Request) -> JSONResponse:
        """Busca en la agenda y decide si hay un ganador claro.

        La decisión se toma **aquí** y no en el agente para que la regla viva
        en un solo sitio y se pueda probar. El agente solo lee `ambiguo`.
        """
        nombre = peticion.query_params.get("nombre", "")
        limite = int(peticion.query_params.get("limite", "5"))
        coincidencias = servicio.agenda.buscar(nombre, limite=limite)
        claro = hay_ganador_claro(coincidencias)
        return JSONResponse(
            {
                "coincidencias": [json.loads(c.model_dump_json()) for c in coincidencias],
                "ambiguo": bool(coincidencias) and not claro,
                "pregunta": "" if claro or not coincidencias else redactar_pregunta(coincidencias),
            }
        )

    async def sincronizar(_: Request) -> JSONResponse:
        resultado = await servicio.sincronizar_agenda(forzar=True)
        return JSONResponse(json.loads(resultado.model_dump_json()))

    async def eventos(_: Request) -> StreamingResponse:
        """Canal SSE con los eventos del teléfono."""

        async def flujo() -> AsyncIterator[bytes]:
            # Se consume la COLA, no un generador asíncrono: `wait_for` cancela
            # lo que espera, y cancelar un `__anext__` finaliza el generador.
            # Ver `BusDeEventos.suscripcion`.
            with servicio.eventos.suscripcion() as cola:
                while True:
                    try:
                        evento = await asyncio.wait_for(cola.get(), timeout=LATIDO_SEGUNDOS)
                    except TimeoutError:
                        # Comentario SSE: mantiene viva la conexión y permite
                        # detectar pronto que el puente se ha caído.
                        yield b": ping\n\n"
                        continue
                    yield f"data: {evento.model_dump_json()}\n\n".encode()

        return StreamingResponse(
            flujo(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @contextlib.asynccontextmanager
    async def ciclo_de_vida(_: Starlette) -> AsyncIterator[None]:
        """Arranca y para el servicio dentro del bucle de eventos de uvicorn.

        Va como `lifespan` y no como `on_startup=`/`on_shutdown=`: Starlette 1.x
        retiró esos argumentos y pasarlos revienta con un `TypeError` en el
        constructor, antes de que nada llegue a arrancar.

        Y tiene que ser aquí y no en `main()`: si el servicio se conectara a
        D-Bus en otro bucle de eventos, la primera señal que llegara fallaría
        con un "attached to a different loop".
        """
        await servicio.arrancar()
        try:
            yield
        finally:
            await servicio.parar()

    rutas = [
        Route("/salud", salud),
        Route("/estado", estado),
        Route("/llamadas", llamadas),
        Route("/llamadas/marcar", marcar, methods=["POST"]),
        Route("/llamadas/colgar-todas", colgar_todas, methods=["POST"]),
        Route("/llamadas/{id_llamada}/contestar", contestar, methods=["POST"]),
        Route("/llamadas/{id_llamada}/colgar", colgar, methods=["POST"]),
        Route("/llamadas/{id_llamada}/tonos", tonos, methods=["POST"]),
        Route("/autocontestar", autocontestar),
        Route("/autocontestar", fijar_autocontestar, methods=["POST"]),
        Route("/contactos", contactos),
        Route("/contactos/sincronizar", sincronizar, methods=["POST"]),
        Route("/eventos", eventos),
    ]
    return Starlette(routes=rutas, lifespan=ciclo_de_vida)


__all__ = ["LATIDO_SEGUNDOS", "crear_app"]
