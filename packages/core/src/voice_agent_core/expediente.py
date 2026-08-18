"""El expediente de una llamada: la fusión de lo que el agente dejó en dos sitios.

El seguimiento clínico de una llamada acaba repartido entre dos almacenes que no
se conocen. Por un lado los JSON de `evaluaciones.py` —las alertas de triaje y
el resumen, con los síntomas, la decisión, la cobertura y los documentos que de
verdad se consultaron—. Por otro la fila del historial SQLite de `historial.py`,
que es la única que sabe **de qué número** vino la llamada y si fue entrante o
una misión saliente. El panel enseñaba cada mitad en su propia pestaña, y por
eso las dos parecían decir lo mismo sin dejar navegar ninguna. Este módulo las
cruza por `id_llamada` y entrega un objeto por llamada.

El cruce es una **fusión externa completa**, y no es un capricho:
`numero_identificable()` rechaza el número oculto y los rellenos de las llamadas
de app, así que una llamada de navegador o de WhatsApp **no tiene fila** y solo
existe como JSON; y una llamada que se cortó antes de que corriera ninguna
herramienta tiene fila y no tiene JSON. Listar desde un solo lado perdería, en
silencio, una mitad distinta cada vez. De ahí también `SIN_FICHA`: sin ese
tercer valor de dirección, las entrantes más las misiones no sumarían el total
y la página parecería rota.

Las fechas se manejan como `date` y como **prefijo de cadena**, nunca como
`datetime`. Es deliberado: un `date` no puede llevar zona horaria, así que este
eje es inmune por construcción a la trampa que documenta `CLAUDE.md` con el
planificador. Y como todos los `momento` son ISO naive de ancho fijo,
`"2026-08-12" < "2026-08-12T09:00:00" < "2026-08-13"`: comparar cadenas **es**
comparar cronología, y no hace falta parsear ni un solo momento.

Vive en `core` por el mismo motivo que sus dos mitades: el agente escribe y el
panel lee, y el panel no puede importar `voice_agent`. Solo usa la stdlib,
pydantic y loguru; lo vigila `tests/test_core_liviano.py`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field

from voice_agent_core.cobertura import Cobertura
from voice_agent_core.evaluaciones import Alerta, NivelAlerta, ResumenLlamada
from voice_agent_core.historial import HistorialPacientes, LlamadaRegistrada
from voice_agent_core.rutas import dir_alertas, dir_resumenes, dir_trazas, ruta_historial

#: Cuántos JSON se abren como mucho por carpeta en una pasada. Es el freno que
#: hace que la vista sin acotar por fecha siga siendo utilizable en la placa:
#: el nombre del fichero lleva el día, así que la ventana de fechas poda **antes
#: de abrir nada** y esta constante solo entra en juego cuando no hay ventana.
TOPE_FICHEROS = 400

#: Cuántas filas del historial se traen como mucho. La tabla es pequeña y la
#: consulta va indexada; el tope está para que un fichero inesperadamente grande
#: no se coma la memoria del panel, no para ahorrar trabajo.
TOPE_FILAS = 1000

#: Los tamaños de página que se aceptan. Nada fuera de aquí: el parámetro viene
#: de la URL y un `limite=100000` sería una forma cómoda de tumbar la placa.
TOPES = (25, 50, 100, 200)
TOPE_DEFECTO = 50

#: Cuántas líneas de traza se pintan como mucho en el detalle, las últimas.
TOPE_LINEAS_TRAZA = 200

#: El valor de dirección que designa a las llamadas que no abrieron ficha.
SIN_FICHA = "sin_ficha"

#: Las direcciones por las que se puede filtrar, incluida la de las que no
#: tienen fila. Las dos primeras son las que escribe `registrar_llamada`.
DIRECCIONES = ("entrante", "mision", SIN_FICHA)

#: Un id de llamada nombra un fichero de traza y un segmento de URL. Se
#: comprueba aquí y no solo en el enrutador de Django porque estas funciones son
#: públicas y `core` no puede depender de Django.
PATRON_ID_LLAMADA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

#: El día que lleva dentro un id como `llamada-20260812-101530`.
_PATRON_DIA_DEL_ID = re.compile(r"(\d{8})-\d{6}")


# --- Lo que se entrega ---------------------------------------------------------


class Expediente(BaseModel):
    """Una llamada entera: su fila del historial, sus alertas y su resumen.

    Cualquiera de las dos mitades puede faltar, y eso es información, no un
    error: sin fila es una llamada que no abrió ficha (navegador, número oculto
    o app) y sin resumen es una que se cortó antes de despedirse.

    Attributes:
        id_llamada: La clave del cruce, la misma que llevan la traza, las
            alertas, el resumen y la fila.
        momento: Cuándo empezó la llamada, en ISO naive. Sale de la fila si la
            hay, porque es la que se escribe al montarla; si no, del artefacto
            más antiguo que se conserve.
        fila: La fila del historial, o `None` si el número no era identificable.
        alertas: Todas las alertas de triaje de la llamada, de la primera a la
            última.
        resumen: El resumen de cierre, o `None`.
        nombre: El nombre que el historial tenía para ese número, o vacío.
    """

    id_llamada: str
    momento: str
    fila: LlamadaRegistrada | None = None
    alertas: list[Alerta] = Field(default_factory=list)
    resumen: ResumenLlamada | None = None
    nombre: str = ""

    @property
    def tiene_ficha(self) -> bool:
        """Si la llamada llegó a abrir ficha de paciente."""
        return self.fila is not None

    @property
    def numero(self) -> str:
        """El número del otro extremo, o vacío si no hubo ficha."""
        return self.fila.numero if self.fila else ""

    @property
    def direccion(self) -> str:
        """Cómo entró la llamada: entrante, mision, o `SIN_FICHA` si no hubo ficha."""
        return self.fila.direccion if self.fila else SIN_FICHA

    @property
    def nivel(self) -> str:
        """El triaje efectivo de la llamada.

        Manda la fila, que es lo que el agente consulta al descolgar la
        siguiente; si viene vacía, rescata el resumen y después la última
        alerta. El rescate importa: la anotación en SQLite se hace bajo el
        `except` de la casa, así que puede haberse perdido sin ruido mientras el
        JSON de la alerta —escrito antes y aparte— sí conserva el color.
        """
        return _primero_no_vacio(
            self.fila.nivel if self.fila else "",
            self.resumen.nivel if self.resumen else "",
            self.alertas[-1].nivel if self.alertas else "",
        )

    @property
    def procedimiento(self) -> str:
        """La cirugía, con la misma precedencia y por el mismo motivo que `nivel`."""
        return _primero_no_vacio(
            self.fila.procedimiento if self.fila else "",
            self.resumen.procedimiento if self.resumen else "",
            self.alertas[-1].procedimiento if self.alertas else "",
        )

    @property
    def cobertura(self) -> str:
        """Cómo quedó la puerta de cobertura. Solo consta en los JSON."""
        return _primero_no_vacio(
            self.resumen.cobertura if self.resumen else "",
            self.alertas[-1].cobertura if self.alertas else "",
        )

    @property
    def es_respaldo(self) -> bool:
        """Si el resumen lo escribió `respaldo.py` porque la llamada se cayó."""
        return bool(self.resumen and self.resumen.transcripcion)

    @property
    def dia(self) -> str:
        """El día de la llamada, `AAAA-MM-DD`."""
        return self.momento[:10]


class CriteriosExpedientes(BaseModel):
    """Por qué se puede acotar el listado.

    Los ejes se combinan en Y. Los cuatro «categorías» —triaje, procedimiento,
    cobertura y dirección— más la fecha y el paciente.
    """

    desde: date | None = None
    hasta: date | None = None
    nivel: NivelAlerta | None = None
    procedimiento: str = ""
    cobertura: Cobertura | None = None
    direccion: str = ""
    numero: str = ""
    tope: int = TOPE_DEFECTO

    @classmethod
    def desde_parametros(
        cls, parametros: Mapping[str, str]
    ) -> tuple[CriteriosExpedientes, list[str]]:
        """Lee los criterios de una cadena de consulta; esto nunca lanza.

        Un filtro que no se entiende se ignora y se avisa: la página tiene que
        salir igual. Devolver un 400 por un `nivel=azul` escrito a mano en la
        URL dejaría a quien mira el panel delante de un error de Django.

        Returns:
            Los criterios y la lista de avisos que hay que enseñar.
        """
        avisos: list[str] = []
        desde = _fecha(parametros.get("desde", ""), "desde", avisos)
        hasta = _fecha(parametros.get("hasta", ""), "hasta", avisos)
        if desde and hasta and desde > hasta:
            desde, hasta = hasta, desde
            avisos.append("El rango de fechas iba al revés; se ha enderezado.")

        direccion = parametros.get("direccion", "").strip()
        if direccion and direccion not in DIRECCIONES:
            avisos.append(f"La dirección «{direccion}» no existe; se ignora ese filtro.")
            direccion = ""

        tope = TOPE_DEFECTO
        try:
            pedido = int(parametros.get("limite", ""))
        except ValueError:
            pedido = 0
        if pedido in TOPES:
            tope = pedido

        criterios = cls(
            desde=desde,
            hasta=hasta,
            nivel=_opcion(parametros.get("nivel", ""), NivelAlerta, "triaje", avisos),
            procedimiento=parametros.get("procedimiento", "").strip(),
            cobertura=_opcion(parametros.get("cobertura", ""), Cobertura, "cobertura", avisos),
            direccion=direccion,
            numero=parametros.get("numero", "").strip(),
            tope=tope,
        )
        return criterios, avisos

    def activos(self) -> bool:
        """Si hay algún filtro puesto. El tamaño de página no cuenta."""
        return any(
            (
                self.desde,
                self.hasta,
                self.nivel,
                self.procedimiento,
                self.cobertura,
                self.direccion,
                self.numero,
            )
        )


class ResultadoExpedientes(BaseModel):
    """Lo que devuelve un listado, con la honestidad de lo que dejó fuera.

    Attributes:
        expedientes: Los que cumplen los criterios, del más reciente al más
            antiguo.
        truncado: Si se dejó algo fuera.
        motivo_truncado: "tope" si solo sobraban resultados para esta página;
            "lectura" si se llegó al tope de ficheros o de filas, que es más
            grave porque puede faltar algo antiguo.
        ficheros_examinados: Cuántos JSON se abrieron de verdad. Se enseña: es
            lo que deja ver al usuario que acotar por fecha sirve para algo.
    """

    expedientes: list[Expediente]
    truncado: bool = False
    motivo_truncado: str = ""
    ficheros_examinados: int = 0


class PasajeTraza(BaseModel):
    """Un pasaje que el RAG devolvió en una consulta."""

    origen: str = ""
    tema: str = ""
    distancia: float = 0.0


class LineaTraza(BaseModel):
    """Una consulta al RAG y lo que devolvió, tal y como la dejó `traza.py`."""

    momento: str = ""
    consulta: str = ""
    motivo: str = ""
    pasajes: list[PasajeTraza] = Field(default_factory=list)


class OpcionPaciente(BaseModel):
    """Un paciente tal y como se ofrece en el desplegable del filtro."""

    numero: str
    nombre: str = ""
    total_llamadas: int = 0


class OpcionesFiltro(BaseModel):
    """Los vocabularios que dependen de los datos y hay que descubrir."""

    procedimientos: list[str] = Field(default_factory=list)
    pacientes: list[OpcionPaciente] = Field(default_factory=list)


# --- La API pública ------------------------------------------------------------


def listar_expedientes(
    data_dir: Path,
    criterios: CriteriosExpedientes,
    *,
    tope_ficheros: int = TOPE_FICHEROS,
) -> ResultadoExpedientes:
    """Los expedientes que cumplen los criterios, del más reciente al más antiguo.

    A SQL se le empujan **solo** el número, la dirección y las fechas: son los
    tres campos que existen únicamente en el historial, así que empujarlos no
    puede perder nada. El triaje y el procedimiento se filtran después, sobre el
    expediente ya fusionado, porque también viven en los JSON (ver `nivel`).

    Nunca lanza: un disco ilegible o un JSON a medias degradan a menos
    resultados, nunca a una excepción.
    """
    historial = HistorialPacientes(ruta_historial(data_dir))
    direccion_sql = criterios.direccion if criterios.direccion in ("entrante", "mision") else ""
    filas = historial.buscar_llamadas(
        numero=criterios.numero,
        direccion=direccion_sql,
        desde=criterios.desde.isoformat() if criterios.desde else "",
        hasta_exclusivo=(
            (criterios.hasta + timedelta(days=1)).isoformat() if criterios.hasta else ""
        ),
        limite=TOPE_FILAS,
    )

    # Cuando se filtra por algo que solo vive en SQL, las filas ya son la lista
    # completa de candidatos: la ventana de ficheros se puede estrechar a los
    # días en los que de verdad hubo llamadas, y el escaneo casi desaparece.
    ids_permitidos: set[str] | None = None
    desde_ventana, hasta_ventana = criterios.desde, criterios.hasta
    if criterios.numero or direccion_sql:
        if not filas:
            return ResultadoExpedientes(expedientes=[])
        ids_permitidos = {f.id_llamada for f in filas}
        dias = sorted(f.momento[:10] for f in filas)
        desde_ventana = _dia(dias[0]) or desde_ventana
        hasta_ventana = _dia(dias[-1]) or hasta_ventana

    acumulados, examinados, tope_agotado = _acumular(
        data_dir, desde_ventana, hasta_ventana, tope_ficheros
    )

    por_id = {f.id_llamada: f for f in filas}
    identificadores = set(por_id) | set(acumulados)
    if ids_permitidos is not None:
        identificadores &= ids_permitidos
    nombres = historial.nombres()

    expedientes: list[Expediente] = []
    for identificador in identificadores:
        expediente = _construir(
            identificador, por_id.get(identificador), acumulados.get(identificador), nombres
        )
        if _pasa_el_filtro(expediente, criterios):
            expedientes.append(expediente)
    expedientes.sort(key=lambda e: (e.momento, e.id_llamada), reverse=True)

    sobran = len(expedientes) > criterios.tope
    agotado = tope_agotado or len(filas) >= TOPE_FILAS
    return ResultadoExpedientes(
        expedientes=expedientes[: criterios.tope],
        truncado=sobran or agotado,
        motivo_truncado="lectura" if agotado else ("tope" if sobran else ""),
        ficheros_examinados=examinados,
    )


def leer_expediente(data_dir: Path, id_llamada: str) -> Expediente | None:
    """El expediente de una llamada concreta, o `None` si no consta en ningún sitio.

    Aquí no hay ventana de fechas que ayude, así que se deriva del propio dato:
    del momento de la fila si la hay, y si no del día que el id lleva dentro
    (`llamada-AAAAMMDD-HHMMSS`). Solo cuando no se puede derivar nada se cae al
    escaneo acotado.
    """
    if not PATRON_ID_LLAMADA.match(id_llamada):
        return None
    historial = HistorialPacientes(ruta_historial(data_dir))
    fila = historial.llamada(id_llamada)
    dia = _dia(fila.momento[:10]) if fila else _dia_del_id(id_llamada)

    acumulados, _, _ = _acumular(data_dir, dia, dia, TOPE_FICHEROS)
    acumulado = acumulados.get(id_llamada)
    if acumulado is None and dia is not None:
        # La ventana derivada pudo quedarse corta (un id reutilizado como
        # `sin-traza`, o un artefacto reescrito días después): se reintenta sin
        # acotar antes de dar la llamada por inexistente.
        acumulados, _, _ = _acumular(data_dir, None, None, TOPE_FICHEROS)
        acumulado = acumulados.get(id_llamada)
    if fila is None and acumulado is None:
        return None
    return _construir(id_llamada, fila, acumulado, historial.nombres())


def leer_traza(data_dir: Path, id_llamada: str) -> list[LineaTraza]:
    """La traza documental de una llamada: qué se le preguntó al RAG y qué devolvió.

    El JSONL lo escribe `traza.py` con `append` a propósito, así que un corte de
    luz deja media línea al final. Esa se salta; las anteriores, que son la
    prueba de trazabilidad, se conservan.
    """
    if not PATRON_ID_LLAMADA.match(id_llamada):
        return []
    ruta = dir_trazas(data_dir) / f"{id_llamada}.jsonl"
    lineas: list[LineaTraza] = []
    try:
        with ruta.open(encoding="utf-8") as fichero:
            for cruda in fichero:
                limpia = cruda.strip()
                if not limpia:
                    continue
                try:
                    lineas.append(LineaTraza.model_validate_json(limpia))
                except ValueError:
                    logger.warning(f"[expediente] línea ilegible en la traza {ruta.name}")
    except FileNotFoundError:
        return []
    except OSError as e:
        logger.error(f"[expediente] no pude leer la traza {ruta}: {e}")
        return []
    return lineas[-TOPE_LINEAS_TRAZA:]


def opciones_de_filtro(data_dir: Path, vistos: Iterable[str] = ()) -> OpcionesFiltro:
    """Los vocabularios que hay que descubrir en los datos para poblar el filtro.

    Los procedimientos salen del historial **unidos** a los de los expedientes
    que la página acaba de cargar: una cirugía que solo consta en el JSON de una
    llamada de navegador no tiene fila, y sin esta unión no se podría filtrar
    por ella. Lo que no se hace es abrir todos los JSON del disco solo para
    poblar un desplegable: eso reintroduciría el escaneo que este diseño evita.

    Nivel, cobertura y dirección no están aquí: su vocabulario es del código
    (`NivelAlerta`, `Cobertura`, `DIRECCIONES`), no de los datos.
    """
    historial = HistorialPacientes(ruta_historial(data_dir))
    procedimientos: dict[str, str] = {}
    for crudo in (*historial.procedimientos(), *vistos):
        limpio = crudo.strip()
        if limpio:
            procedimientos.setdefault(limpio.casefold(), limpio)
    return OpcionesFiltro(
        procedimientos=[procedimientos[clave] for clave in sorted(procedimientos)],
        pacientes=[
            OpcionPaciente(
                numero=ficha.numero, nombre=ficha.nombre, total_llamadas=ficha.total_llamadas
            )
            for ficha in historial.pacientes()
        ],
    )


# --- Las tripas ----------------------------------------------------------------


@dataclass
class _Acumulado:
    """Los artefactos JSON de una llamada, mientras se recogen."""

    alertas: list[Alerta] = field(default_factory=list)
    resumen: ResumenLlamada | None = None
    respaldo: ResumenLlamada | None = None


def _primero_no_vacio(*candidatos: str) -> str:
    return next((str(c) for c in candidatos if c), "")


def _fecha(texto: str, cual: str, avisos: list[str]) -> date | None:
    limpio = texto.strip()
    if not limpio:
        return None
    try:
        return date.fromisoformat(limpio)
    except ValueError:
        avisos.append(f"La fecha «{limpio}» de «{cual}» no se entiende; se ignora ese filtro.")
        return None


def _opcion[T: StrEnum](texto: str, vocabulario: type[T], cual: str, avisos: list[str]) -> T | None:
    limpio = texto.strip()
    if not limpio:
        return None
    try:
        return vocabulario(limpio)
    except ValueError:
        avisos.append(f"El valor «{limpio}» de «{cual}» no existe; se ignora ese filtro.")
        return None


def _dia(texto: str) -> date | None:
    try:
        return date.fromisoformat(texto)
    except ValueError:
        return None


def _dia_del_id(id_llamada: str) -> date | None:
    """El día que lleva dentro `llamada-AAAAMMDD-HHMMSS`, si lo lleva."""
    hallazgo = _PATRON_DIA_DEL_ID.search(id_llamada)
    if not hallazgo:
        return None
    crudo = hallazgo.group(1)
    return _dia(f"{crudo[:4]}-{crudo[4:6]}-{crudo[6:]}")


def _ficheros_en_ventana(
    carpeta: Path, desde: date | None, hasta: date | None, tope: int
) -> tuple[list[Path], bool]:
    """Los JSON cuyo nombre cae en la ventana, del más nuevo al más viejo.

    El nombre es `%Y%m%d-%H%M%S.json`, así que sus ocho primeros caracteres son
    el día: se puede podar **sin abrir el fichero**, que es la mitad cara en
    esta placa. La ventana llega con un día de holgura a cada lado, porque el
    nombre lleva el instante en que se escribió el artefacto y el momento del
    expediente lleva el instante en que se montó la llamada: una llamada de las
    23:58 deja su resumen en el fichero del día siguiente.

    Returns:
        Los ficheros y si se llegó al tope (es decir, si pudo quedar algo fuera).
    """
    if not carpeta.is_dir():
        return [], False
    marca_desde = f"{desde - timedelta(days=1):%Y%m%d}" if desde else ""
    marca_hasta = f"{hasta + timedelta(days=1):%Y%m%d}" if hasta else ""
    elegidos: list[Path] = []
    try:
        candidatos = list(carpeta.iterdir())
    except OSError as e:
        logger.error(f"[expediente] no pude listar {carpeta}: {e}")
        return [], False
    for fichero in candidatos:
        if fichero.suffix != ".json":
            continue
        marca = fichero.name[:8]
        # Un nombre sin fecha (`roto.json`) entra siempre: por el nombre no se
        # puede decidir, y ya lo descartará su momento real o su validación.
        if marca.isdigit() and (
            (marca_desde and marca < marca_desde) or (marca_hasta and marca > marca_hasta)
        ):
            continue
        elegidos.append(fichero)
    elegidos.sort(key=lambda f: f.name, reverse=True)
    return elegidos[:tope], len(elegidos) > tope


def _acumular(
    data_dir: Path, desde: date | None, hasta: date | None, tope: int
) -> tuple[dict[str, _Acumulado], int, bool]:
    """Lee y agrupa por `id_llamada` los JSON de la ventana.

    Returns:
        Los acumulados, cuántos ficheros se abrieron y si se agotó algún tope.
    """
    acumulados: dict[str, _Acumulado] = {}
    examinados = 0
    agotado = False

    alertas, corte = _ficheros_en_ventana(dir_alertas(data_dir), desde, hasta, tope)
    agotado = agotado or corte
    for fichero in alertas:
        examinados += 1
        alerta = _validar(fichero, Alerta)
        if alerta is not None:
            acumulados.setdefault(alerta.id_llamada, _Acumulado()).alertas.append(alerta)

    resumenes, corte = _ficheros_en_ventana(dir_resumenes(data_dir), desde, hasta, tope)
    agotado = agotado or corte
    for fichero in resumenes:
        examinados += 1
        resumen = _validar(fichero, ResumenLlamada)
        if resumen is None:
            continue
        acumulado = acumulados.setdefault(resumen.id_llamada, _Acumulado())
        # Una llamada que se cae deja resumen de respaldo, y si más tarde se
        # rehace deja el normal: manda el normal, que lo escribió el modelo.
        if fichero.stem.endswith("-respaldo"):
            acumulado.respaldo = acumulado.respaldo or resumen
        else:
            acumulado.resumen = acumulado.resumen or resumen

    for acumulado in acumulados.values():
        acumulado.alertas.sort(key=lambda a: a.momento)
    return acumulados, examinados, agotado


def _validar[M: BaseModel](fichero: Path, modelo: type[M]) -> M | None:
    """Lee un JSON y lo valida, tolerando ficheros a medias e inventados."""
    try:
        return modelo.model_validate_json(fichero.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(f"[expediente] me salto {fichero.name}: {e}")
        return None


def _construir(
    id_llamada: str,
    fila: LlamadaRegistrada | None,
    acumulado: _Acumulado | None,
    nombres: Mapping[str, str],
) -> Expediente:
    """Cose las dos mitades en un expediente."""
    acumulado = acumulado or _Acumulado()
    resumen = acumulado.resumen or acumulado.respaldo
    if fila is not None:
        momento = fila.momento
    else:
        candidatos = [a.momento for a in acumulado.alertas]
        if resumen is not None:
            candidatos.append(resumen.momento)
        momento = min(candidatos) if candidatos else ""
    return Expediente(
        id_llamada=id_llamada,
        momento=momento,
        fila=fila,
        alertas=acumulado.alertas,
        resumen=resumen,
        nombre=nombres.get(fila.numero, "") if fila else "",
    )


def _pasa_el_filtro(expediente: Expediente, criterios: CriteriosExpedientes) -> bool:
    """Aplica los ejes que no se pudieron empujar a SQL, sobre el ya fusionado."""
    dia = expediente.dia
    if criterios.desde and dia < criterios.desde.isoformat():
        return False
    if criterios.hasta and dia > criterios.hasta.isoformat():
        return False
    if criterios.nivel and expediente.nivel != criterios.nivel:
        return False
    if criterios.cobertura and expediente.cobertura != criterios.cobertura:
        return False
    if criterios.direccion and expediente.direccion != criterios.direccion:
        return False
    if criterios.numero and expediente.numero != criterios.numero:
        return False
    return not (
        criterios.procedimiento
        and expediente.procedimiento.casefold() != criterios.procedimiento.casefold()
    )


__all__ = [
    "DIRECCIONES",
    "SIN_FICHA",
    "TOPES",
    "TOPE_DEFECTO",
    "TOPE_FICHEROS",
    "TOPE_LINEAS_TRAZA",
    "CriteriosExpedientes",
    "Expediente",
    "LineaTraza",
    "OpcionPaciente",
    "OpcionesFiltro",
    "PasajeTraza",
    "ResultadoExpedientes",
    "leer_expediente",
    "leer_traza",
    "listar_expedientes",
    "opciones_de_filtro",
]
