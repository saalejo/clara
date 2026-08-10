"""El demonio: junta las piezas y ejecuta.

Lee botones, los traduce a verbos con la tabla de `acciones.py`, los ejecuta y pita
el resultado. Todo lo que puede fallar —el mezclador, systemd, el device— falla
hacia un pitido de error, nunca hacia una excepción: un mando físico que se muere
porque alguien ha tocado el hub USB no sirve de nada.

## Las cuatro capas que evitan un codazo catastrófico

Parar el agente con un gesto único sería la vía directa a «se me apoyó el codo y el
agente se murió». Así que hay cuatro barreras, y ninguna sobra:

1. **Solo con mantenido**, y del nivel más alto: 2,5 segundos de rocker.
2. **Un pip al cruzar cada frontera**, mientras sigues apretando, para que oigas
   por dónde vas antes de soltar.
3. **Confirmación con un clic de MUTE** dentro de diez segundos. Cualquier gesto
   del rocker cancela — y cancela *sin* ejecutar lo que ese gesto pedía, porque
   quien cancela no quiere además otra cosa.
4. **Durante una llamada, estos gestos no existen** (lo garantiza el mapa).

La confirmación se hace con MUTE y no repitiendo el gesto porque repetir un
mantenido de 2,5 segundos es incómodo, y porque MUTE ya es el botón de «sí, eso».

## Asimetría deliberada

**Arrancar no pide confirmación. Parar y reiniciar sí.** Arrancar es inocuo; los
otros dos interrumpen una conversación en curso. Tratarlos igual sería simetría mal
entendida.

## Orden aceptada no es orden completada

`StartUnit` vuelve en centésimas de segundo, y para el agente **ni siquiera el
`active` de systemd significa listo**: su unidad es Quadlet con
`--sdnotify=conmon`, así que se da por activa en cuanto arranca el contenedor, no
cuando el proceso ha cargado Whisper y Piper. Medido en la placa: 24 segundos de
diferencia entre lo uno y lo otro. Ver `_testigo_de`.

Así que hay dos pitidos: uno inmediato de «recibido» y otro cuando la unidad está
lista **de verdad**. Mientras ese segundo está pendiente, la unidad queda **en
vuelo** y otra orden sobre ella se rechaza: es lo que evita encolar tres
reinicios por nerviosismo.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from loguru import logger

from voice_agent_botones.acciones import (
    DESTRUCTIVOS,
    NECESITAN_PUENTE,
    Modo,
    Verbo,
    verbo_de,
)
from voice_agent_botones.config import Ajustes
from voice_agent_botones.entrada import LectorDeBotones, gestos_de
from voice_agent_botones.gestos import KEY_MUTE, DetectorDeGestos, Gesto, Nivel
from voice_agent_botones.mezclador import Mezclador
from voice_agent_botones.pitidos import Pitidos
from voice_agent_botones.servicios import Servicios
from voice_agent_botones.telefonia import ClienteTelefonia
from voice_agent_core.rutas import ruta_estado
from voice_agent_core.telefonia import TipoEvento

# Todos los verbos del mapa están implementados. `test_botones_acciones` comprueba
# que ninguno se quede fuera sin que nada lo delate.
VERBOS_IMPLEMENTADOS = frozenset(set(Verbo) - {Verbo.NADA})

# Qué pitido anuncia el cruce de cada frontera de nivel.
PITIDO_DE_NIVEL = {Nivel.LARGO: "nivel2", Nivel.MUY_LARGO: "nivel3"}

# Retroceso del canal de eventos, igual que en `telefonia_anuncios` del agente.
ESPERA_INICIAL = 1.0
ESPERA_MAXIMA = 30.0

# Qué modo implica cada evento del puente. Los que no están no cambian el modo.
MODO_POR_EVENTO = {
    TipoEvento.LLAMADA_ENTRANTE: Modo.LLAMADA_ENTRANTE,
    TipoEvento.LLAMADA_CONTESTADA: Modo.LLAMADA_EN_CURSO,
    TipoEvento.LLAMADA_SALIENTE: Modo.LLAMADA_EN_CURSO,
    TipoEvento.LLAMADA_TERMINADA: Modo.NORMAL,
    TipoEvento.TELEFONO_DESCONECTADO: Modo.NORMAL,
}


class Demonio:
    """Cablea lector, detector, mezclador, servicios y pitidos."""

    def __init__(
        self,
        ajustes: Ajustes,
        *,
        mezclador: Mezclador | None = None,
        pitidos: Pitidos | None = None,
        servicios: Servicios | None = None,
        telefonia: ClienteTelefonia | None = None,
        lector: LectorDeBotones | None = None,
    ) -> None:
        """Monta el demonio, inyectando lo que haga falta para los tests."""
        self._ajustes = ajustes
        self._mezclador = mezclador if mezclador is not None else Mezclador(ajustes)
        self._pitidos = pitidos if pitidos is not None else Pitidos(ajustes.dir_pitidos)
        self._servicios = servicios if servicios is not None else Servicios(ajustes)
        self._telefonia = (
            telefonia if telefonia is not None else ClienteTelefonia(ajustes.socket_telefonia)
        )
        # La anotación no es decorativa: sin ella mypy infiere el tipo literal
        # `Literal[Modo.NORMAL]` y da por imposible cualquier comparación con otro
        # modo, incluso las que ocurren de verdad.
        self._modo: Modo = Modo.NORMAL

        # Último estado conocido del micrófono. Se guarda porque al reenumerarse el
        # USB la tarjeta vuelve con los valores por defecto del driver, y un
        # silencio puesto por el usuario se desharía solo sin reaplicarlo.
        self._micro_abierto: bool | None = None

        # Verbo esperando confirmación, y la tarea que la caduca.
        self._pendiente: Verbo | None = None
        self._caducidad: asyncio.Task[None] | None = None

        # Unidades con una orden cuyo resultado todavía no se conoce.
        self._en_vuelo: set[str] = set()
        self._seguimientos: set[asyncio.Task[None]] = set()

        self._detector = DetectorDeGestos(
            ventana_ms=ajustes.ventana_agrupacion_ms,
            umbral_largo_ms=ajustes.umbral_largo_ms,
            umbral_muy_largo_ms=ajustes.umbral_muy_largo_ms,
            al_cruzar_nivel=self._al_cruzar_nivel,
            al_cancelar=lambda: self._pitidos.sonar("cancelado"),
        )
        self._lector = (
            lector
            if lector is not None
            else LectorDeBotones(
                ajustes.dispositivo,
                acaparar=ajustes.acaparar,
                espera_segundos=ajustes.espera_dispositivo_segundos,
                al_reconectar=self._al_reconectar,
            )
        )

    # -- Bucle principal ------------------------------------------------------

    async def ejecutar(self) -> None:
        """Atiende gestos hasta que se la cancele."""
        async with self._pitidos, self._lector:
            self._micro_abierto = await self._mezclador.micro_abierto()
            logger.info(
                "Mando físico listo"
                + ("" if self._micro_abierto is None else f" (micrófono {self._nombre_micro()})")
            )
            self._pitidos.sonar("listo")
            escucha = asyncio.create_task(self._escuchar_telefonia())
            self._seguimientos.add(escucha)
            if self._ajustes.recordatorio_micro_minutos > 0:
                recordatorio = asyncio.create_task(self._recordar_micro())
                self._seguimientos.add(recordatorio)
                recordatorio.add_done_callback(self._seguimientos.discard)
            try:
                async for gesto in gestos_de(self._lector, self._detector):
                    await self.atender(gesto)
            finally:
                escucha.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await escucha
                await self._recoger_tareas()

    async def atender(self, gesto: Gesto) -> None:
        """Traduce un gesto y lo ejecuta, o lo usa como confirmación."""
        verbo = verbo_de(self._modo, gesto)

        if self._pendiente is not None:
            await self._resolver_confirmacion(gesto)
            return

        logger.info(f"{gesto} -> {verbo}")
        if verbo is Verbo.NADA:
            # Que se oiga que el gesto llegó pero no significa nada en este modo: el
            # silencio se confundiría con un botón que no funciona.
            self._pitidos.sonar("error")
            return
        if verbo not in VERBOS_IMPLEMENTADOS:
            logger.warning(f"{verbo} todavía no está implementado")
            self._pitidos.sonar("error")
            return
        await self._hacer(verbo)

    # -- Confirmación ---------------------------------------------------------

    async def _resolver_confirmacion(self, gesto: Gesto) -> None:
        """Un clic de MUTE confirma; cualquier otra cosa cancela."""
        verbo = self._pendiente
        self._olvidar_pendiente()

        if verbo is not None and gesto.tecla == KEY_MUTE and gesto.nivel is Nivel.CORTO:
            logger.info(f"Confirmado: {verbo}")
            await self._hacer(verbo, confirmado=True)
            return

        # Se cancela y el gesto que ha cancelado NO se ejecuta: quien cancela no
        # quiere además otra cosa.
        logger.info(f"Cancelado: {verbo} (llegó {gesto})")
        self._pitidos.sonar("cancelado")

    def _pedir_confirmacion(self, verbo: Verbo) -> None:
        """Deja un verbo esperando un clic de MUTE, con su caducidad."""
        self._pendiente = verbo
        self._pitidos.sonar("pregunta")
        logger.info(
            f"{verbo} espera confirmación: pulsa MUTE en "
            f"{self._ajustes.confirmacion_segundos:.0f} s"
        )
        self._caducidad = asyncio.create_task(self._caducar())
        self._seguimientos.add(self._caducidad)
        self._caducidad.add_done_callback(self._seguimientos.discard)

    async def _caducar(self) -> None:
        """Cancela la confirmación pendiente al agotarse la ventana.

        Sin esto, la caducidad solo se notaría al llegar el gesto siguiente, y quien
        pulsó no sabría nunca que su orden se ha caído.
        """
        await asyncio.sleep(self._ajustes.confirmacion_segundos)
        if self._pendiente is not None:
            logger.info(f"Caducada la confirmación de {self._pendiente}")
            self._pendiente = None
            self._pitidos.sonar("cancelado")

    def _olvidar_pendiente(self) -> None:
        """Descarta la confirmación pendiente y su tarea de caducidad."""
        self._pendiente = None
        if self._caducidad is not None:
            self._caducidad.cancel()
            self._caducidad = None

    # -- Ejecución ------------------------------------------------------------

    async def _hacer(self, verbo: Verbo, *, confirmado: bool = False) -> None:
        """Ejecuta un verbo."""
        if verbo is Verbo.MICRO:
            await self._alternar_micro()
        elif verbo is Verbo.VOLUMEN_MAS:
            await self._mover_volumen(subir=True)
        elif verbo is Verbo.VOLUMEN_MENOS:
            await self._mover_volumen(subir=False)
        elif verbo in DESTRUCTIVOS:
            await self._gobernar(verbo, confirmado=confirmado)
        elif verbo in NECESITAN_PUENTE:
            await self._telefonear(verbo)

    async def _alternar_micro(self) -> None:
        """Silencia o reabre el micrófono, y lo anuncia."""
        abierto = await self._mezclador.alternar_micro()
        if abierto is None:
            self._pitidos.sonar("error")
            return
        self._micro_abierto = abierto
        # Agudo al abrir, grave al silenciar: el grave tiene que ser
        # inconfundible, porque un micrófono silenciado y olvidado hace que el
        # agente parezca averiado.
        self._pitidos.sonar("si" if abierto else "no")
        logger.info(f"Micrófono {self._nombre_micro()}")

    async def _mover_volumen(self, *, subir: bool) -> None:
        """Sube o baja el volumen, distinguiendo el límite."""
        resultado = await (self._mezclador.subir() if subir else self._mezclador.bajar())
        if resultado is None:
            self._pitidos.sonar("error")
            return
        porcentaje, en_el_limite = resultado
        self._pitidos.sonar("tope" if en_el_limite else "si")
        logger.info(f"Volumen al {porcentaje}%" + (" (límite)" if en_el_limite else ""))

    # -- Telefonía ------------------------------------------------------------

    async def _telefonear(self, verbo: Verbo) -> None:
        """Ejecuta los verbos que pasan por el puente.

        `RECHAZAR` y `COLGAR` mandan la misma orden: para oFono terminar una
        llamada entrante y terminar una en curso es lo mismo, y distinguirlas aquí
        solo añadiría un camino que no cambia nada. Se separan en el mapa porque en
        el gesto **sí** significan cosas distintas para quien lo pulsa.
        """
        if verbo is Verbo.AUTOCONTESTAR:
            activo = await self._telefonia.alternar_autocontestar()
            if activo is None:
                self._pitidos.sonar("error")
                return
            self._pitidos.sonar("si" if activo else "no")
            logger.info(f"Autocontestar {'activado' if activo else 'desactivado'}")
            return

        if verbo is Verbo.CONTESTAR:
            hecho = await self._telefonia.contestar()
        else:
            hecho = await self._telefonia.colgar()
        self._pitidos.sonar("si" if hecho else "error")

    async def _escuchar_telefonia(self) -> None:
        """Sigue el canal de eventos del puente para saber en qué modo estamos.

        No deja escapar nunca una excepción: que el puente no esté es el estado
        **normal** la mitad del tiempo, porque el modo «solo tarjeta de sonido» lo
        para a propósito. Se reintenta con retroceso exponencial, igual que hace el
        agente en `telefonia_anuncios`.

        **Solo se registra al cambiar de estado.** Anotar cada intento llenaría el
        journal a un mensaje cada treinta segundos para siempre, y el mando físico
        está pensado para quedarse encendido meses.

        La conexión se detecta con el aviso `al_conectar` del cliente, y no con la
        llegada del primer evento. La diferencia se vio al probarlo: el «canal
        conectado» apareció **41 segundos** después de arrancar, justo cuando pasó
        el primer evento. Con esa lógica un canal sano y callado —lo normal, si no
        hay llamadas— es indistinguible de uno caído, y el retroceso no se reinicia
        hasta que pase algo, así que una caída tras horas de silencio esperaría
        treinta segundos en lugar de uno.
        """
        espera = ESPERA_INICIAL
        conectado = False

        ausencia_registrada = False

        def al_conectar() -> None:
            nonlocal espera, conectado, ausencia_registrada
            if not conectado:
                logger.info("Canal de eventos del puente conectado")
                conectado = True
                ausencia_registrada = False
            espera = ESPERA_INICIAL

        while True:
            try:
                async for evento in self._telefonia.eventos(al_conectar=al_conectar):
                    self._al_llegar_evento(evento.tipo)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if conectado:
                    logger.info(f"Canal de eventos del puente caído ({e})")
                    conectado = False
                    ausencia_registrada = True
                elif not ausencia_registrada:
                    # El primer fallo de cada racha se registra aunque nunca se
                    # llegara a conectar. Sin esto, arrancar sin puente dejaba el
                    # log completamente mudo sobre telefonía, y quien depurara «por
                    # qué no me funciona el botón de autocontestar» no encontraría
                    # nada. Es el mismo criterio que `LectorDeBotones.abrir` aplica
                    # al device que todavía no está.
                    logger.info(f"Sin puente de telefonía ({e}); reintentando en segundo plano")
                    ausencia_registrada = True
                # Sin puente no hay llamadas, así que ningún modo de llamada puede
                # seguir en pie: dejarlo puesto haría que MUTE contestara a nadie.
                self._modo = Modo.NORMAL

            await asyncio.sleep(espera)
            espera = min(espera * 2, ESPERA_MAXIMA)

    def _al_llegar_evento(self, tipo: TipoEvento) -> None:
        """Cambia el modo según lo que diga el puente."""
        nuevo = MODO_POR_EVENTO.get(tipo)
        if nuevo is None or nuevo is self._modo:
            return
        logger.info(f"Modo {self._modo} -> {nuevo} (por {tipo})")
        self._modo = nuevo

    # -- Gobierno de servicios ------------------------------------------------

    def _unidad_de(self, verbo: Verbo) -> str:
        """Qué unidad toca cada verbo destructivo."""
        if verbo is Verbo.SOLO_TARJETA:
            return self._ajustes.unidad_telefonia
        return self._ajustes.unidad_agente

    def _testigo_de(self, unidad: str) -> Path | None:
        """Fichero que delata que una unidad está lista de verdad.

        Hace falta porque **para el agente, `active` de systemd no significa
        listo**. Su unidad la genera Quadlet con `--sdnotify=conmon`, así que
        systemd la da por activa en cuanto arranca el contenedor, no cuando el
        proceso de dentro ha terminado de cargar Whisper, Piper y el modelo de
        embeddings.

        Medido en la placa: systemd reportó `active` a las 00:54:50 y el agente no
        escribió su `estado_arranque.json` —que publica justo tras montar el
        pipeline— hasta las 00:55:14. **Veinticuatro segundos de diferencia.** Un
        arpegio de «ya puedes hablarle» sonando en el primer instante es
        exactamente el feedback que miente que este diseño quería evitar.

        El puente de telefonía no tiene ese problema: es un proceso nativo y
        `active` sí significa listo, así que no lleva testigo.
        """
        if unidad != self._ajustes.unidad_agente:
            return None
        return ruta_estado(self._ajustes.directorio_datos)

    async def _gobernar(self, verbo: Verbo, *, confirmado: bool) -> None:
        """Arranca, para o reinicia una unidad, pidiendo permiso si hace falta."""
        unidad = self._unidad_de(verbo)

        if unidad in self._en_vuelo:
            # Ya hay una orden sobre esta unidad cuyo resultado no se conoce.
            # Encolar otra solo produciría un reinicio doble.
            logger.warning(f"{unidad} ya tiene una orden en vuelo; ignoro {verbo}")
            self._pitidos.sonar("error")
            return

        estado = await self._servicios.estado(unidad)
        if estado is None:
            self._pitidos.sonar("error")
            return

        if verbo is Verbo.AGENTE_REINICIAR:
            accion, deja_activa, destructivo = "reiniciar", True, True
        else:
            # `AGENTE_ALTERNAR` y `SOLO_TARJETA` son interruptores: lo destructivo
            # es apagar, y arrancar es inocuo.
            apagar = estado.activo
            accion = "parar" if apagar else "arrancar"
            deja_activa = not apagar
            destructivo = apagar

        if destructivo and not confirmado:
            self._pedir_confirmacion(verbo)
            return

        await self._aplicar(unidad, accion, deja_activa=deja_activa)

    async def _aplicar(self, unidad: str, accion: str, *, deja_activa: bool) -> None:
        """Manda la orden, pita que se recibió y vigila el resultado de verdad."""
        metodo = {
            "arrancar": self._servicios.arrancar,
            "parar": self._servicios.parar,
            "reiniciar": self._servicios.reiniciar,
        }[accion]

        logger.info(f"{accion} {unidad}")
        if not await metodo(unidad):
            self._pitidos.sonar("error")
            return

        # Pitido inmediato de «recibido». El de «hecho» llega cuando la unidad
        # esté lista de verdad, que en el agente son unos 25 segundos.
        self._pitidos.sonar("si")
        self._en_vuelo.add(unidad)
        tarea = asyncio.create_task(self._seguir(unidad, deja_activa=deja_activa))
        self._seguimientos.add(tarea)
        tarea.add_done_callback(self._seguimientos.discard)

    async def _seguir(self, unidad: str, *, deja_activa: bool) -> None:
        """Espera el resultado real y lo anuncia."""
        try:
            llego = await self._servicios.esperar_estado(
                unidad,
                activa=deja_activa,
                testigo=self._testigo_de(unidad) if deja_activa else None,
            )
        finally:
            self._en_vuelo.discard(unidad)
        if not llego:
            self._pitidos.sonar("error")
            return
        # Arpegio al quedar en marcha, grave al quedar parada: el mismo idioma que
        # el micrófono, donde grave siempre significa «apagado».
        self._pitidos.sonar("listo" if deja_activa else "no")
        logger.info(f"{unidad} {'en marcha' if deja_activa else 'parada'}")

    # -- Reacciones -----------------------------------------------------------

    def _al_cruzar_nivel(self, _tecla: int, nivel: Nivel) -> None:
        """Pita al cruzar una frontera, con el botón todavía pulsado.

        Es lo que enseña la interfaz sola: oyes que vas por el nivel 2 y decides si
        sigues apretando hasta el 3 o sueltas.
        """
        pitido = PITIDO_DE_NIVEL.get(nivel)
        if pitido is not None:
            self._pitidos.sonar(pitido)

    def _al_reconectar(self) -> None:
        """Repara el estado tras una reenumeración del USB.

        Se olvida el gesto a medias —el instante de inicio ya no significa nada— y
        se vuelve a aplicar el silencio del micrófono, que la tarjeta ha perdido al
        volver con los valores por defecto del driver.
        """
        self._detector.olvidar()
        if self._micro_abierto is False:
            logger.info("La tarjeta ha vuelto; devuelvo el micrófono a silenciado")
            tarea = asyncio.get_running_loop().create_task(self._reaplicar_micro())
            self._seguimientos.add(tarea)
            tarea.add_done_callback(self._seguimientos.discard)

    async def _recordar_micro(self, intervalo: float | None = None) -> None:
        """Recuerda cada tantos minutos que el micrófono sigue silenciado.

        Existe porque **el agente no sabe que está mudo**: el silencio se hace en el
        mezclador de ALSA, así que el pipeline recibe bloques de ceros y no tiene
        nada que registrar. Desde fuera es indistinguible de un agente averiado —no
        contesta, y su log no dice nada— y en esta misma sesión el micrófono se
        quedó silenciado sin querer más de una vez.

        Se reutiliza el pitido `no`, que ya significa «micrófono silenciado». Un
        tono nuevo solo añadiría algo que memorizar para decir lo mismo.

        Args:
            intervalo: Segundos entre recordatorios. Por defecto, los minutos
                configurados. Los tests lo pasan en fracciones de segundo para no
                esperar de verdad.
        """
        segundos = (
            intervalo if intervalo is not None else self._ajustes.recordatorio_micro_minutos * 60
        )
        while True:
            await asyncio.sleep(segundos)
            if self._micro_abierto is False:
                logger.info("El micrófono sigue silenciado")
                self._pitidos.sonar("no")

    async def _reaplicar_micro(self) -> None:
        """Reaplica el último estado conocido del micrófono."""
        if await self._mezclador.fijar_micro(False) is None:
            self._pitidos.sonar("error")

    async def _recoger_tareas(self) -> None:
        """Cancela las tareas en vuelo al apagarse.

        Sin esto, apagar el demonio con un reinicio del agente a medias dejaría una
        tarea sondeando el bus y asyncio se quejaría de una tarea destruida y
        pendiente, que es un aviso que no dice nada de la causa.
        """
        self._olvidar_pendiente()
        for tarea in list(self._seguimientos):
            tarea.cancel()
        for tarea in list(self._seguimientos):
            with contextlib.suppress(asyncio.CancelledError):
                await tarea

    def _nombre_micro(self) -> str:
        """Describe el estado del micrófono para el log."""
        return "abierto" if self._micro_abierto else "silenciado"


__all__ = ["PITIDO_DE_NIVEL", "VERBOS_IMPLEMENTADOS", "Demonio"]
