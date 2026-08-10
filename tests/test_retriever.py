"""Tests del filtrado y la fusión del buscador del RAG.

Lo que se prueba aquí es la lógica de decisión —qué pasajes se aceptan, cómo se
fusionan los de varios temas y cómo se presentan al modelo—, no la calidad de
los embeddings. Por eso se usan dobles de ChromaDB: los tests corren en
milisegundos, sin descargar modelos ni depender de la red, y siguen cubriendo la
parte donde de verdad se cometen errores.

La calidad de la recuperación es otra cosa y se mide aparte, con el corpus real;
`docs/rag.md` explica cómo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from voice_agent.rag import retriever as modulo
from voice_agent.rag.retriever import Retriever
from voice_agent.rag.store import nombre_coleccion
from voice_agent_core.config import Settings
from voice_agent_core.corpus import TEMA_RAIZ

#: (texto, origen, distancia)
Fila = tuple[str, str, float]


class ColeccionFalsa:
    """Doble de una colección de ChromaDB."""

    def __init__(self, name: str, tema: str, filas: list[Fila], *, con_tema: bool = True) -> None:
        # `name` es el nombre completo de la colección (con su prefijo), que es
        # lo que ve `temas_indexados`; `tema` es lo que va en los metadatos.
        self.name = name
        self._tema = tema
        self._filas = filas
        self._con_tema = con_tema
        self.consultas: list[Any] = []

    def count(self) -> int:
        return len(self._filas)

    def query(
        self, *, query_embeddings: list[Any], n_results: int, include: Any = None
    ) -> dict[str, Any]:
        self.consultas.append(query_embeddings[0])
        recorte = self._filas[:n_results]
        return {
            "documents": [[t for t, _, _ in recorte]],
            "metadatas": [
                [
                    # Un índice construido antes de que existieran los temas no
                    # trae la clave, y el buscador no puede reventar por eso.
                    {"origen": o, "tema": self._tema} if self._con_tema else {"origen": o}
                    for _, o, _ in recorte
                ]
            ],
            "distances": [[d for _, _, d in recorte]],
        }


class ClienteFalso:
    """Doble del cliente persistente."""

    def __init__(self, colecciones: dict[str, ColeccionFalsa]) -> None:
        self.colecciones = colecciones

    def list_collections(self) -> list[ColeccionFalsa]:
        return list(self.colecciones.values())

    def get_or_create_collection(self, *, name: str, **_: Any) -> ColeccionFalsa:
        return self.colecciones[name]


class EmbeddingsFalsos:
    """Doble del modelo de embeddings, que cuenta cuántas veces lo llaman."""

    def __init__(self) -> None:
        self.llamadas: list[str] = []

    def embed_query(self, input: list[str]) -> list[Any]:
        self.llamadas.extend(input)
        return [[0.0, 1.0]]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path, rag_max_distance=0.68, rag_top_k=4)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def embeddings(monkeypatch: pytest.MonkeyPatch) -> EmbeddingsFalsos:
    """Nadie carga el modelo de verdad.

    Hay que sustituirlo en los **dos** sitios: el buscador lo pide por su cuenta
    para vectorizar la pregunta, y `store.abrir_coleccion` lo pide otra vez para
    dárselo a la colección. Parchear solo el primero deja pasar el segundo, y el
    síntoma es que la batería tarda el doble y baja 120 MB de modelo — que es
    justo lo que este proyecto no quiere de sus tests.
    """
    falsos = EmbeddingsFalsos()
    monkeypatch.setattr(modulo, "funcion_embeddings", lambda *a, **k: falsos)
    monkeypatch.setattr("voice_agent.rag.store.funcion_embeddings", lambda *a, **k: falsos)
    return falsos


def _montar(
    settings: Settings,
    por_tema: dict[str, list[Fila]],
    monkeypatch: pytest.MonkeyPatch,
    *,
    con_tema: bool = True,
) -> tuple[Retriever, ClienteFalso]:
    """Construye un Retriever sobre un cliente falso con las colecciones dadas.

    Solo se sustituyen el cliente y el modelo: `temas_indexados` y
    `abrir_coleccion` son los de verdad, así que el reparto por temas y el
    prefijo de las colecciones se prueban de paso.
    """
    colecciones = {
        nombre_coleccion(settings, tema): ColeccionFalsa(
            nombre_coleccion(settings, tema), tema, filas, con_tema=con_tema
        )
        for tema, filas in por_tema.items()
    }
    cliente = ClienteFalso(colecciones)
    monkeypatch.setattr(modulo, "abrir_cliente", lambda *a, **k: cliente)
    return Retriever(settings, exigir_indice=False), cliente


def _retriever(settings: Settings, filas: list[Fila], monkeypatch: pytest.MonkeyPatch) -> Retriever:
    """Atajo para los casos de un solo tema."""
    return _montar(settings, {TEMA_RAIZ: filas}, monkeypatch)[0]


# --- Filtrado por distancia --------------------------------------------------


def test_descarta_los_pasajes_por_encima_del_umbral(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    """El filtro por distancia es la defensa principal contra las alucinaciones."""
    pasajes = _retriever(
        settings,
        [
            ("Muy relevante", "a.md", 0.30),
            ("Algo relevante", "b.md", 0.60),
            ("Irrelevante", "c.md", 0.69),  # justo por encima de 0.68
            ("Nada que ver", "d.md", 0.95),
        ],
        monkeypatch,
    ).buscar("una pregunta")

    assert [p.texto for p in pasajes] == ["Muy relevante", "Algo relevante"]


def test_conserva_el_origen_para_poder_citar(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    pasajes = _retriever(settings, [("Contenido", "manual/placa.md", 0.20)], monkeypatch).buscar(
        "algo"
    )

    assert pasajes[0].origen == "manual/placa.md"


def test_la_similitud_es_el_complemento_de_la_distancia(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    pasajes = _retriever(settings, [("Contenido", "a.md", 0.25)], monkeypatch).buscar("algo")

    assert pasajes[0].similitud == pytest.approx(0.75)


def test_respeta_el_limite_top_k(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    filas = [(f"Pasaje {i}", "a.md", 0.10) for i in range(10)]
    pasajes = _retriever(settings, filas, monkeypatch).buscar("algo", top_k=3)

    assert len(pasajes) == 3


def test_una_consulta_vacia_no_llega_a_buscar(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    retriever, cliente = _montar(settings, {TEMA_RAIZ: [("Contenido", "a.md", 0.10)]}, monkeypatch)

    assert retriever.buscar("   ") == []
    assert embeddings.llamadas == []
    assert cliente.colecciones["conocimiento"].consultas == []


def test_un_indice_vacio_devuelve_lista_vacia_sin_reventar(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    retriever, _ = _montar(settings, {}, monkeypatch)
    assert retriever.buscar("algo") == []


# --- Fusión de varios temas --------------------------------------------------


def test_fusiona_los_temas_ordenando_por_distancia(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    """El resultado no depende de cómo estén repartidos los documentos.

    Es la propiedad que permite trocear el índice por temas sin cambiar lo que el
    agente recupera, y por tanto sin invalidar el umbral medido.
    """
    retriever, _ = _montar(
        settings,
        {
            TEMA_RAIZ: [("Raíz media", "r.md", 0.40)],
            "la-placa": [("Placa cercana", "p.md", 0.15), ("Placa lejana", "p2.md", 0.66)],
            "el-agente": [("Agente cercano", "a.md", 0.25)],
        },
        monkeypatch,
    )

    pasajes = retriever.buscar("algo")

    assert [p.texto for p in pasajes] == [
        "Placa cercana",
        "Agente cercano",
        "Raíz media",
        "Placa lejana",
    ]


def test_el_top_k_es_global_no_por_tema(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    retriever, _ = _montar(
        settings,
        {
            "uno": [("a", "a.md", 0.10), ("b", "b.md", 0.11)],
            "dos": [("c", "c.md", 0.12), ("d", "d.md", 0.13)],
            "tres": [("e", "e.md", 0.14), ("f", "f.md", 0.15)],
        },
        monkeypatch,
    )

    assert len(retriever.buscar("algo", top_k=2)) == 2


def test_la_pregunta_se_vectoriza_una_sola_vez(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    """Lo caro de una consulta es el embedding, no recorrer el índice.

    Si esto se rompiera, cada tema nuevo añadiría el coste entero de vectorizar
    la pregunta, y el fan-out dejaría de salir gratis.
    """
    retriever, cliente = _montar(
        settings,
        {TEMA_RAIZ: [("a", "a.md", 0.1)], "uno": [("b", "b.md", 0.1)], "dos": [("c", "c.md", 0.1)]},
        monkeypatch,
    )

    retriever.buscar("una pregunta")

    assert embeddings.llamadas == ["una pregunta"]
    # Y el mismo vector ha llegado a las tres colecciones.
    assert all(c.consultas == [[0.0, 1.0]] for c in cliente.colecciones.values())


def test_cada_pasaje_sabe_de_que_tema_viene(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    retriever, _ = _montar(
        settings,
        {TEMA_RAIZ: [("Suelto", "s.md", 0.20)], "la-placa": [("De la placa", "p.md", 0.10)]},
        monkeypatch,
    )

    pasajes = retriever.buscar("algo")

    assert [(p.tema, p.texto) for p in pasajes] == [
        ("la-placa", "De la placa"),
        (TEMA_RAIZ, "Suelto"),
    ]


def test_un_indice_sin_metadato_de_tema_no_revienta(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    """Los fragmentos indexados antes de que existieran los temas siguen valiendo."""
    retriever, _ = _montar(
        settings, {TEMA_RAIZ: [("Antiguo", "a.md", 0.20)]}, monkeypatch, con_tema=False
    )

    pasajes = retriever.buscar("algo")

    assert pasajes[0].tema == ""
    assert pasajes[0].origen == "a.md"


def test_un_tema_nuevo_aparece_sin_reconstruir_el_retriever(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    """Reindexar desde el panel no reinicia el agente.

    Si el buscador se quedara con la lista de colecciones del arranque, subir un
    documento a un tema nuevo y reindexar no serviría de nada hasta el siguiente
    reinicio, y el agente juraría no saber nada de él.
    """
    retriever, cliente = _montar(settings, {TEMA_RAIZ: [("Viejo", "v.md", 0.20)]}, monkeypatch)
    assert len(retriever.buscar("algo")) == 1

    cliente.colecciones["conocimiento__nuevo"] = ColeccionFalsa(
        "conocimiento__nuevo", "nuevo", [("Recién indexado", "n.md", 0.10)]
    )

    pasajes = retriever.buscar("algo")
    assert [p.texto for p in pasajes] == ["Recién indexado", "Viejo"]


def test_un_tema_borrado_deja_de_consultarse(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    retriever, cliente = _montar(
        settings,
        {TEMA_RAIZ: [("Queda", "q.md", 0.20)], "se-va": [("Desaparece", "d.md", 0.10)]},
        monkeypatch,
    )
    assert len(retriever.buscar("algo")) == 2

    del cliente.colecciones["conocimiento__se-va"]

    assert [p.texto for p in retriever.buscar("algo")] == ["Queda"]


def test_num_fragmentos_suma_todos_los_temas(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    retriever, _ = _montar(
        settings,
        {TEMA_RAIZ: [("a", "a.md", 0.1)], "uno": [("b", "b.md", 0.1), ("c", "c.md", 0.1)]},
        monkeypatch,
    )

    assert retriever.num_fragmentos == 3


def test_exigir_indice_falla_si_no_hay_nada(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    monkeypatch.setattr(modulo, "abrir_cliente", lambda *a, **k: ClienteFalso({}))

    with pytest.raises(FileNotFoundError, match="make ingest"):
        Retriever(settings)


# --- Formato para el modelo --------------------------------------------------


class TestFormatoParaElModelo:
    def test_incluye_numeracion_y_fuente(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
    ) -> None:
        texto = _retriever(
            settings,
            [("Primer pasaje", "uno.md", 0.20), ("Segundo pasaje", "dos.md", 0.30)],
            monkeypatch,
        ).buscar_como_texto("algo")

        assert "[1]" in texto and "[2]" in texto
        assert "uno.md" in texto and "dos.md" in texto
        assert "Primer pasaje" in texto and "Segundo pasaje" in texto

    def test_sin_resultados_devuelve_una_instruccion_explicita(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
    ) -> None:
        """Nunca una cadena vacía: el modelo la leería como un fallo de la herramienta."""
        texto = _retriever(
            settings, [("Irrelevante", "a.md", 0.99)], monkeypatch
        ).buscar_como_texto("algo")

        assert texto.strip()
        assert "no contiene información relevante" in texto
        assert "no te inventes" in texto
