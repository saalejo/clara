"""Ciclo de vida del puente: conectar, vigilar, reconectar, apagar limpio.

Es la pieza que junta las demás. Su regla de oro es que **nada de lo que pase
con el Bluetooth puede tumbar el proceso**: el móvil se aleja, el dongle se
reenumera, oFono se reinicia... y el puente tiene que seguir en pie contestando
que ahora mismo no hay teléfono. Un agente de voz sin teléfono sigue
conversando; un agente que se cae, no.

Por eso el bucle de vigilancia lo envuelve todo en `try/except Exception` y se
limita a registrar. Es el mismo criterio que el proyecto ya aplica a los
servidores MCP: lo que falla se anota, y se sigue.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from dbus_fast import BusType
from dbus_fast.aio import MessageBus
from loguru import logger

from voice_agent_core.telefonia import (
    ESTADOS_CONTESTABLES,
    RELLENOS_SIN_IDENTIFICAR,
    EstadoLlamada,
    EstadoTelefonia,
    EventoTelefonia,
    Llamada,
    ResultadoSincronizacion,
    TipoEvento,
)
from voice_agent_telefonia.audio_sco import AudioSCO
from voice_agent_telefonia.bus import ErrorBus, conectar
from voice_agent_telefonia.contactos import Agenda
from voice_agent_telefonia.eventos import BusDeEventos
from voice_agent_telefonia.llamadas import (
    ErrorTelefono,
    SinTelefono,
    Telefono,
    ahora,
    id_de_ruta,
    llamada_desde_propiedades,
)
from voice_agent_telefonia.pbap import descargar_agenda
from voice_agent_telefonia.preferencias import cargar as cargar_preferencias
from voice_agent_telefonia.preferencias import guardar as guardar_preferencias
from voice_agent_telefonia.preferencias import ruta as ruta_preferencias
from voice_agent_telefonia.reconexion import reconectar_movil

#: Cada cuánto se comprueba si hay módem. Es un respaldo de las señales, no la
#: vía principal: si una señal se pierde (oFono reiniciado, dongle reenumerado),
#: esto lo arregla solo en unos segundos.
INTERVALO_VIGILANCIA = 5.0

#: Cada cuánto se intenta conectar el móvil cuando no lo está. Más espaciado
#: que la vigilancia a propósito: cada intento pagina la radio durante varios
#: segundos, y machacar a un móvil que está fuera de casa no lo acerca.
INTERVALO_RECONEXION = 30.0


class Servicio:
    """El puente entero: buses, teléfono, agenda y eventos."""

    def __init__(
        self,
        *,
        directorio_datos: Path,
        direccion_preferida: str = "",
        ttl_agenda_horas: int = 12,
        modo_audio: str = "off",
        msbc_audio: bool = False,
    ) -> None:
        """Prepara el puente sin conectar nada todavía.

        Args:
            directorio_datos: Donde viven la caché de la agenda y el .vcf.
            direccion_preferida: MAC del móvil, si hay más de uno emparejado.
            ttl_agenda_horas: Cada cuánto se vuelve a descargar la agenda.
            modo_audio: Qué hacer con el audio SCO de las llamadas. `off` deja
                el audio en el móvil, como siempre; ver `audio_sco.py`.
            msbc_audio: Anunciar mSBC además de CVSD; ver `AudioSCO`.
        """
        self.directorio = directorio_datos
        self.direccion_preferida = direccion_preferida
        self.ttl_agenda_horas = ttl_agenda_horas
        self.audio = AudioSCO(modo_audio, con_msbc=msbc_audio)

        self.eventos = BusDeEventos()
        self.agenda = Agenda(directorio_datos / "telefonia" / "agenda.json")
        self.telefono: Telefono | None = None

        self.ruta_preferencias = ruta_preferencias(directorio_datos)
        self.preferencias = cargar_preferencias(self.ruta_preferencias)

        self._bus_sistema: MessageBus | None = None
        self._bus_sesion: MessageBus | None = None
        self._vigilancia: asyncio.Task[None] | None = None
        self._sincronizando = asyncio.Lock()
        self._ultimas: dict[str, Llamada] = {}
        # Dirección de cada llamada, anotada en cuanto llega `CallAdded` con su
        # estado inicial. Es el único momento en que se puede saber: `incoming`
        # es entrante y `dialing`/`alerting` salientes, pero ese estado dura
        # segundos. Ver `_refrescar_llamadas`.
        self._direcciones: dict[str, bool] = {}
        # Llamadas para las que ya hay un descuelgue automático en marcha.
        # `_refrescar_llamadas` corre en tareas concurrentes —una por señal del
        # bus— y puede reentrar con la misma llamada entrante antes de que la
        # anterior haya terminado.
        self._autocontestando: set[str] = set()

    # --- Arranque y parada ---------------------------------------------------

    async def arrancar(self) -> None:
        """Conecta lo que se pueda y deja el bucle de vigilancia en marcha.

        No falla si el Bluetooth no está: el puente arranca igual y se va
        enganchando según aparezcan las cosas. Eso es lo que permite que la
        unidad de systemd del puente no dependa de `bluetooth.service`, que
        además es del sistema y el gestor de usuario no conoce.
        """
        self.agenda.cargar_cache()
        await self.audio.arrancar(self.directorio / "run" / "telefonia-audio.sock")
        await self._conectar_buses()
        self._vigilancia = asyncio.create_task(self._vigilar())
        logger.info("Puente de telefonía en marcha")

    async def parar(self) -> None:
        """Cierra todo con orden."""
        if self._vigilancia is not None:
            self._vigilancia.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._vigilancia
            self._vigilancia = None
        await self.audio.parar()
        for bus in (self._bus_sistema, self._bus_sesion):
            if bus is not None:
                with contextlib.suppress(Exception):
                    bus.disconnect()
        self._bus_sistema = self._bus_sesion = None
        logger.info("Puente de telefonía detenido")

    async def _conectar_buses(self) -> None:
        """Abre los dos buses; los fallos se registran y se reintentan luego."""
        if self._bus_sistema is None:
            try:
                # Con negociación de descriptores: es lo que permite que
                # `NewConnection` del agente de audio entregue el socket SCO.
                self._bus_sistema = await conectar(BusType.SYSTEM, con_fds=True)
                self.telefono = Telefono(self._bus_sistema)
                await self.telefono.escuchar_cambios(self._al_cambiar_algo)
                logger.debug("Conectado al bus del sistema (oFono)")
            except ErrorBus as e:
                logger.warning(f"Sin bus del sistema todavía: {e}")
        if self._bus_sesion is None:
            try:
                self._bus_sesion = await conectar(BusType.SESSION)
                logger.debug("Conectado al bus de sesión (obexd)")
            except ErrorBus as e:
                logger.warning(f"Sin bus de sesión todavía: {e}")

    # --- Vigilancia ----------------------------------------------------------

    async def _vigilar(self) -> None:
        """Comprueba periódicamente el módem y refresca la agenda cuando toca.

        También es quien llama a la puerta del móvil cuando no está conectado:
        tras reenumerarse el adaptador —cambio de puerto, hub, reinicio de la
        placa— Android no vuelve solo, y sin esto el manos libres quedaba
        muerto hasta que alguien tocara el teléfono. Ver `reconexion.py`.
        """
        conectado_antes = False
        # Negativo para que el primer intento sea inmediato: al arrancar el
        # puente (por ejemplo tras un reinicio de la placa) el móvil suele
        # estar ahí, esperando a que alguien tome la iniciativa.
        ultimo_intento_conexion = -INTERVALO_RECONEXION
        while True:
            try:
                await self._conectar_buses()
                await self.audio.asegurar_registro(self._bus_sistema)
                conectado = False
                if self.telefono is not None:
                    conectado = await self.telefono.buscar_modem(self.direccion_preferida)

                if conectado and not conectado_antes:
                    self.eventos.publicar(
                        EventoTelefonia(
                            tipo=TipoEvento.TELEFONO_CONECTADO,
                            momento=ahora(),
                            motivo=(self.telefono.nombre or "") if self.telefono else "",
                        )
                    )
                    # Al conectar es buen momento para la agenda: el canal está
                    # libre y así los nombres están listos antes de la primera
                    # llamada.
                    if self.agenda.caducada(self.ttl_agenda_horas):
                        asyncio.create_task(self._sincronizar_en_segundo_plano())  # noqa: RUF006
                elif not conectado and conectado_antes:
                    self.eventos.publicar(
                        EventoTelefonia(tipo=TipoEvento.TELEFONO_DESCONECTADO, momento=ahora())
                    )
                    self._ultimas.clear()
                    self._direcciones.clear()
                    # Sin esto, un id que quedara marcado impediría autocontestar
                    # esa misma llamada si el móvil se reconecta y oFono reutiliza
                    # la ruta. La preferencia NO se toca: sobrevive al móvil.
                    self._autocontestando.clear()

                if not conectado and self._bus_sistema is not None:
                    ahora_mono = time.monotonic()
                    if ahora_mono - ultimo_intento_conexion >= INTERVALO_RECONEXION:
                        ultimo_intento_conexion = ahora_mono
                        nombre = await reconectar_movil(self._bus_sistema, self.direccion_preferida)
                        if nombre:
                            logger.info(f"Móvil conectado a iniciativa de la placa: {nombre}")

                conectado_antes = conectado
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"El bucle de vigilancia ha tropezado: {e}")

            await asyncio.sleep(INTERVALO_VIGILANCIA)

    def _al_cambiar_algo(self, clase: str, ruta: str, propiedades: dict[str, Any]) -> None:
        """Recibe las señales de oFono y las convierte en eventos.

        Es síncrona porque la llama dbus-fast desde su manejador de mensajes.
        Lo que necesite el bus se hace en una tarea aparte.

        La dirección de la llamada se anota **aquí**, con las propiedades que
        trae la propia señal `CallAdded`, y no después. Ver `_direcciones`.
        """
        if clase == "añadida" and propiedades:
            llamada = llamada_desde_propiedades(ruta, propiedades)
            self._direcciones[llamada.id] = llamada.entrante
            # El estado EN CRUDO de oFono, sin traducir, y la dirección que se
            # ha deducido de él. Es el único momento en que ese dato existe —dos
            # décimas después la llamada ya está en otro estado— y sin él una
            # dirección mal deducida es indiagnosticable a posteriori: el log
            # solo decía "llamada_saliente" y no de dónde salía esa conclusión.
            logger.info(
                f"CallAdded {llamada.id}: State={propiedades.get('State')!r} "
                f"LineIdentification={propiedades.get('LineIdentification')!r} "
                f"-> {'entrante' if llamada.entrante else 'saliente'}"
            )
        if clase in ("añadida", "cambiada", "quitada"):
            asyncio.create_task(self._refrescar_llamadas(clase, ruta))  # noqa: RUF006

    async def _refrescar_llamadas(self, clase: str, ruta: str) -> None:
        """Vuelve a leer las llamadas y publica lo que haya cambiado.

        Se relee el estado completo en vez de fiarse de lo que trae la señal.
        Es una llamada barata a `GetCalls` y evita toda una clase de errores:
        las señales pueden llegar desordenadas o perderse, y una máquina de
        estados construida solo con ellas acaba desincronizada de la realidad
        justo cuando más importa.

        La excepción es **la dirección**, que se toma de `_direcciones` y no de
        lo que se acabe de leer. El motivo lo enseñó una llamada de verdad: la
        dirección solo se puede deducir del estado INICIAL —`incoming` es
        entrante, `dialing`/`alerting` son salientes—, y para cuando `GetCalls`
        contesta, una llamada entrante ya puede haber pasado a `active`. El
        resultado era anunciar como saliente una llamada que estaba entrando.
        """
        if self.telefono is None or not self.telefono.listo:
            return
        try:
            llamadas = await self.telefono.listar()
        except (ErrorTelefono, SinTelefono):
            return

        actuales = {ll.id: ll for ll in llamadas if ll.viva}

        for id_llamada, llamada in actuales.items():
            # Orden de fiabilidad para la dirección, de más a menos:
            #   1. La marcamos nosotros -> saliente, seguro.
            #   2. El estado inicial que trajo `CallAdded`.
            #   3. Lo que se dedujo la última vez.
            # Nunca lo que se acabe de releer: para entonces la llamada ya puede
            # estar en `active`, que no dice nada de la dirección.
            if self.telefono is not None and id_llamada in self.telefono.marcadas_por_nosotros:
                llamada.entrante = False
            elif (entrante := self._direcciones.get(id_llamada)) is not None:
                llamada.entrante = entrante
            elif (anterior_dir := self._ultimas.get(id_llamada)) is not None:
                llamada.entrante = anterior_dir.entrante

            self.poner_nombre(llamada)
            anterior = self._ultimas.get(id_llamada)
            if anterior is None:
                tipo = (
                    TipoEvento.LLAMADA_ENTRANTE if llamada.entrante else TipoEvento.LLAMADA_SALIENTE
                )
                self.eventos.publicar(EventoTelefonia(tipo=tipo, momento=ahora(), llamada=llamada))
                if llamada.estado is EstadoLlamada.EN_CURSO:
                    # La vemos por primera vez y ya está en curso. Pasa cuando
                    # el puente arranca o se reengancha con una conversación ya
                    # empezada. Sin esto, `LLAMADA_CONTESTADA` no saldría nunca
                    # para ella, porque la rama de abajo pide una TRANSICIÓN a
                    # `EN_CURSO` y aquí no hay estado anterior del que venir.
                    #
                    # OJO con las llamadas de app: su `EN_CURSO` es mentira —el
                    # móvil lo declara mientras la aplicación aún timbra, ver
                    # `Llamada.es_de_app`—, así que este evento no significa que
                    # haya nadie al otro lado. Quien reaccione a él tiene que
                    # comprobarlo; el agente lo hace en `_saludar_en_llamada`.
                    self.eventos.publicar(
                        EventoTelefonia(
                            tipo=TipoEvento.LLAMADA_CONTESTADA, momento=ahora(), llamada=llamada
                        )
                    )
                elif llamada.entrante and self.preferencias.autocontestar:
                    asyncio.create_task(self._autocontestar(id_llamada))  # noqa: RUF006
            elif (
                anterior.estado is not EstadoLlamada.EN_CURSO
                and llamada.estado is EstadoLlamada.EN_CURSO
            ):
                self.eventos.publicar(
                    EventoTelefonia(
                        tipo=TipoEvento.LLAMADA_CONTESTADA, momento=ahora(), llamada=llamada
                    )
                )

        for id_llamada, anterior in list(self._ultimas.items()):
            if id_llamada not in actuales:
                self._direcciones.pop(id_llamada, None)
                if self.telefono is not None:
                    self.telefono.marcadas_por_nosotros.discard(id_llamada)
                anterior.estado = EstadoLlamada.TERMINADA
                self.eventos.publicar(
                    EventoTelefonia(
                        tipo=TipoEvento.LLAMADA_TERMINADA, momento=ahora(), llamada=anterior
                    )
                )

        self._ultimas = actuales
        _ = clase, ruta  # se conservan por claridad del manejador

    # --- Autocontestar -------------------------------------------------------

    def fijar_autocontestar(self, activo: bool) -> bool:
        """Cambia la preferencia, la persiste y la anuncia. Devuelve el valor nuevo.

        Es caliente por construcción: el puente es un proceso de larga vida y esto
        es un atributo, así que el cambio surte efecto en la llamada siguiente sin
        reiniciar nada. Es justo lo contrario del agente, que lee su configuración
        una sola vez al arrancar.
        """
        self.preferencias.autocontestar = activo
        guardar_preferencias(self.ruta_preferencias, self.preferencias)
        logger.info(f"Autocontestar {'activado' if activo else 'desactivado'}")
        self.eventos.publicar(
            EventoTelefonia(
                tipo=TipoEvento.AUTOCONTESTAR_CAMBIADO,
                momento=ahora(),
                motivo="activado" if activo else "desactivado",
            )
        )
        return activo

    def alternar_autocontestar(self) -> bool:
        """Invierte la preferencia. Devuelve el valor nuevo.

        Existe para el mando físico: sin esto tendría que leer y luego escribir, y
        entre las dos cosas el panel podría haber cambiado el valor. Un solo
        método evita ese TOCTOU y le ahorra un viaje por el socket.
        """
        return self.fijar_autocontestar(not self.preferencias.autocontestar)

    async def _autocontestar(self, id_llamada: str) -> None:
        """Descuelga sola una llamada entrante, si nadie se adelanta.

        Cinco reglas, y ninguna es decorativa:

        1. **Guarda de reentrada.** `_refrescar_llamadas` corre en una tarea por
           señal del bus, y las señales pueden repetirse.
        2. **Margen antes de descolgar.** Da tiempo a rechazar a mano, y deja que
           la máquina de estados de oFono se asiente antes de otra orden.
        3. **Releer antes de actuar.** En esos segundos la persona puede haber
           cogido el teléfono, o el otro haber colgado. Que la llamada ya no sea
           contestable es la carrera **normal**, no un error.
        4. **`contestar` ya es idempotente**, así que un descuelgue de más no
           rompe nada.
        5. **Nunca propagar el error.** Esto corre en una tarea suelta: una
           excepción aquí se convertiría en un «Task exception was never
           retrieved» que no dice nada de la causa.
        """
        if id_llamada in self._autocontestando:
            return
        self._autocontestando.add(id_llamada)
        try:
            await asyncio.sleep(self.preferencias.autocontestar_segundos)
            if self.telefono is None:
                return
            llamadas = {ll.id: ll for ll in await self.telefono.listar()}
            llamada = llamadas.get(id_llamada)
            if llamada is None or llamada.estado not in ESTADOS_CONTESTABLES:
                logger.debug(f"No autocontesto {id_llamada}: ya no es contestable")
                return
            # `listar()` no resuelve la agenda, así que sin esto el log diría el
            # número crudo mientras el anuncio en voz alta dice «Mamá Nora». Cuadrar
            # los dos importa para poder seguir una llamada entre los dos logs.
            self.poner_nombre(llamada)
            logger.info(f"Autocontestando la llamada de {llamada.quien}")
            await self.telefono.contestar(id_llamada)
        except (ErrorTelefono, SinTelefono) as e:
            logger.warning(f"No he podido autocontestar {id_llamada}: {e}")
        except asyncio.CancelledError:
            raise
        finally:
            self._autocontestando.discard(id_llamada)

    def poner_nombre(self, llamada: Llamada) -> None:
        """Resuelve el número contra la agenda para poder decir 'te llama Ana'.

        Las llamadas de apps de mensajería no traen número que resolver: el
        identificador es un relleno de Android. Se salta la búsqueda en vez de
        recorrer 1176 contactos para no encontrar nada.
        """
        if llamada.nombre_agenda or not llamada.numero:
            return
        if llamada.numero in RELLENOS_SIN_IDENTIFICAR:
            return
        if (contacto := self.agenda.por_numero(llamada.numero)) is not None:
            llamada.nombre_agenda = contacto.nombre

    # --- Agenda --------------------------------------------------------------

    async def sincronizar_agenda(self, *, forzar: bool = False) -> ResultadoSincronizacion:
        """Descarga la agenda del móvil por PBAP.

        Args:
            forzar: Descargarla aunque la copia local siga fresca.
        """
        if self._sincronizando.locked():
            return ResultadoSincronizacion(ok=False, detalle="ya hay una sincronización en marcha")
        async with self._sincronizando:
            if not forzar and not self.agenda.caducada(self.ttl_agenda_horas):
                return ResultadoSincronizacion(
                    ok=True,
                    contactos=len(self.agenda.contactos),
                    detalle="la copia local sigue fresca",
                )
            if self._bus_sesion is None:
                return ResultadoSincronizacion(ok=False, detalle="no hay bus de sesión")
            direccion = self.direccion_preferida or (
                self.telefono.direccion if self.telefono else None
            )
            if not direccion:
                return ResultadoSincronizacion(ok=False, detalle="no hay ningún móvil conocido")

            contactos, resultado = await descargar_agenda(
                self._bus_sesion, direccion, self.directorio / "telefonia" / "agenda.vcf"
            )
            if resultado.ok and contactos:
                self.agenda.reemplazar(contactos)
                self.eventos.publicar(
                    EventoTelefonia(
                        tipo=TipoEvento.AGENDA_ACTUALIZADA,
                        momento=ahora(),
                        motivo=f"{len(contactos)} contactos",
                    )
                )
            return resultado

    async def _sincronizar_en_segundo_plano(self) -> None:
        """Sincroniza sin que nadie espere, y sin dejar escapar excepciones."""
        try:
            await self.sincronizar_agenda()
        except Exception as e:
            logger.warning(f"No se pudo sincronizar la agenda: {e}")

    # --- Estado --------------------------------------------------------------

    async def estado(self) -> EstadoTelefonia:
        """Compone la foto completa, con su frase lista para decir en voz alta."""
        listo = self.telefono is not None and self.telefono.listo
        llamadas: list[Llamada] = []
        if listo and self.telefono is not None:
            with contextlib.suppress(ErrorTelefono, SinTelefono):
                llamadas = [ll for ll in await self.telefono.listar() if ll.viva]
                for llamada in llamadas:
                    self.poner_nombre(llamada)

        return EstadoTelefonia(
            disponible=listo,
            adaptador_encendido=self._bus_sistema is not None,
            telefono_direccion=self.telefono.direccion if self.telefono else None,
            telefono_nombre=self.telefono.nombre if self.telefono else None,
            telefono_conectado=listo,
            modem_listo=listo,
            llamadas=llamadas,
            contactos=len(self.agenda.contactos),
            contactos_actualizados=self.agenda.actualizada,
            autocontestar=self.preferencias.autocontestar,
            autocontestar_segundos=self.preferencias.autocontestar_segundos,
            detalle=_redactar_detalle(
                listo,
                self.telefono.nombre if self.telefono else None,
                llamadas,
                len(self.agenda.contactos),
                self.agenda.actualizada,
                self.preferencias.autocontestar,
            ),
        )


def _redactar_detalle(
    listo: bool,
    nombre: str | None,
    llamadas: list[Llamada],
    contactos: int,
    actualizada: datetime | None,
    autocontestar: bool = False,
) -> str:
    """Escribe la frase que el modelo puede decir tal cual.

    Se redacta aquí, igual que `descripcion` en la herramienta de la fecha,
    para que el LLM no tenga que componerla: cuando se le dan datos sueltos
    tiende a añadir matices que no están.
    """
    if not listo:
        return (
            "No hay ningún teléfono conectado por Bluetooth ahora mismo. "
            "Comprueba que el móvil esté cerca y con el Bluetooth encendido."
        )

    partes = [f"El teléfono {nombre or 'emparejado'} está conectado"]
    if llamadas:
        descripciones = [f"{ll.quien} ({ll.estado.value.replace('_', ' ')})" for ll in llamadas]
        partes.append("y ahora mismo hay una llamada con " + ", ".join(descripciones))
    else:
        partes.append("y no hay ninguna llamada")

    if contactos:
        cuando = ""
        if actualizada is not None:
            horas = (ahora() - actualizada).total_seconds() / 3600
            cuando = (
                ", sincronizados hace menos de una hora"
                if horas < 1
                else f", sincronizados hace {int(horas)} horas"
            )
        partes.append(f". Tengo {contactos} contactos{cuando}")
    else:
        partes.append(". Todavía no tengo la agenda descargada")

    if autocontestar:
        # El modelo dice esta frase tal cual, así que tiene que ser precisa: el
        # puente descuelga, no conversa. Ver `docs/telefonia.md`.
        partes.append(". Tengo puesto que las llamadas entrantes se descuelguen solas")

    return "".join(p if p.startswith(".") else " " + p for p in partes).strip() + "."


__all__ = ["INTERVALO_VIGILANCIA", "Servicio", "id_de_ruta"]
