"""Control de llamadas sobre `org.ofono`, y traducción de sus estados.

La placa es la **unidad manos libres** y el móvil la **pasarela de audio**. Todo
lo que aquí parece telefonía son en realidad comandos AT viajando por RFCOMM:
oFono los traduce a una API de D-Bus decente y este módulo la envuelve.

## Lo que hay que saber del módem HFP de oFono

No hay que poner `Online=true`. El driver `hfp` no define `set_online` y crea
todos sus átomos en `pre_sim`: en cuanto se establece el enlace HFP, `Powered`
pasa a `true` por su cuenta y `org.ofono.VoiceCallManager` ya está en la ruta
del módem. Las guías que dicen lo contrario hablan de módems GSM de verdad.

## Contestar no siempre es `Answer()`

Con una sola llamada entrante se usa `VoiceCall.Answer()`. Pero si ya hay una
conversación en marcha y entra otra —que oFono deja en estado `waiting`—, lo
correcto es `VoiceCallManager.HoldAndAnswer()`, que pone la primera en espera.
Llamar a `Answer()` sobre una llamada en `waiting` falla.

Esa distinción la resuelve `contestar()` mirando el estado, **no el modelo de
lenguaje**: es una regla de telefonía, no una decisión de conversación, y
pedirle al LLM que la aprenda solo serviría para que se equivocara.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from dbus_fast import Message
from dbus_fast.aio import MessageBus
from loguru import logger

from voice_agent_core.telefonia import (
    ESTADOS_CONTESTABLES,
    ESTADOS_OFONO,
    NUMERO_OCULTO,
    RELLENOS_SIN_IDENTIFICAR,
    EstadoLlamada,
    Llamada,
)
from voice_agent_telefonia.bus import ErrorBus, desenvolver, escuchar, llamar

SERVICIO = "org.ofono"
IFAZ_MANAGER = "org.ofono.Manager"
IFAZ_MODEM = "org.ofono.Modem"
IFAZ_GESTOR_LLAMADAS = "org.ofono.VoiceCallManager"
IFAZ_LLAMADA = "org.ofono.VoiceCall"

#: Lo que oFono pone en `LineIdentification` cuando el que llama oculta su
#: número. Sin traducirlo, el agente diría "te llama withheld".
OCULTO_OFONO = "withheld"

#: El ÚNICO estado inicial que delata con seguridad una llamada saliente.
#:
#: `alerting` también significa "saliente, sonando al otro lado" según el
#: estándar de HFP, y así estaba escrito aquí al principio. Medido contra un
#: TECNO POVA 5 Pro, resultó ser falso: ese móvil entrega las llamadas
#: **entrantes** como `alerting` y con un identificador de llamada de relleno
#: (`10000000`), no como `incoming` con CLIP. El resultado era que el agente
#: anunciaba "llamada saliente" cada vez que alguien te llamaba.
#:
#: La comparación que lo demostró, las dos capturadas en la misma placa:
#:
#: | | Saliente de verdad (marcada por el puente) | La supuesta "saliente" |
#: |---|---|---|
#: | Estado inicial | `dialing` | `alerting` |
#: | Número | el marcado, correcto | `10000000` |
#: | Paso a `active` | al descolgar | 57 ms |
#:
#: Así que `alerting` se trata como entrante. El precio de equivocarse es
#: asimétrico: dar por entrante una llamada que marcaste tú desde el propio
#: móvil solo provoca un anuncio de más, mientras que dar por saliente una
#: entrante hace que el agente **nunca** avise de que te están llamando, que es
#: justo la función principal.
_ESTADOS_SALIENTES = frozenset({EstadoLlamada.MARCANDO})


def _parece_entrante(estado: EstadoLlamada, numero: str) -> bool:
    """Deduce la dirección de una llamada de su estado inicial y su número.

    `MARCANDO` (`dialing`) tampoco basta por sí solo, y eso costó una segunda
    llamada de prueba. Medido con el mismo TECNO POVA 5 Pro: una llamada de
    **WhatsApp entrante** apareció en `dialing`, no en `alerting` como decía la
    tabla de `docs/telefonia.md`. El puente la dio por saliente y **no la
    anunció**: quien llamaba oyó el saludo, pero en la habitación nadie se
    enteró de que estaba entrando una llamada.

    Lo que las separa es el identificador. Una saliente de verdad marcada en el
    móvil lleva el número al que se llamó; una de app lleva el relleno de
    Android (`RELLENOS_SIN_IDENTIFICAR`), porque el identificador de llamada de
    HFP solo entiende de números de teléfono. Así que `dialing` **con relleno**
    es una llamada de app que entra.

    El caso que esto se equivoca es una llamada de app **saliente** marcada en el
    móvil, que también lleva relleno y se anunciará como entrante. Se acepta a
    propósito, por la misma asimetría que ya justifica `_ESTADOS_SALIENTES`: un
    anuncio de más es una molestia, y un anuncio de menos es el agente callado
    mientras alguien te llama, que es su función principal.

    Args:
        estado: El estado inicial traducido, tal y como llegó en `CallAdded`.
        numero: El `LineIdentification`, ya normalizado.

    Returns:
        `True` si hay que tratarla como entrante.
    """
    if estado not in _ESTADOS_SALIENTES:
        return True
    return numero in RELLENOS_SIN_IDENTIFICAR


def estado_legible(estado_ofono: str) -> EstadoLlamada:
    """Traduce el `State` de oFono al del contrato.

    Un valor desconocido se trata como terminada y se registra: es preferible
    dar una llamada por acabada —el agente dejará de ofrecerse a contestarla—
    que quedarse con una llamada fantasma en un estado que nadie entiende.
    """
    traducido = ESTADOS_OFONO.get(estado_ofono)
    if traducido is None:
        logger.warning(f"oFono ha devuelto un estado de llamada desconocido: {estado_ofono!r}")
        return EstadoLlamada.TERMINADA
    return traducido


def id_de_ruta(ruta: str) -> str:
    """Extrae el identificador corto de una ruta de llamada de oFono.

    De `/hfp/org/bluez/hci0/dev_.../voicecall01` sale `voicecall01`. Se usa ese
    y no la ruta entera porque viaja por una URL y porque la ruta completa lleva
    dentro la MAC del móvil, que no tiene por qué salir del puente.
    """
    return ruta.rsplit("/", 1)[-1]


def llamada_desde_propiedades(ruta: str, propiedades: dict[str, Any]) -> Llamada:
    """Construye una `Llamada` a partir de lo que devuelve oFono.

    Es una **función pura**: ni toca el bus ni depende de nada externo. Ese
    corte es lo que permite probar el análisis de los mensajes de oFono con
    payloads grabados, sin móvil y sin bus.

    Args:
        ruta: Ruta D-Bus del objeto `org.ofono.VoiceCall`.
        propiedades: Sus propiedades, ya desenvueltas.
    """
    props = desenvolver(propiedades)
    estado = estado_legible(str(props.get("State", "")))

    numero = str(props.get("LineIdentification", "") or "")
    if numero == OCULTO_OFONO:
        numero = NUMERO_OCULTO

    inicio: datetime | None = None
    if marca := props.get("StartTime"):
        try:
            inicio = datetime.fromisoformat(str(marca).replace("Z", "+00:00"))
        except ValueError:
            inicio = None

    return Llamada(
        id=id_de_ruta(ruta),
        estado=estado,
        numero=numero,
        nombre_red=str(props.get("Name", "") or ""),
        # oFono no marca la dirección, así que se deduce del estado inicial y
        # del identificador. Ver `_parece_entrante`; el puente además pisa esto
        # con lo que sabe de lo que marcó él mismo.
        entrante=_parece_entrante(estado, numero),
        inicio=inicio,
    )


class ErrorTelefono(Exception):
    """No se pudo hacer la operación sobre el teléfono."""


class SinTelefono(ErrorTelefono):
    """No hay ningún móvil conectado con módem HFP listo."""


class Telefono:
    """El móvil conectado, visto como algo sobre lo que se puede actuar.

    Mantiene la ruta del módem al día siguiendo las señales de oFono, de modo
    que alejarse con el móvil y volver no obliga a reiniciar nada.
    """

    def __init__(self, bus: MessageBus) -> None:
        """Prepara el control de llamadas sobre el bus del sistema."""
        self.bus = bus
        self.modem: str | None = None
        self.nombre: str | None = None
        self.direccion: str | None = None
        # Ids de las llamadas que hemos marcado NOSOTROS. Es la única fuente
        # totalmente fiable sobre la dirección de una llamada: no depende de lo
        # que el móvil decida contar por HFP, que resultó ser poco de fiar.
        self.marcadas_por_nosotros: set[str] = set()

    # --- Descubrimiento del módem -------------------------------------------

    async def buscar_modem(self, direccion_preferida: str = "") -> bool:
        """Busca el módem HFP entre los que oFono conozca.

        Args:
            direccion_preferida: MAC del móvil que se quiere, si hay varios.
                Vacío significa "el primero que sirva".

        Returns:
            `True` si hay un módem listo.
        """
        try:
            modems = await llamar(
                self.bus,
                destino=SERVICIO,
                ruta="/",
                interfaz=IFAZ_MANAGER,
                metodo="GetModems",
            )
        except ErrorBus as e:
            logger.debug(f"oFono no responde todavía: {e}")
            self.modem = None
            return False

        for ruta, props in modems or []:
            if props.get("Type") != "hfp":
                continue
            serial = str(props.get("Serial", ""))
            if direccion_preferida and serial.upper() != direccion_preferida.upper():
                continue
            if not props.get("Powered"):
                # `Powered` lo pone oFono solo cuando el enlace HFP está
                # establecido; antes de eso el módem existe pero no sirve.
                continue
            if self.modem != ruta:
                logger.info(f"Módem HFP listo: {props.get('Name')} ({serial})")
            self.modem = ruta
            self.nombre = str(props.get("Name", "") or "")
            self.direccion = serial
            return True

        if self.modem is not None:
            logger.info("El módem HFP ha desaparecido; el móvil se ha desconectado")
        self.modem = None
        return False

    @property
    def listo(self) -> bool:
        """Indica si hay un módem sobre el que actuar."""
        return self.modem is not None

    def _exigir_modem(self) -> str:
        if self.modem is None:
            raise SinTelefono("no hay ningún móvil conectado por Bluetooth")
        return self.modem

    # --- Consultas -----------------------------------------------------------

    async def listar(self) -> list[Llamada]:
        """Devuelve las llamadas que hay ahora mismo."""
        modem = self._exigir_modem()
        try:
            crudas = await llamar(
                self.bus,
                destino=SERVICIO,
                ruta=modem,
                interfaz=IFAZ_GESTOR_LLAMADAS,
                metodo="GetCalls",
            )
        except ErrorBus as e:
            raise ErrorTelefono(str(e)) from e
        return [llamada_desde_propiedades(ruta, props) for ruta, props in crudas or []]

    async def _buscar(self, id_llamada: str | None) -> Llamada:
        """Localiza una llamada por su id, o la única que haya."""
        llamadas = await self.listar()
        vivas = [ll for ll in llamadas if ll.viva]
        if id_llamada:
            for ll in llamadas:
                if ll.id == id_llamada:
                    return ll
            raise ErrorTelefono(f"la llamada {id_llamada} ya no existe")
        if not vivas:
            raise ErrorTelefono("no hay ninguna llamada ahora mismo")
        return vivas[0]

    # --- Acciones ------------------------------------------------------------

    async def marcar(self, numero: str) -> Llamada:
        """Llama a un número.

        Args:
            numero: El número, ya normalizado.

        Returns:
            La llamada recién creada.
        """
        modem = self._exigir_modem()
        try:
            ruta = await llamar(
                self.bus,
                destino=SERVICIO,
                ruta=modem,
                interfaz=IFAZ_GESTOR_LLAMADAS,
                metodo="Dial",
                firma="ss",
                # El segundo argumento es `hide_callerid`: vacío = lo que tenga
                # configurado la línea.
                cuerpo=[numero, ""],
            )
        except ErrorBus as e:
            raise ErrorTelefono(f"no se pudo marcar: {e}") from e
        id_llamada = id_de_ruta(str(ruta))
        self.marcadas_por_nosotros.add(id_llamada)
        logger.info(f"Marcando {numero} -> {ruta}")
        return await self._buscar(id_llamada)

    async def contestar(self, id_llamada: str | None = None) -> Llamada:
        """Contesta una llamada entrante.

        Elige entre `Answer()` y `HoldAndAnswer()` según el estado; ver el
        docstring del módulo. Es idempotente: contestar una llamada que ya está
        en curso no falla, devuelve la llamada tal cual está.
        """
        modem = self._exigir_modem()
        llamada = await self._buscar(id_llamada)

        if llamada.estado is EstadoLlamada.EN_CURSO:
            return llamada
        if llamada.estado not in ESTADOS_CONTESTABLES:
            raise ErrorTelefono(f"la llamada no se puede contestar, está {llamada.estado.value}")

        try:
            if llamada.estado is EstadoLlamada.EN_ESPERA:
                # Ya hay otra conversación: hay que ponerla en espera.
                await llamar(
                    self.bus,
                    destino=SERVICIO,
                    ruta=modem,
                    interfaz=IFAZ_GESTOR_LLAMADAS,
                    metodo="HoldAndAnswer",
                )
            else:
                await llamar(
                    self.bus,
                    destino=SERVICIO,
                    ruta=f"{modem}/{llamada.id}",
                    interfaz=IFAZ_LLAMADA,
                    metodo="Answer",
                )
        except ErrorBus as e:
            raise ErrorTelefono(f"no se pudo contestar: {e}") from e

        logger.info(f"Llamada contestada: {llamada.id}")
        return await self._buscar(llamada.id)

    async def colgar(self, id_llamada: str | None = None) -> None:
        """Cuelga una llamada, o rechaza una que esté entrando."""
        modem = self._exigir_modem()
        llamada = await self._buscar(id_llamada)
        try:
            await llamar(
                self.bus,
                destino=SERVICIO,
                ruta=f"{modem}/{llamada.id}",
                interfaz=IFAZ_LLAMADA,
                metodo="Hangup",
            )
        except ErrorBus as e:
            raise ErrorTelefono(f"no se pudo colgar: {e}") from e
        logger.info(f"Llamada colgada: {llamada.id}")

    async def colgar_todas(self) -> None:
        """Cuelga todo lo que haya."""
        modem = self._exigir_modem()
        try:
            await llamar(
                self.bus,
                destino=SERVICIO,
                ruta=modem,
                interfaz=IFAZ_GESTOR_LLAMADAS,
                metodo="HangupAll",
            )
        except ErrorBus as e:
            raise ErrorTelefono(f"no se pudo colgar: {e}") from e
        logger.info("Colgadas todas las llamadas")

    async def enviar_tonos(self, tonos: str) -> None:
        """Marca dígitos DTMF durante una conversación.

        Args:
            tonos: Solo `0-9`, `*`, `#`, `A-D`.

        Raises:
            ErrorTelefono: Si hay algún carácter que no es un tono válido.
        """
        modem = self._exigir_modem()
        permitidos = set("0123456789*#ABCD")
        limpio = tonos.strip().upper().replace(" ", "")
        if not limpio or any(c not in permitidos for c in limpio):
            raise ErrorTelefono(f"«{tonos}» no son tonos válidos; solo 0-9, * y #")
        try:
            await llamar(
                self.bus,
                destino=SERVICIO,
                ruta=modem,
                interfaz=IFAZ_GESTOR_LLAMADAS,
                metodo="SendTones",
                firma="s",
                cuerpo=[limpio],
            )
        except ErrorBus as e:
            raise ErrorTelefono(f"no se pudieron marcar los tonos: {e}") from e

    # --- Señales -------------------------------------------------------------

    async def escuchar_cambios(self, al_cambiar: Any) -> None:
        """Se suscribe a las señales de llamadas de oFono.

        Args:
            al_cambiar: Se llama con `(clase, ruta, propiedades)` donde `clase`
                es `"añadida"`, `"quitada"` o `"cambiada"`.
        """

        def manejador(mensaje: Message) -> None:
            if mensaje.interface == IFAZ_GESTOR_LLAMADAS:
                if mensaje.member == "CallAdded":
                    al_cambiar("añadida", mensaje.body[0], desenvolver(mensaje.body[1]))
                elif mensaje.member == "CallRemoved":
                    al_cambiar("quitada", mensaje.body[0], {})
            elif mensaje.interface == IFAZ_LLAMADA and mensaje.member == "PropertyChanged":
                nombre, valor = mensaje.body[0], desenvolver(mensaje.body[1])
                al_cambiar("cambiada", mensaje.path, {nombre: valor})
            elif mensaje.interface == IFAZ_MODEM and mensaje.member == "PropertyChanged":
                al_cambiar("modem", mensaje.path, {mensaje.body[0]: desenvolver(mensaje.body[1])})

        await escuchar(
            self.bus,
            manejador,
            interfaces=(IFAZ_GESTOR_LLAMADAS, IFAZ_LLAMADA, IFAZ_MODEM, IFAZ_MANAGER),
        )


def ahora() -> datetime:
    """Devuelve el instante actual, en UTC y con zona."""
    return datetime.now(UTC)


__all__ = [
    "ErrorTelefono",
    "SinTelefono",
    "Telefono",
    "ahora",
    "estado_legible",
    "id_de_ruta",
    "llamada_desde_propiedades",
]
