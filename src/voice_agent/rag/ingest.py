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

`--reset` se mantiene para lo que sí invalida el índice completo: cambiar
`EMBEDDING_MODEL`, `CHUNK_SIZE` o `CHUNK_OVERLAP`.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
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
    documentos_ignorados,
    listar_documentos,
    listar_temas,
)

# Se sube en lotes para no cargar todos los embeddings en memoria a la vez ni
# hacer una llamada por fragmento.
TAMANO_LOTE = 32


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


def _indexar_tema(settings: Settings, cliente: ClientAPI, tema: str) -> int:
    """Indexa un tema y deja su colección conteniendo exactamente su carpeta.

    Args:
        settings: Configuración del agente.
        cliente: Cliente de ChromaDB ya abierto.
        tema: El tema a indexar, o `TEMA_RAIZ`.

    Returns:
        El número de fragmentos escritos.
    """
    documentos = listar_documentos(settings.corpus_dir, tema)
    etiqueta = tema or "(raíz)"

    if not documentos:
        # Un tema vacío no merece una colección: ocuparía su índice HNSW en
        # memoria para no devolver nunca nada.
        if borrar_coleccion(settings, tema, cliente):
            logger.info(f"  {etiqueta}: sin documentos, colección eliminada")
        return 0

    coleccion = abrir_coleccion(settings, tema, cliente=cliente)

    # Los ids ya presentes, una sola vez por colección. El id incluye un
    # resumen del contenido, así que "el id existe" implica "el fragmento es
    # idéntico": no hay que volver a calcular su embedding. Sin este filtro,
    # `upsert` re-embebe el corpus entero en cada reconciliación —medido en la
    # placa, cerca de una hora con el corpus clínico— y el botón Reindexar del
    # panel sería inutilizable en vivo: con él, añadir un documento cuesta lo
    # que cuesta ese documento.
    existentes = set(coleccion.get(include=[])["ids"])

    escritos: list[str] = []
    total = 0
    for documento in documentos:
        ruta = settings.corpus_dir / documento.ruta_relativa
        origen = documento.ruta_relativa
        texto = leer_documento(ruta)
        if not texto.strip():
            logger.warning(f"  {origen}: vacío o ilegible, se omite")
            continue

        fragmentos = trocear(texto, tamano=settings.chunk_size, solape=settings.chunk_overlap)
        if not fragmentos:
            continue

        titulo = extraer_titulo(texto, ruta)
        textos = [contextualizar(f.texto, titulo) for f in fragmentos]
        ids = [_identificador(origen, f.indice, t) for f, t in zip(fragmentos, textos, strict=True)]
        metadatos: list[dict[str, str | int]] = [
            {"origen": origen, "titulo": titulo, "tema": tema, "fragmento": f.indice}
            for f in fragmentos
        ]

        escritos.extend(ids)
        total += len(fragmentos)

        pendientes = [i for i, id_ in enumerate(ids) if id_ not in existentes]
        if not pendientes:
            logger.info(f"  {origen}: {len(fragmentos)} fragmento(s), sin cambios")
            continue

        for i in range(0, len(pendientes), TAMANO_LOTE):
            lote = pendientes[i : i + TAMANO_LOTE]
            coleccion.upsert(
                ids=[ids[j] for j in lote],
                documents=[textos[j] for j in lote],
                metadatas=[metadatos[j] for j in lote],
            )

        logger.info(f"  {origen}: {len(fragmentos)} fragmento(s), {len(pendientes)} nuevo(s)")

    _olvidar_sobrantes(coleccion, escritos, etiqueta)
    return total


def _olvidar_sobrantes(coleccion: Collection, escritos: list[str], etiqueta: str) -> None:
    """Borra de la colección los fragmentos que esta pasada no ha vuelto a escribir.

    Es lo que permite que un `make ingest` a secas olvide un documento borrado.
    Lo que sobra son los fragmentos de ficheros que ya no están y las versiones
    anteriores de los que se han editado —el identificador incluye un resumen del
    contenido, así que editar un párrafo cambia el id de su fragmento—.

    Args:
        coleccion: La colección del tema.
        escritos: Los identificadores que sí deben quedar.
        etiqueta: Nombre del tema, para el log.
    """
    existentes = set(coleccion.get(include=[])["ids"])
    sobrantes = sorted(existentes - set(escritos))
    if not sobrantes:
        return
    for i in range(0, len(sobrantes), TAMANO_LOTE):
        coleccion.delete(ids=sobrantes[i : i + TAMANO_LOTE])
    logger.info(f"  {etiqueta}: {len(sobrantes)} fragmento(s) obsoleto(s) olvidado(s)")


def ingerir(settings: Settings, *, reset: bool = False) -> int:
    """Reconcilia el índice con el corpus.

    Args:
        settings: Configuración del agente.
        reset: Si es True, borra todas nuestras colecciones antes de empezar.

    Returns:
        El número total de fragmentos indexados.

    Raises:
        FileNotFoundError: Si la carpeta del corpus no existe.
    """
    settings.apply_model_cache_env()

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

    total = 0
    for tema in temas:
        total += _indexar_tema(settings, cliente, tema)

    # Lo que quede indexado y ya no exista en disco: temas borrados desde el
    # panel o por SSH. Sin esto, el agente seguiría respondiendo con ellos.
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
    return total


def main() -> int:
    """Punto de entrada del comando de ingesta."""
    parser = argparse.ArgumentParser(description="Indexa el corpus en ChromaDB.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Borra todas las colecciones antes de indexar. Solo hace falta al "
            "cambiar EMBEDDING_MODEL, CHUNK_SIZE o CHUNK_OVERLAP: los borrados de "
            "documentos y temas ya los reconcilia la ingesta normal."
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
