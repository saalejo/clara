"""Función de embeddings multilingüe para ChromaDB, basada en fastembed.

ChromaDB trae de serie `all-MiniLM-L6-v2`, entrenado esencialmente en inglés.
Con un corpus y unas preguntas en español da resultados pobres: los vectores de
"¿cuál es el horario?" y del párrafo que contiene el horario no acaban
suficientemente cerca.

Aquí se usa `paraphrase-multilingual-MiniLM-L12-v2` a través de **fastembed**,
que ejecuta el modelo en ONNX Runtime. La razón de no usar
`sentence-transformers`, que sería lo habitual, es concreta: arrastra PyTorch,
que en aarch64 son casi mil megabytes de dependencia y una huella de memoria
que esta placa no puede permitirse. fastembed obtiene el mismo modelo con solo
ONNX Runtime, que ya está instalado porque lo necesita el VAD.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import numpy as np
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings, Space
from loguru import logger

MODELO_POR_DEFECTO = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


@lru_cache(maxsize=2)
def cargar_modelo(model_name: str) -> Any:
    """Carga el modelo de fastembed una sola vez por proceso.

    La caché **no es una optimización opcional**. ChromaDB guarda en los
    metadatos de cada colección con qué función de embeddings se creó y la
    reconstruye por su cuenta con `build_from_config`, así que el número de veces
    que se instancia esta clase no lo decide este código: sale de cuántas
    colecciones se abren y cuántas operaciones se hacen sobre ellas.

    Medido en la placa antes de existir esta caché: indexar 11 fragmentos cargaba
    el modelo **seis veces** y tardaba 43 s. Cachear el modelo —que son unos
    120 MB de ONNX— deja que haya tantos envoltorios como Chroma quiera con un
    solo modelo detrás.

    Args:
        model_name: Identificador del modelo en el catálogo de fastembed.

    Returns:
        El modelo de fastembed, compartido.
    """
    from fastembed import TextEmbedding

    logger.debug(f"Cargando el modelo de embeddings '{model_name}'...")
    return TextEmbedding(model_name=model_name)


class FastEmbedEmbeddingFunction(EmbeddingFunction[Documents]):
    """Adaptador entre fastembed y la interfaz de embeddings de ChromaDB.

    Implementa el contrato completo de `EmbeddingFunction` —incluidos
    `name()`, `get_config()` y `build_from_config()`— para que ChromaDB pueda
    guardar en los metadatos de la colección con qué modelo se indexó y
    reconstruirlo al reabrirla. Sin eso, ChromaDB avisa al recargar y se corre
    el riesgo silencioso de consultar un índice con un modelo distinto del que
    lo creó, lo que devuelve resultados sin sentido.
    """

    def __init__(self, model_name: str = MODELO_POR_DEFECTO) -> None:
        """Inicializa la función de embeddings.

        Args:
            model_name: Identificador del modelo en el catálogo de fastembed.
        """
        self._model_name = model_name
        self._model = cargar_modelo(model_name)

    @staticmethod
    def name() -> str:
        """Identificador con el que ChromaDB registra esta función."""
        return "fastembed_multilingue"

    def default_space(self) -> Space:
        """Métrica de distancia por defecto.

        Coseno es la correcta para embeddings de frases: mide diferencia de
        orientación e ignora la magnitud del vector, que en estos modelos no
        codifica significado.
        """
        return "cosine"

    def supported_spaces(self) -> list[Space]:
        """Métricas admitidas por esta función."""
        return ["cosine", "l2", "ip"]

    def get_config(self) -> dict[str, Any]:
        """Devuelve la configuración serializable de esta función."""
        return {"model_name": self._model_name}

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> FastEmbedEmbeddingFunction:
        """Reconstruye la función a partir de la configuración guardada.

        Args:
            config: El diccionario devuelto en su día por `get_config`.

        Returns:
            Una instancia equivalente.
        """
        return FastEmbedEmbeddingFunction(model_name=config["model_name"])

    @staticmethod
    def validate_config(config: dict[str, Any]) -> None:
        """Comprueba que la configuración guardada tiene la forma esperada.

        Args:
            config: Configuración a validar.

        Raises:
            ValueError: Si falta `model_name`.
        """
        if "model_name" not in config:
            raise ValueError("La configuración debe incluir 'model_name'.")

    def __call__(self, input: Documents) -> Embeddings:  # el nombre `input` lo impone Chroma
        """Calcula los embeddings de una lista de documentos.

        Args:
            input: Textos a vectorizar.

        Returns:
            Un vector por texto, en el mismo orden.
        """
        return [np.asarray(v, dtype=np.float32) for v in self._model.embed(list(input))]

    def embed_query(self, input: Documents) -> Embeddings:
        """Calcula los embeddings de una consulta.

        fastembed distingue entre indexar un pasaje y vectorizar una pregunta,
        porque algunos modelos esperan un prefijo distinto en cada caso (los de
        la familia E5, por ejemplo, exigen "query: " y "passage: "). Delegar en
        `query_embed` en vez de reutilizar `embed` hace que cambiar de modelo
        no rompa silenciosamente la calidad de la búsqueda.

        Args:
            input: Consultas a vectorizar.

        Returns:
            Un vector por consulta.
        """
        return [np.asarray(v, dtype=np.float32) for v in self._model.query_embed(list(input))]
