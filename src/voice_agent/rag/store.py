"""Las colecciones de ChromaDB donde vive la base de conocimiento.

ChromaDB se usa **embebido** (`PersistentClient`), no como servicio aparte: el
índice es una carpeta en disco que abre el propio proceso del agente. En una
placa con 3.8 GB de RAM, levantar un segundo contenedor con su propio servidor
HTTP costaría entre 300 y 500 MB para no ganar nada, porque solo hay un
consumidor. La reindexación se hace lanzando el comando de ingesta contra la
misma carpeta.

## Una colección por tema

Cada tema del corpus —cada subcarpeta de `corpus/`— tiene su **propia
colección**, y los documentos sueltos de la raíz viven en la que lleva el prefijo
a secas::

    corpus/*.md          ->  conocimiento
    corpus/la-placa/*.md ->  conocimiento__la-placa

No es una cuestión de orden. Con una sola colección, olvidar un documento
borrado obligaba a reconstruir el índice entero (`--reset`, unos cien segundos),
porque la ingesta incremental no puede recorrer lo que ya no existe. Repartido en
colecciones, borrar un tema es `delete_collection` y borrar un documento es
quitar sus identificadores de una colección pequeña: la ingesta puede
**reconciliar** cada tema contra su carpeta sin tocar los demás. Ver
`rag/ingest.py`.

El precio es que una búsqueda tiene que consultar todas las colecciones y
fusionar los resultados. Sale barato porque la pregunta se vectoriza una sola
vez; ver `rag/retriever.py`.
"""

from __future__ import annotations

import re
from functools import lru_cache

from chromadb import PersistentClient
from chromadb.api import ClientAPI
from chromadb.api.models.Collection import Collection
from chromadb.config import Settings as ChromaSettings
from loguru import logger

from voice_agent.rag.embeddings import FastEmbedEmbeddingFunction
from voice_agent_core.config import Settings
from voice_agent_core.corpus import TEMA_RAIZ

#: Separador entre el prefijo y el nombre del tema. Dos guiones bajos porque un
#: solo carácter podría aparecer dentro de un prefijo elegido a mano y haría
#: ambiguo el camino de vuelta de colección a tema.
SEPARADOR = "__"

#: Lo que ChromaDB admite como nombre de colección: de 3 a 63 caracteres, solo
#: `[a-zA-Z0-9._-]`, empezando y acabando en alfanumérico. Se comprueba aquí, al
#: componer el nombre, para que un tema imposible falle al crearse en el panel y
#: no cuarenta segundos después, dentro del contenedor de la ingesta.
_NOMBRE_CHROMA = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{1,61}[a-zA-Z0-9]$")


class NombreDeColeccionInvalido(ValueError):
    """El tema no puede convertirse en un nombre de colección que Chroma acepte."""


def nombre_coleccion(settings: Settings, tema: str = TEMA_RAIZ) -> str:
    """Compone el nombre de la colección de un tema.

    Args:
        settings: Configuración del agente. `chroma_collection` es el prefijo.
        tema: El tema, o `TEMA_RAIZ` para los documentos sueltos de la raíz.

    Returns:
        El nombre de la colección.

    Raises:
        NombreDeColeccionInvalido: Si el resultado no cumple las reglas de Chroma.
    """
    nombre = settings.chroma_collection
    if tema != TEMA_RAIZ:
        nombre = f"{nombre}{SEPARADOR}{tema}"
    if not _NOMBRE_CHROMA.fullmatch(nombre):
        raise NombreDeColeccionInvalido(
            f"'{nombre}' no vale como nombre de colección de ChromaDB: se admiten "
            "de 3 a 63 caracteres de [a-zA-Z0-9._-], empezando y acabando en letra o cifra."
        )
    return nombre


def tema_de_coleccion(settings: Settings, nombre: str) -> str | None:
    """Camino de vuelta: de qué tema es una colección.

    Args:
        settings: Configuración del agente.
        nombre: Nombre de la colección.

    Returns:
        El tema, `TEMA_RAIZ` si es la de la raíz, o None si la colección no es
        nuestra —el mismo `chroma_dir` podría albergar otras cosas y no somos
        quienes para tocarlas—.
    """
    prefijo = settings.chroma_collection
    if nombre == prefijo:
        return TEMA_RAIZ
    if nombre.startswith(prefijo + SEPARADOR):
        return nombre[len(prefijo) + len(SEPARADOR) :]
    return None


@lru_cache(maxsize=2)
def funcion_embeddings(model_name: str) -> FastEmbedEmbeddingFunction:
    """La función de embeddings que comparten todas las colecciones.

    Desde que hay una colección por tema, tanto la ingesta como el buscador abren
    varias seguidas. Quien garantiza que el modelo se cargue una sola vez es
    `embeddings.cargar_modelo` —tiene que ser allí, porque ChromaDB reconstruye
    esta clase por su cuenta con `build_from_config`—; esta caché solo evita
    envoltorios de más.

    Args:
        model_name: Identificador del modelo en el catálogo de fastembed.

    Returns:
        La función de embeddings.
    """
    return FastEmbedEmbeddingFunction(model_name=model_name)


def abrir_cliente(settings: Settings) -> ClientAPI:
    """Abre el cliente persistente de ChromaDB.

    Args:
        settings: Configuración del agente.

    Returns:
        El cliente, apuntando a `chroma_dir`.
    """
    settings.ensure_directories()
    return PersistentClient(
        path=str(settings.chroma_dir),
        settings=ChromaSettings(anonymized_telemetry=False),
    )


def abrir_coleccion(
    settings: Settings,
    tema: str = TEMA_RAIZ,
    *,
    crear: bool = True,
    cliente: ClientAPI | None = None,
) -> Collection:
    """Abre (o crea) la colección de un tema.

    Args:
        settings: Configuración del agente.
        tema: El tema, o `TEMA_RAIZ`.
        crear: Si es True se crea cuando no existe; si es False se exige que
            exista, lo que evita que el agente arranque tan campante contra un
            índice vacío porque nadie ejecutó la ingesta.
        cliente: Cliente ya abierto, para no crear uno por colección cuando se
            recorren varias.

    Returns:
        La colección de ChromaDB.

    Raises:
        FileNotFoundError: Si `crear` es False y la colección no existe.
        NombreDeColeccionInvalido: Si el tema no da un nombre válido.
    """
    cliente = cliente or abrir_cliente(settings)
    nombre = nombre_coleccion(settings, tema)

    if not crear and nombre not in {c.name for c in cliente.list_collections()}:
        raise FileNotFoundError(
            f"La colección '{nombre}' no existe en {settings.chroma_dir}.\n"
            f"Indexa primero el corpus con:  make ingest"
        )

    return cliente.get_or_create_collection(
        name=nombre,
        embedding_function=funcion_embeddings(settings.embedding_model),  # type: ignore[arg-type]
        # La distancia coseno es la adecuada para embeddings de frases; hay que
        # fijarla al crear la colección porque define cómo se construye el
        # índice HNSW y no se puede cambiar después.
        metadata={"hnsw:space": "cosine"},
    )


def temas_indexados(settings: Settings, cliente: ClientAPI | None = None) -> list[str]:
    """Lista los temas que tienen colección en el índice.

    Args:
        settings: Configuración del agente.
        cliente: Cliente ya abierto, opcional.

    Returns:
        Los temas, con `TEMA_RAIZ` el primero si existe. Puede no coincidir con
        los del corpus: esa diferencia es justo lo que la ingesta reconcilia.
    """
    cliente = cliente or abrir_cliente(settings)
    temas = [
        tema
        for c in cliente.list_collections()
        if (tema := tema_de_coleccion(settings, c.name)) is not None
    ]
    return sorted(temas)


def borrar_coleccion(
    settings: Settings, tema: str | None = None, cliente: ClientAPI | None = None
) -> list[str]:
    """Elimina la colección de un tema, o todas las nuestras.

    Args:
        settings: Configuración del agente.
        tema: El tema a borrar; None para borrar todas las del prefijo, que es
            lo que hace `--reset`.
        cliente: Cliente ya abierto, opcional.

    Returns:
        Los temas cuyas colecciones se han borrado.
    """
    cliente = cliente or abrir_cliente(settings)
    # Solo se tocan las colecciones con nuestro prefijo: el mismo `chroma_dir`
    # podría contener otras y `--reset` no es una excusa para arrasarlas.
    objetivo = temas_indexados(settings, cliente) if tema is None else [tema]

    borrados: list[str] = []
    existentes = {c.name for c in cliente.list_collections()}
    for t in objetivo:
        nombre = nombre_coleccion(settings, t)
        if nombre in existentes:
            cliente.delete_collection(nombre)
            borrados.append(t)
            logger.info(f"Colección '{nombre}' eliminada.")
    return borrados
