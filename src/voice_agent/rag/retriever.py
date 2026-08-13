"""Consulta de la base de conocimiento.

Es la mitad de "recuperación" del RAG: dada una pregunta en lenguaje natural,
devuelve los fragmentos del corpus más parecidos, ya filtrados y formateados
para inyectarlos en el contexto del modelo.

Tres decisiones de diseño merecen explicación:

**Se filtra por distancia, no solo por posición.** Una búsqueda vectorial
siempre devuelve los `k` vecinos más cercanos, aunque el más cercano no tenga
nada que ver con la pregunta. Si se le pasa eso al modelo tal cual, el modelo
tiende a construir una respuesta a partir de contexto irrelevante: es la causa
más habitual de alucinación en un RAG. Descartar por encima de una distancia
máxima permite responder honestamente "no lo sé".

**Cada fragmento se etiqueta con su origen.** Así el modelo puede citar de
dónde sale lo que dice, y quien depura el sistema puede comprobar si el
problema estuvo en la recuperación o en la generación.

**Se consultan todas las colecciones y se fusionan.** El índice está repartido
en una colección por tema (ver `rag/store.py`), pero eso es un detalle de
mantenimiento que ni el modelo ni la herramienta ven: se pregunta a todas y se
ordena el conjunto por distancia. Sale barato porque **la pregunta se vectoriza
una sola vez** y el vector se reutiliza en cada colección; lo caro de una
consulta es el embedding, no el recorrido del índice.

Fusionar no cambia los resultados respecto de tener una sola colección: la
distancia de un fragmento a la pregunta no depende de con quién comparta
colección. Por eso el umbral medido de `RAG_MAX_DISTANCE` sigue siendo válido.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from chromadb.api.models.Collection import Collection
from loguru import logger

from voice_agent.logging import setup_logging
from voice_agent.rag.embeddings import cargar_modelo
from voice_agent.rag.store import (
    abrir_cliente,
    abrir_coleccion,
    funcion_embeddings,
    temas_indexados,
)
from voice_agent_core.cobertura import Cobertura, cargar_alias, frase_temas, resolver_cirugia
from voice_agent_core.config import Settings, get_settings
from voice_agent_core.corpus import TEMA_RAIZ


@dataclass(frozen=True)
class Pasaje:
    """Un fragmento recuperado del índice.

    Attributes:
        texto: Contenido del fragmento.
        origen: Ruta del documento del que procede, relativa al corpus.
        distancia: Distancia coseno a la consulta. 0 es idéntico, 2 es opuesto.
        tema: Tema del que salió. Cadena vacía si es de la raíz del corpus o si
            el fragmento se indexó antes de que existieran los temas.
    """

    texto: str
    origen: str
    distancia: float
    tema: str = ""

    @property
    def similitud(self) -> float:
        """Similitud coseno equivalente, más intuitiva de leer que la distancia."""
        return 1.0 - self.distancia


class Retriever:
    """Buscador sobre la base de conocimiento.

    Se construye una sola vez al arrancar el agente —cargar el modelo de
    embeddings cuesta varios segundos— y se comparte con las herramientas a
    través de `AppResources`.
    """

    def __init__(self, settings: Settings, *, exigir_indice: bool = True) -> None:
        """Abre el índice y carga el modelo de embeddings.

        Args:
            settings: Configuración del agente.
            exigir_indice: Si es True, falla cuando no hay ninguna colección en
                lugar de arrancar contra un índice vacío.

        Raises:
            FileNotFoundError: Si `exigir_indice` es True y no hay nada indexado.
        """
        self._settings = settings
        settings.apply_model_cache_env()
        self._cliente = abrir_cliente(settings)
        self._embeddings = funcion_embeddings(settings.embedding_model)
        # La carga del modelo se pide **aquí y a propósito**: la función de
        # embeddings lo carga en el primer uso (ver su `__init__`), y sin este
        # empujón los diez y pico segundos de ONNX se los comería la primera
        # pregunta del paciente en vez del arranque del agente.
        cargar_modelo(settings.embedding_model)
        self._abiertas: dict[str, Collection] = {}

        temas = temas_indexados(settings, self._cliente)
        if exigir_indice and not temas:
            raise FileNotFoundError(
                f"No hay ninguna colección indexada en {settings.chroma_dir}.\n"
                f"Indexa primero el corpus con:  make ingest"
            )

        logger.info(
            f"RAG listo: {len(temas)} tema(s) con {self.num_fragmentos} fragmento(s) en total"
        )

    def _colecciones(self) -> dict[str, Collection]:
        """Las colecciones vivas, releyendo la lista de temas en cada llamada.

        Releer parece un derroche y no lo es: es una consulta al SQLite local con
        un puñado de filas, y los objetos `Collection` ya abiertos se conservan.
        Lo que compra es importante — reindexar desde el panel **no reinicia el
        agente**, así que sin esto un tema nuevo no existiría hasta el siguiente
        arranque, y quien acabara de subir un documento vería al agente jurar que
        no sabe nada de él.

        Returns:
            Las colecciones por tema.
        """
        vivos = temas_indexados(self._settings, self._cliente)
        for tema in vivos:
            if tema not in self._abiertas:
                self._abiertas[tema] = abrir_coleccion(self._settings, tema, cliente=self._cliente)
        # Un tema borrado deja de consultarse y suelta su índice HNSW.
        for tema in list(self._abiertas):
            if tema not in vivos:
                del self._abiertas[tema]
        return self._abiertas

    @property
    def num_fragmentos(self) -> int:
        """Número de fragmentos indexados, sumando todos los temas."""
        return sum(c.count() for c in self._colecciones().values())

    def temas_disponibles(self) -> list[str]:
        """Los temas indexados ahora mismo, releídos en caliente.

        Lo usa la herramienta de búsqueda para decirle al modelo qué cubre la
        base: sin esa lista, un pasaje de otra cirugía que pase el umbral de
        distancia se disfraza de respuesta, y el modelo acaba atribuyendo la
        información a guías que no existen (pasó en una llamada real con
        una cirugía de cataratas: recuperó colecistitis y lo citó como "las
        guías de cataratas").
        """
        return sorted(self._colecciones().keys())

    def buscar(
        self,
        consulta: str,
        *,
        top_k: int | None = None,
        temas: Sequence[str] | None = None,
    ) -> list[Pasaje]:
        """Recupera los fragmentos más parecidos a la consulta.

        Args:
            consulta: Pregunta en lenguaje natural.
            top_k: Cuántos fragmentos devolver como máximo. Si es None se usa
                el valor de la configuración.
            temas: Si se pasa, **solo** se consultan esos temas. Es lo que
                impide que un fragmento de otra cirugía se disfrace de
                respuesta: el umbral de distancia mide parecido con la
                consulta, no pertenencia a una cirugía, y una pregunta
                postoperatoria genérica se parece al texto postoperatorio de
                cualquier documento clínico (ver el módulo `cobertura`).
                `None` busca en todos, que es el comportamiento de siempre.

        Returns:
            Los pasajes que superan el umbral de distancia, del más al menos
            parecido. Lista vacía si ninguno lo supera.
        """
        consulta = consulta.strip()
        if not consulta:
            return []

        k = top_k or self._settings.rag_top_k
        colecciones = self._colecciones()
        if not colecciones:
            logger.warning("La base de conocimiento está vacía; ejecuta `make ingest`.")
            return []

        if temas is not None:
            permitidos = set(temas)
            # Diccionario nuevo: `_colecciones()` devuelve el suyo por
            # referencia, y filtrarlo en sitio dejaría al retriever sin temas
            # para la siguiente búsqueda.
            colecciones = {t: c for t, c in colecciones.items() if t in permitidos}
            if not colecciones:
                # Y aquí NUNCA un `else` que caiga a todas las colecciones:
                # ese fallback es exactamente el fallo que este parámetro
                # existe para impedir. Se devuelve vacío antes incluso de
                # vectorizar, que además ahorra el embedding.
                logger.info(
                    f"RAG restringido a {sorted(permitidos)}: ningún tema indexado coincide."
                )
                return []

        # Una sola vez, y se reutiliza en todas las colecciones. `embed_query`
        # y no `__call__` porque algunos modelos esperan un prefijo distinto
        # para las preguntas que para los pasajes (ver `embeddings.py`).
        vector = self._embeddings.embed_query([consulta])[0]

        candidatos: list[Pasaje] = []
        examinados = 0
        for coleccion in colecciones.values():
            cuantos = coleccion.count()
            if cuantos == 0:
                continue
            resultado = coleccion.query(
                query_embeddings=[vector],
                # Se piden k a cada colección y luego se recortan a k en total:
                # es lo que hace que el resultado no dependa de cómo estén
                # repartidos los documentos entre temas.
                n_results=min(k, cuantos),
                include=["documents", "metadatas", "distances"],
            )
            documentos = (resultado.get("documents") or [[]])[0]
            metadatos = (resultado.get("metadatas") or [[]])[0]
            distancias = (resultado.get("distances") or [[]])[0]
            examinados += len(documentos)

            for texto, meta, distancia in zip(documentos, metadatos, distancias, strict=False):
                if distancia > self._settings.rag_max_distance:
                    continue
                datos = meta or {}
                candidatos.append(
                    Pasaje(
                        texto=str(texto),
                        origen=str(datos.get("origen", "desconocido")),
                        distancia=float(distancia),
                        # Los índices anteriores a los temas no traen la clave.
                        tema=str(datos.get("tema", "") or ""),
                    )
                )

        pasajes = sorted(candidatos, key=lambda p: p.distancia)[:k]
        logger.debug(
            f"RAG '{consulta[:50]}': {len(pasajes)} pasaje(s) de {len(colecciones)} tema(s), "
            f"{examinados - len(candidatos)} descartado(s) por distancia > "
            f"{self._settings.rag_max_distance}"
        )
        return pasajes

    def buscar_como_texto(
        self,
        consulta: str,
        *,
        top_k: int | None = None,
        temas: Sequence[str] | None = None,
    ) -> str:
        """Recupera pasajes y los formatea para inyectarlos en el contexto del LLM.

        El formato numera los pasajes y nombra su origen, de modo que el modelo
        pueda citar la fuente al responder. No incluye el tema: `origen` ya lo
        lleva como prefijo de ruta, y esta cadena es contexto del modelo —
        cambiarla cambia cómo responde.

        Args:
            consulta: Pregunta en lenguaje natural.
            top_k: Máximo de pasajes a incluir.
            temas: Restringe la búsqueda a esos temas. Ver `buscar`.

        Returns:
            El texto listo para el modelo, o un mensaje explícito de "no hay
            nada" —nunca una cadena vacía, que el modelo interpretaría como un
            fallo de la herramienta en vez de como una ausencia de datos.
        """
        pasajes = self.buscar(consulta, top_k=top_k, temas=temas)
        if not pasajes:
            return (
                "La base de conocimiento no contiene información relevante para esa consulta. "
                "Dilo con naturalidad y no te inventes la respuesta."
            )

        bloques = [
            f"[{i}] (fuente: {p.origen}, similitud {p.similitud:.2f})\n{p.texto}"
            for i, p in enumerate(pasajes, start=1)
        ]
        return "\n\n".join(bloques)


def main() -> int:
    """Consulta la base de conocimiento desde la línea de órdenes.

    Permite probar y afinar el RAG sin pasar por el micrófono, que es el modo
    razonable de trabajar cuando lo que se está ajustando es la recuperación.
    """
    parser = argparse.ArgumentParser(description="Consulta la base de conocimiento del agente.")
    parser.add_argument("consulta", nargs="+", help="La pregunta a buscar.")
    parser.add_argument("-k", "--top-k", type=int, default=None, help="Número de pasajes.")
    parser.add_argument(
        "-p",
        "--procedimiento",
        default=None,
        help="De qué operaron al paciente. Aplica la puerta de cobertura, igual que en una llamada.",
    )
    parser.add_argument(
        "-t",
        "--tema",
        action="append",
        default=None,
        help="Restringe a este tema. Repetible. Salta la puerta: es para depurar el índice.",
    )
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)

    try:
        retriever = Retriever(settings)
    except FileNotFoundError as e:
        logger.error(str(e))
        return 1

    consulta = " ".join(args.consulta)
    temas: list[str] | None = args.tema

    # La puerta, tal cual la aplica la herramienta. Reproducirla aquí es lo que
    # permite comprobar en la placa que una cirugía ajena no busca nada, sin
    # tener que hablarle al micrófono.
    if args.procedimiento is not None:
        disponibles = retriever.temas_disponibles()
        resolucion = resolver_cirugia(
            args.procedimiento, disponibles, cargar_alias(settings.data_dir)
        )
        print(f"\nProcedimiento: {args.procedimiento}  ->  {resolucion.estado}")
        print(f"Cirugías cubiertas: {frase_temas(disponibles)}")
        if resolucion.estado is Cobertura.CUBIERTA:
            assert resolucion.tema is not None
            temas = [resolucion.tema, TEMA_RAIZ]
            print(f"Búsqueda restringida a: {resolucion.tema}")
        elif resolucion.estado is not Cobertura.DESCONOCIDA:
            detalle = (
                f" (empata con {frase_temas(resolucion.candidatos)})"
                if resolucion.candidatos
                else ""
            )
            print(f"\n  BLOQUEADO{detalle}: no se busca nada y no hay extractos que dar.\n")
            return 0

    pasajes = retriever.buscar(consulta, top_k=args.top_k, temas=temas)

    print(f"\nConsulta: {consulta}")
    if not pasajes:
        print(
            "\n  Sin resultados por encima del umbral de similitud "
            f"(distancia máxima {settings.rag_max_distance}).\n"
        )
        return 0

    for i, p in enumerate(pasajes, start=1):
        tema = p.tema or "(raíz)"
        print(
            f"\n[{i}] {p.origen}  tema: {tema}  "
            f"(similitud {p.similitud:.3f}, distancia {p.distancia:.3f})"
        )
        print(f"    {p.texto[:400]}{'...' if len(p.texto) > 400 else ''}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
