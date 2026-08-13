"""Indexado del corpus en ChromaDB.

Recorre `corpus/`, trocea cada documento, calcula sus embeddings y los guarda en
la colección de su tema. Se ejecuta fuera de línea::

    make ingest      # reconcilia el índice con el corpus
    make reingest    # lo reconstruye desde cero

La ingesta **reconcilia**, no acumula. Por cada tema —cada subcarpeta de
`corpus/`, más los documentos sueltos de la raíz— hace tres cosas:

1. Vuelca sus documentos en la colección del tema con `upsert`. El identificador
   de cada fragmento se deriva de la ruta, el número de fragmento y un resumen
   criptográfico del contenido, así que volver a ejecutarla sobre un corpus sin
   cambios no duplica nada.
2. **Borra de esa colección los identificadores que ya no ha escrito.** Son los
   fragmentos de documentos borrados o de versiones anteriores de uno editado.
3. Al terminar, elimina las colecciones de los temas cuya carpeta ya no está.

Los pasos 2 y 3 son la razón de repartir el índice por temas. Antes, con una
sola colección, la ingesta incremental no podía olvidar un documento borrado
—ya no hay nada que recorrer— y la única salida era reconstruirlo todo. Ahora
cada tema se compara contra su propia carpeta, que sí se puede recorrer entera.

## Lo que NO se vuelve a hacer

Antes de tocar nada, la ingesta **explora**: calcula la huella de cada fichero
del corpus y le pregunta a la colección de su tema con qué huella indexó ese
mismo documento. Si coinciden y el número de fragmentos cuadra, el documento no
se abre siquiera.

Esto no es una optimización de manual. Con el corpus clínico —106 PDF, 10 157
fragmentos— la pasada anterior tardaba **cerca de una hora** aunque no hubiera
cambiado nada, porque para saber si un fragmento ya estaba había que extraer el
texto del PDF y trocearlo: el filtro por identificador ahorraba los embeddings,
no la lectura, y en esta placa la lectura es la mitad cara. Con la huella, una
reindexación sin cambios se va en segundos y añadir un documento cuesta lo que
cuesta ese documento.

La huella resume el contenido del fichero **y la receta con la que se troceó**
(`EMBEDDING_MODEL`, `CHUNK_SIZE`, `CHUNK_OVERLAP`), así que cambiar el tamaño de
fragmento ya no puede dejar el índice viejo en silencio: cambia la huella de
todo y todo se reprocesa. `--reset` sigue haciendo falta para cambiar de modelo
de embeddings, porque eso además invalida el índice HNSW que ChromaDB creó con
la función anterior.

Un índice construido antes de que existieran las huellas se reconoce porque a
sus fragmentos les falta el metadato: la primera pasada los relee (una vez) y
les remienda los metadatos **sin recalcular embeddings**, con `update`.

El avance se publica en `<DATA_DIR>/ingesta/progreso.json` para que la página de
Conocimiento del panel pinte la barra. Ver `voice_agent_core.ingesta`.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from chromadb.api import ClientAPI
from chromadb.api.models.Collection import Collection
from loguru import logger

from voice_agent.logging import setup_logging
from voice_agent.rag.chunking import trocear
from voice_agent.rag.store import abrir_cliente, abrir_coleccion, borrar_coleccion, temas_indexados
from voice_agent_core.config import Settings, get_settings
from voice_agent_core.corpus import (
    EXTENSIONES_SOPORTADAS,
    TEMA_RAIZ,
    Documento,
    documentos_ignorados,
    listar_documentos,
    listar_temas,
)
from voice_agent_core.ingesta import FaseIngesta, ProgresoIngesta, escribir_progreso

# Se sube en lotes para no cargar todos los embeddings en memoria a la vez ni
# hacer una llamada por fragmento.
TAMANO_LOTE = 32

#: Cuánto de un sha256 se guarda como huella. Diez y seis caracteres hexadecimales
#: son 64 bits: de sobra para distinguir versiones de un mismo documento sin
#: engordar los metadatos de diez mil fragmentos.
LONGITUD_HUELLA = 16

#: Bloque con el que se lee un fichero para resumirlo. Un mega no se nota en
#: memoria y evita mil llamadas por PDF.
BLOQUE_HUELLA = 1024 * 1024


def leer_documento(ruta: Path) -> str:
    """Extrae el texto de un documento del corpus.

    Args:
        ruta: Fichero a leer.

    Returns:
        Su contenido como texto plano. Cadena vacía si no se pudo extraer nada.
    """
    if ruta.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        lector = PdfReader(str(ruta))
        return "\n\n".join((pagina.extract_text() or "") for pagina in lector.pages)

    return ruta.read_text(encoding="utf-8", errors="replace")


def extraer_titulo(texto: str, ruta: Path) -> str:
    """Deduce el título de un documento.

    Args:
        texto: Contenido del documento.
        ruta: Su ruta, usada como respaldo.

    Returns:
        El primer encabezado de nivel 1 en Markdown, o si no lo hay, el nombre
        del fichero convertido a algo legible.
    """
    for linea in texto.splitlines():
        if linea.startswith("# "):
            return linea[2:].strip()
    return ruta.stem.replace("-", " ").replace("_", " ").capitalize()


def contextualizar(fragmento: str, titulo: str) -> str:
    """Antepone el título del documento al fragmento antes de indexarlo.

    Un fragmento tomado de la mitad de un documento pierde el contexto que lo
    hacía identificable. Por ejemplo, el párrafo que explica cómo se empaqueta
    el proyecto no contiene en ningún sitio la palabra "agente", así que el
    vector de la pregunta "¿cómo se despliega el agente?" no acaba
    suficientemente cerca del suyo.

    Anteponer el título del documento a cada fragmento —una técnica conocida
    como *troceado contextual*— arregla eso a coste cero: el vector pasa a
    codificar también de qué documento habla, y el modelo de lenguaje que luego
    lo lee sabe de dónde salió.

    El **tema no se añade aquí a propósito**. Viaja como metadato y como prefijo
    de `origen`, pero no entra en el texto que se vectoriza: el umbral de
    `RAG_MAX_DISTANCE` está medido sobre exactamente esta cadena (ver la tabla de
    calibración de `docs/rag.md`), y meter el tema dentro obligaría a repetir la
    medición entera para no dejarlo desfasado en silencio.

    Args:
        fragmento: Texto del fragmento.
        titulo: Título del documento de origen.

    Returns:
        El texto que realmente se indexará.
    """
    return f"{titulo}\n\n{fragmento}"


def _identificador(origen: str, indice: int, texto: str) -> str:
    """Construye el identificador estable de un fragmento.

    Incluir el resumen del contenido es lo que hace que editar un documento
    sustituya sus fragmentos en vez de acumular versiones viejas.

    Args:
        origen: Ruta relativa del documento.
        indice: Número de fragmento dentro del documento.
        texto: Contenido del fragmento.

    Returns:
        Un identificador determinista.
    """
    resumen = hashlib.sha256(texto.encode("utf-8")).hexdigest()[:16]
    return f"{origen}::{indice}::{resumen}"


def _huella(ruta: Path, settings: Settings) -> str:
    """Resume un documento junto con la receta con la que se trocearía.

    La receta —modelo de embeddings, tamaño de fragmento y solape— entra en el
    resumen a propósito: así, cambiar `CHUNK_SIZE` no puede dejar en el índice
    fragmentos troceados con el valor anterior sin que nadie se entere.

    Args:
        ruta: Fichero del corpus.
        settings: Configuración del agente.

    Returns:
        Un resumen corto y determinista. Si coincide con el que quedó guardado
        en los metadatos, el documento ni se abre.
    """
    resumen = hashlib.sha256()
    receta = f"{settings.embedding_model}|{settings.chunk_size}|{settings.chunk_overlap}"
    resumen.update(receta.encode("utf-8"))
    resumen.update(b"\0")
    with ruta.open("rb") as fichero:
        for bloque in iter(lambda: fichero.read(BLOQUE_HUELLA), b""):
            resumen.update(bloque)
    return resumen.hexdigest()[:LONGITUD_HUELLA]


class _Publicador:
    """Publica el avance de la reindexación donde el panel lo lee.

    Lleva un freno de medio segundo porque un documento pequeño se despacha en
    milisegundos y no tiene sentido reescribir el fichero por cada uno. Los
    hitos que sí importan —empezar un documento largo, cambiar de fase,
    terminar— se publican con `forzar`, que es justo lo que hace que la barra no
    se quede parada media hora en un PDF de cien páginas.
    """

    #: Mínimo entre dos escrituras no forzadas.
    INTERVALO = 0.5

    def __init__(self, data_dir: Path) -> None:
        """Arranca el progreso y lo publica ya, para que el panel tenga qué pintar."""
        ahora = datetime.now()
        self.progreso = ProgresoIngesta(iniciada_en=ahora, actualizada_en=ahora)
        self._data_dir = data_dir
        self._ultima = 0.0
        self.publicar(forzar=True)

    def publicar(self, *, forzar: bool = False) -> None:
        """Escribe el progreso, salvo que acabe de escribirlo y no sea urgente."""
        ahora = time.monotonic()
        if not forzar and ahora - self._ultima < self.INTERVALO:
            return
        self._ultima = ahora
        self.progreso.actualizada_en = datetime.now()
        escribir_progreso(self._data_dir, self.progreso)

    def fallar(self, motivo: str) -> None:
        """Deja constancia de que la reindexación se fue al traste."""
        self.progreso.fase = FaseIngesta.ERROR
        self.progreso.error = motivo
        self.publicar(forzar=True)


@dataclass
class _DocumentoPrevio:
    """Lo que la colección ya tiene guardado de un documento.

    Attributes:
        ids: Identificadores de sus fragmentos.
        huellas: Las huellas con las que se escribieron. Con un índice sano es
            un conjunto de un solo elemento; más de uno delata una pasada
            interrumpida a mitad de documento.
        fragmentos: Cuántos fragmentos dijo tener el documento al indexarse.
    """

    ids: list[str] = field(default_factory=list)
    huellas: set[str] = field(default_factory=set)
    fragmentos: int = 0

    def coincide(self, huella: str) -> bool:
        """Si lo indexado es exactamente este documento y está entero.

        Que la huella cuadre no basta: una ingesta interrumpida deja el
        documento a medias con la huella correcta en los fragmentos que sí
        llegaron a escribirse. Por eso se compara también el número.
        """
        return self.huellas == {huella} and self.fragmentos == len(self.ids)


@dataclass
class _PlanTema:
    """Lo que hay que hacer con un tema, decidido antes de abrir un solo PDF.

    Attributes:
        tema: El tema, o `TEMA_RAIZ`.
        coleccion: Su colección de ChromaDB.
        pendientes: Los documentos que hay que leer, trocear y vectorizar.
        conservados: Identificadores de los documentos que no han cambiado.
        huellas: Huella recién calculada de cada documento pendiente.
        huella_previa: Huella con la que está guardado cada fragmento que ya
            existe en la colección; vacía si se indexó antes de las huellas.
    """

    tema: str
    coleccion: Collection
    pendientes: list[Documento] = field(default_factory=list)
    conservados: list[str] = field(default_factory=list)
    huellas: dict[str, str] = field(default_factory=dict)
    huella_previa: dict[str, str] = field(default_factory=dict)

    @property
    def etiqueta(self) -> str:
        """Nombre del tema para los logs."""
        return self.tema or "(raíz)"


def _indice_previo(coleccion: Collection) -> tuple[dict[str, str], dict[str, _DocumentoPrevio]]:
    """Lee de una colección qué documentos tiene y con qué huella.

    Es **una** consulta por tema y sin embeddings: `get` sin `query` no vectoriza
    nada, solo lee el SQLite de ChromaDB.

    Args:
        coleccion: La colección del tema.

    Returns:
        La huella de cada fragmento por identificador, y lo que hay de cada
        documento por su ruta relativa.
    """
    datos = coleccion.get(include=["metadatas"])
    ids = list(datos["ids"])
    metadatos = list(datos.get("metadatas") or [])

    huella_de_id: dict[str, str] = {}
    previos: dict[str, _DocumentoPrevio] = {}
    for posicion, identificador in enumerate(ids):
        # Por índice y no con `zip`: si por lo que sea vinieran menos metadatos
        # que ids, un `zip` se comería los ids sobrantes y `_olvidar_sobrantes`
        # los daría por basura y los borraría.
        meta = metadatos[posicion] if posicion < len(metadatos) else None
        meta = meta or {}
        huella = str(meta.get("huella", ""))
        huella_de_id[identificador] = huella
        previo = previos.setdefault(str(meta.get("origen", "")), _DocumentoPrevio())
        previo.ids.append(identificador)
        previo.huellas.add(huella)
        # Un metadato de Chroma puede ser de varios tipos; el que no sea un
        # entero es de otra época y vale como "no lo sé", que reprocesa.
        cuantos = meta.get("fragmentos", 0)
        previo.fragmentos = max(previo.fragmentos, cuantos if isinstance(cuantos, int) else 0)
    return huella_de_id, previos


def _planear_tema(
    settings: Settings,
    cliente: ClientAPI,
    tema: str,
    documentos: list[Documento],
    progreso: ProgresoIngesta,
) -> _PlanTema:
    """Decide qué documentos de un tema hay que reprocesar, sin leer ninguno.

    Args:
        settings: Configuración del agente.
        cliente: Cliente de ChromaDB ya abierto.
        tema: El tema a planear.
        documentos: Sus documentos en disco.
        progreso: Contadores que se van rellenando para la barra del panel.

    Returns:
        El plan del tema.
    """
    coleccion = abrir_coleccion(settings, tema, cliente=cliente)
    huella_de_id, previos = _indice_previo(coleccion)
    plan = _PlanTema(tema=tema, coleccion=coleccion, huella_previa=huella_de_id)

    for documento in documentos:
        origen = documento.ruta_relativa
        huella = _huella(settings.corpus_dir / origen, settings)
        plan.huellas[origen] = huella
        previo = previos.get(origen)
        if previo is not None and previo.coincide(huella):
            plan.conservados.extend(previo.ids)
            progreso.documentos_sin_cambios += 1
        else:
            plan.pendientes.append(documento)
            progreso.documentos_pendientes += 1
        progreso.documentos_total += 1
    return plan


def _indexar_documento(
    settings: Settings, plan: _PlanTema, documento: Documento, progreso: ProgresoIngesta
) -> list[str]:
    """Lee, trocea e indexa un documento.

    Args:
        settings: Configuración del agente.
        plan: El plan de su tema, de donde salen la colección y la huella.
        documento: El documento a indexar.
        progreso: Contadores para la barra del panel.

    Returns:
        Los identificadores que deben quedar en la colección para este
        documento. Vacío si no había nada que indexar.
    """
    origen = documento.ruta_relativa
    ruta = settings.corpus_dir / origen
    texto = leer_documento(ruta)
    if not texto.strip():
        logger.warning(f"  {origen}: vacío o ilegible, se omite")
        return []

    fragmentos = trocear(texto, tamano=settings.chunk_size, solape=settings.chunk_overlap)
    if not fragmentos:
        return []

    titulo = extraer_titulo(texto, ruta)
    huella = plan.huellas[origen]
    textos = [contextualizar(f.texto, titulo) for f in fragmentos]
    ids = [_identificador(origen, f.indice, t) for f, t in zip(fragmentos, textos, strict=True)]
    metadatos: list[dict[str, str | int]] = [
        {
            "origen": origen,
            "titulo": titulo,
            "tema": plan.tema,
            "fragmento": f.indice,
            "huella": huella,
            "fragmentos": len(fragmentos),
        }
        for f in fragmentos
    ]

    # Que el id ya exista significa que el fragmento es literalmente el mismo
    # texto —el id lleva un resumen del contenido—, así que no hay que volver a
    # vectorizarlo. Y si su huella no cuadra es un fragmento de antes de que
    # existieran las huellas: se le remienda el metadato con `update`, que no
    # toca embeddings, y la próxima pasada ya podrá saltarse el documento entero.
    nuevos = [i for i, id_ in enumerate(ids) if id_ not in plan.huella_previa]
    remiendos = [
        i
        for i, id_ in enumerate(ids)
        if id_ in plan.huella_previa and plan.huella_previa[id_] != huella
    ]

    for i in range(0, len(nuevos), TAMANO_LOTE):
        lote = nuevos[i : i + TAMANO_LOTE]
        plan.coleccion.upsert(
            ids=[ids[j] for j in lote],
            documents=[textos[j] for j in lote],
            metadatas=[metadatos[j] for j in lote],
        )
    for i in range(0, len(remiendos), TAMANO_LOTE):
        lote = remiendos[i : i + TAMANO_LOTE]
        plan.coleccion.update(
            ids=[ids[j] for j in lote],
            metadatas=[metadatos[j] for j in lote],
        )

    progreso.fragmentos_nuevos += len(nuevos)
    detalle = f"{len(nuevos)} nuevo(s)"
    if remiendos:
        detalle += f", {len(remiendos)} con la huella al día"
    logger.info(f"  {origen}: {len(fragmentos)} fragmento(s), {detalle}")
    return ids


def _olvidar_sobrantes(coleccion: Collection, escritos: list[str], etiqueta: str) -> int:
    """Borra de la colección los fragmentos que esta pasada no ha vuelto a escribir.

    Es lo que permite que un `make ingest` a secas olvide un documento borrado.
    Lo que sobra son los fragmentos de ficheros que ya no están y las versiones
    anteriores de los que se han editado —el identificador incluye un resumen del
    contenido, así que editar un párrafo cambia el id de su fragmento—.

    Args:
        coleccion: La colección del tema.
        escritos: Los identificadores que sí deben quedar, incluidos los de los
            documentos que esta pasada ni ha abierto por no haber cambiado.
        etiqueta: Nombre del tema, para el log.

    Returns:
        Cuántos fragmentos se han olvidado.
    """
    existentes = set(coleccion.get(include=[])["ids"])
    sobrantes = sorted(existentes - set(escritos))
    if not sobrantes:
        return 0
    for i in range(0, len(sobrantes), TAMANO_LOTE):
        coleccion.delete(ids=sobrantes[i : i + TAMANO_LOTE])
    logger.info(f"  {etiqueta}: {len(sobrantes)} fragmento(s) obsoleto(s) olvidado(s)")
    return len(sobrantes)


def ingerir(settings: Settings, *, reset: bool = False) -> int:
    """Reconcilia el índice con el corpus, publicando el avance para el panel.

    Args:
        settings: Configuración del agente.
        reset: Si es True, borra todas nuestras colecciones antes de empezar.

    Returns:
        El número total de fragmentos indexados.

    Raises:
        FileNotFoundError: Si la carpeta del corpus no existe.
    """
    publicador = _Publicador(settings.data_dir)
    try:
        return _reconciliar(settings, publicador, reset=reset)
    except Exception as e:
        # El panel se queda mirando este fichero; si la ingesta revienta y nadie
        # lo dice, la barra se queda a medias para siempre.
        publicador.fallar(str(e))
        raise


def _reconciliar(settings: Settings, publicador: _Publicador, *, reset: bool) -> int:
    """El trabajo de `ingerir`, ya con quien publica el avance.

    Args:
        settings: Configuración del agente.
        publicador: Quien deja el progreso donde el panel lo lee.
        reset: Si hay que borrar todas nuestras colecciones antes de empezar.

    Returns:
        El número total de fragmentos indexados.

    Raises:
        FileNotFoundError: Si la carpeta del corpus no existe.
    """
    settings.apply_model_cache_env()
    progreso = publicador.progreso

    if not settings.corpus_dir.is_dir():
        raise FileNotFoundError(
            f"No existe la carpeta del corpus: {settings.corpus_dir.resolve()}\n"
            f"Créala y mete dentro los documentos "
            f"({', '.join(sorted(EXTENSIONES_SOPORTADAS))}) que quieras que el agente conozca."
        )

    # Un documento a más de un nivel no se indexa, y callárselo es la clase de
    # detalle que cuesta media hora descubrir. Ver `corpus.descubrir_documentos`.
    for ignorado in documentos_ignorados(settings.corpus_dir):
        logger.warning(
            f"  {ignorado.relative_to(settings.corpus_dir)}: está a más de un nivel de "
            "profundidad, no se indexa. Los temas son un solo nivel de subcarpetas."
        )

    cliente = abrir_cliente(settings)
    if reset:
        borrar_coleccion(settings, None, cliente)

    temas = [TEMA_RAIZ, *listar_temas(settings.corpus_dir)]
    logger.info(f"Indexando {len(temas)} tema(s) de {settings.corpus_dir.resolve()}")

    # --- Explorar: qué hay que reprocesar, sin abrir un solo documento --------
    progreso.temas_total = len(temas)
    publicador.publicar(forzar=True)

    planes: list[_PlanTema] = []
    vacios: list[str] = []
    for tema in temas:
        progreso.tema_actual = tema
        documentos = listar_documentos(settings.corpus_dir, tema)
        if documentos:
            planes.append(_planear_tema(settings, cliente, tema, documentos, progreso))
        else:
            vacios.append(tema)
        progreso.temas_hechos += 1
        publicador.publicar()

    logger.info(
        f"{progreso.documentos_total} documento(s): {progreso.documentos_pendientes} por "
        f"indexar, {progreso.documentos_sin_cambios} sin cambios."
    )

    # --- Indexar: solo lo que la exploración ha marcado como pendiente -------
    progreso.fase = FaseIngesta.INDEXANDO
    progreso.tema_actual = ""
    publicador.publicar(forzar=True)

    total = 0
    for plan in planes:
        progreso.tema_actual = plan.tema
        escritos = list(plan.conservados)
        total += len(plan.conservados)
        for documento in plan.pendientes:
            progreso.documento_actual = documento.ruta_relativa
            publicador.publicar(forzar=True)
            ids = _indexar_documento(settings, plan, documento, progreso)
            escritos.extend(ids)
            total += len(ids)
            progreso.documentos_hechos += 1
            progreso.fragmentos_total = total
            publicador.publicar(forzar=True)
        progreso.fragmentos_olvidados += _olvidar_sobrantes(plan.coleccion, escritos, plan.etiqueta)

    # --- Limpiar: lo que sigue indexado y ya no existe en disco --------------
    progreso.fase = FaseIngesta.LIMPIANDO
    progreso.documento_actual = ""
    progreso.tema_actual = ""
    progreso.fragmentos_total = total
    publicador.publicar(forzar=True)

    for tema in vacios:
        # Un tema vacío no merece una colección: ocuparía su índice HNSW en
        # memoria para no devolver nunca nada.
        if borrar_coleccion(settings, tema, cliente):
            logger.info(f"  {tema or '(raíz)'}: sin documentos, colección eliminada")

    # Temas borrados desde el panel o por SSH. Sin esto, el agente seguiría
    # respondiendo con ellos.
    huerfanos = [t for t in temas_indexados(settings, cliente) if t not in temas]
    for tema in huerfanos:
        borrar_coleccion(settings, tema, cliente)
        logger.info(f"  {tema}: el tema ya no existe en el corpus, colección eliminada")

    if total == 0:
        logger.warning(
            f"No hay documentos indexables en {settings.corpus_dir.resolve()} "
            f"(se admiten: {', '.join(sorted(EXTENSIONES_SOPORTADAS))})"
        )
    logger.info(
        f"Listo. {total} fragmento(s) en {len(temas_indexados(settings, cliente))} tema(s)."
    )

    progreso.fase = FaseIngesta.TERMINADO
    publicador.publicar(forzar=True)
    return total


def main() -> int:
    """Punto de entrada del comando de ingesta."""
    parser = argparse.ArgumentParser(description="Indexa el corpus en ChromaDB.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Borra todas las colecciones antes de indexar. Solo hace falta al "
            "cambiar EMBEDDING_MODEL, porque eso invalida el índice HNSW que "
            "ChromaDB creó con la función de embeddings anterior. Los borrados de "
            "documentos y temas, y los cambios de CHUNK_SIZE o CHUNK_OVERLAP, ya "
            "los reconcilia la ingesta normal: entran en la huella."
        ),
    )
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)

    try:
        ingerir(settings, reset=args.reset)
    except FileNotFoundError as e:
        logger.error(str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
