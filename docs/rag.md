# La base de conocimiento (RAG)

RAG significa *retrieval-augmented generation*: en lugar de meter todo lo que el
agente debe saber en el prompt del sistema, se indexa en una base de datos
vectorial y el modelo lo consulta **cuando lo necesita**, usando una herramienta.

## Uso rápido

Lo normal es hacerlo desde el navegador, en la página **Conocimiento** del panel
(ver `panel.md`): crear temas, subir documentos y darle a reindexar, sin entrar
por SSH. Por línea de órdenes:

```bash
# 1. Mete tus documentos (.md, .txt, .pdf) en corpus/, agrupados por temas
mkdir corpus/manuales && cp mis-manuales/*.md corpus/manuales/

# 2. Indexa
make ingest          # reconcilia el índice con el corpus
make reingest        # lo reconstruye desde cero

# 3. Prueba la recuperación sin pasar por el micrófono
make ask Q="cuántos núcleos tiene la placa"
```

Trabajar por línea de órdenes es la forma correcta de afinar el RAG: separa el
problema de recuperación del de conversación. Si `make ask` no encuentra lo que
buscas, no tiene sentido probarlo hablando.

## Los temas: una carpeta y una colección

Cada subcarpeta de `corpus/` es un **tema**, y cada tema tiene su **propia
colección en ChromaDB**. Los documentos sueltos en la raíz forman el tema sin
nombre:

```
corpus/como-funciona-el-agente.md  ←→  colección  conocimiento
corpus/la-placa/*.md               ←→  colección  conocimiento__la-placa
```

El corpus tiene **exactamente dos niveles**. Un documento más hondo no se indexa,
y tanto la ingesta como el panel lo dicen en vez de ignorarlo en silencio.

Repartir el índice no es orden por el orden: es lo que hace que se pueda
mantener. Con una sola colección, olvidar algo obligaba a reconstruirlo todo.

| | Una colección | Una por tema |
|---|---|---|
| Borrar un tema | `--reset` y reindexar entero | se elimina su colección |
| Borrar un documento | `--reset` y reindexar entero | se quitan sus fragmentos |
| Reindexar tras un cambio | todo | solo lo que cambió |

**El agente no se entera de nada de esto.** El buscador vectoriza la pregunta una
sola vez y consulta con ese mismo vector todas las colecciones, fusiona por
distancia y se queda con los `top_k` mejores del conjunto. Como la distancia de
un fragmento a la pregunta no depende de con quién comparta colección, el
resultado es idéntico al de un índice único —medido: "cuántos núcleos tiene la
placa" sigue dando 0.602, el mismo valor de la tabla de calibración de más
abajo— y el umbral no hay que recalibrarlo.

Los nombres de tema se limitan a 40 caracteres y al alfabeto `[a-z0-9._-]`
porque acaban formando el de una colección, y ChromaDB solo admite de 3 a 63
caracteres de `[a-zA-Z0-9._-]` empezando y acabando en alfanumérico. Un test
comprueba que todo tema que el panel deja crear produce un nombre válido, para
que el fallo no aparezca a mitad de una indexación.

## Cómo funciona

```
corpus/<tema>/*.md
    │
    ├─ leer_documento()      texto plano (pypdf si es PDF)
    ├─ trocear()             fragmentos de ~700 caracteres con 120 de solape
    ├─ contextualizar()      antepone el título del documento a cada fragmento
    ├─ FastEmbedEmbedding    cada fragmento -> vector de 384 dimensiones
    └─ coleccion.upsert()    la colección del tema, distancia coseno
                                    │
consulta ──► vector ──► k vecinos por colección ──► fusión ──► filtro ──► LLM
```

El **tema no entra en el texto que se vectoriza**. Viaja como metadato y como
prefijo de `origen`, pero `contextualizar()` no lo toca a propósito: el umbral
de la tabla de calibración está medido sobre exactamente esa cadena, y meterlo
dentro obligaría a repetir la medición entera.

### El troceado

Un modelo de embeddings comprime cada fragmento en un solo vector, así que el
tamaño del fragmento decide la calidad de todo lo demás. Demasiado grande y el
vector mezcla temas y se vuelve un promedio difuso que no se parece a ninguna
pregunta concreta; demasiado pequeño y el fragmento pierde el contexto que lo
hacía comprensible.

`rag/chunking.py` implementa un troceado **recursivo por separadores**: corta
primero por los límites más semánticos que encuentre (encabezados `##`, luego
párrafos, luego líneas, luego frases) y solo parte por palabras cuando no queda
otra. Así los cortes caen casi siempre donde los haría una persona.

El **solape** de 120 caracteres hace que una frase que caiga justo en la frontera
entre dos fragmentos aparezca completa al menos en uno. La cola de solape se
ajusta a un límite de palabra: cortar por número de caracteres a secas produce
fragmentos que empiezan por "ra es monofónica...", que se leen mal y ensucian la
tokenización.

Está escrito a mano, en unas pocas decenas de líneas, en vez de traer LangChain.
Es menos dependencia, se lee de un vistazo y —lo importante— se puede probar:
`tests/test_chunking.py`.

### El troceado contextual

Cada fragmento se indexa precedido del **título de su documento**:

```
Cómo funciona este agente de voz

Todo se empaqueta en un contenedor que se ejecuta con Podman...
```

La razón es concreta. El párrafo sobre el empaquetado no contiene en ningún
sitio la palabra "agente", así que el vector de la pregunta "¿cómo se despliega
el agente?" no acababa lo bastante cerca. Anteponer el título codifica en el
vector de qué documento habla, y además el modelo que luego lo lee sabe de dónde
salió. Cuesta cero y mejora la recuperación de forma medible.

### Los embeddings

`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, ejecutado por
**fastembed** sobre ONNX Runtime. 384 dimensiones, multilingüe, unos 120 MB.

Deliberadamente **no** se usa `sentence-transformers`, que sería lo habitual,
porque arrastra PyTorch: casi mil megabytes en aarch64. Tampoco se usa el
`all-MiniLM-L6-v2` que ChromaDB trae por defecto, porque está entrenado
esencialmente en inglés.

Si quieres más calidad a costa de velocidad, `jinaai/jina-embeddings-v2-base-es`
está especializado en español. Cambiar `EMBEDDING_MODEL` **obliga a reindexar**
(`make reingest`): un índice construido con un modelo y consultado con otro
devuelve resultados sin ningún sentido.

### La ingesta reconcilia, no acumula

El identificador de cada fragmento se deriva de la ruta del documento, el número
de fragmento y un resumen SHA-256 de su contenido. Volver a ejecutar `make
ingest` sobre un corpus sin cambios no duplica nada; sobre un documento editado,
sustituye solo los fragmentos afectados.

Y además **olvida**. Por cada tema, la ingesta:

1. vuelca sus documentos en la colección del tema,
2. borra de esa colección los identificadores que no ha vuelto a escribir —los
   de documentos borrados y los de versiones anteriores de uno editado—,
3. y al terminar elimina las colecciones de los temas cuya carpeta ya no está.

Durante mucho tiempo esto no se podía hacer: con un índice único, una ingesta
incremental no puede olvidar un documento borrado porque ya no hay nada que
recorrer, y la única salida era reconstruirlo todo. Con cada tema en su propia
colección sí se puede, porque se compara la colección contra su carpeta, que sí
se recorre entera.

`make reingest` (o `--reset`) sigue existiendo, pero ya solo hace falta cuando
cambia algo que invalida los vectores en sí: `EMBEDDING_MODEL` o
`CHROMA_COLLECTION`. Los cambios de `CHUNK_SIZE` y `CHUNK_OVERLAP` los reconcilia
la ingesta normal desde que entran en la huella, que es de lo que va el apartado
siguiente.

### La huella: lo que ya está indexado no se vuelve a abrir

Reconciliar por identificador de fragmento ahorra los embeddings, pero no la
lectura: para saber si un fragmento ya estaba había que **extraer el texto del
PDF y trocearlo**, todos los documentos, en todas las pasadas. Con los 106 PDF
del corpus clínico eso era cerca de una hora por pasada aunque no hubiera
cambiado ni una coma, y convertía el botón *Reindexar* del panel en algo que
nadie se atrevía a pulsar en vivo.

Ahora la ingesta empieza **explorando**: calcula la huella de cada fichero
—SHA-256 del contenido, más `EMBEDDING_MODEL`, `CHUNK_SIZE` y `CHUNK_OVERLAP`— y
le pregunta a la colección de su tema con qué huella indexó ese documento, que
va en los metadatos de cada fragmento. Si coinciden **y el número de fragmentos
cuadra**, el documento no se abre siquiera. Lo segundo no es paranoia: una
ingesta interrumpida a mitad de un documento deja la huella correcta en los
fragmentos que sí llegaron a escribirse, y sin contarlos se quedaría cojo para
siempre.

Que la receta del troceado entre en la huella es lo que permite quitar
`CHUNK_SIZE` y `CHUNK_OVERLAP` de la lista de `--reset`: cambiarlos cambia la
huella de todo el corpus, y la ingesta lo reprocesa entero sin que nadie tenga
que acordarse.

Medido en la placa, con el corpus clínico entero (107 documentos, 10 238
fragmentos):

| Pasada | Antes | Ahora |
|---|---|---|
| Sin cambios | ~1 h | **7 s** (3,3 s de ingesta; el resto es arrancar Python) |
| Un documento nuevo | ~1 h | lo que cueste ese documento |
| Adoptar el índice viejo | — | 6 min, una sola vez |

La última fila es la pega, y es de una sola vez: a un índice construido antes de
que existieran las huellas hay que leerle los documentos una vez para saber a qué
fragmento pertenece cada cual. Esa pasada **no recalcula ni un embedding** —los
identificadores ya coinciden, así que solo remienda los metadatos con `update`—,
y a partir de ahí toda reindexación entra por el camino rápido.

### El modelo se carga cuando hace falta

Desde las huellas hay un caso muy frecuente —reindexar sin cambios— en el que se
abren todas las colecciones y no se vectoriza ni un fragmento. Como abrir una
colección construye la función de embeddings, cargar el modelo ahí eran más de
diez segundos de ONNX para nada, así que `FastEmbedEmbeddingFunction` lo carga en
el **primer uso**. El agente no se ve afectado porque `Retriever.__init__` pide
la carga explícitamente: quien tiene que pagar esos segundos es el arranque, no
la primera pregunta del paciente.

### El modelo se carga una sola vez

ChromaDB guarda en los metadatos de cada colección con qué función de embeddings
se creó, y la **reconstruye por su cuenta** llamando a `build_from_config`. O sea
que cuántas veces se instancia `FastEmbedEmbeddingFunction` no lo decide nuestro
código: sale de cuántas colecciones se abren y cuántas operaciones se hacen.

Medido en la placa: sin cachear, indexar 11 fragmentos cargaba el modelo **seis
veces** y tardaba 43 s. Con la caché de `cargar_modelo`, una vez y 16 s. Por eso
el `lru_cache` está en `embeddings.py`, sobre el modelo, y no sobre el envoltorio
que Chroma se empeña en rehacer.

## El filtro por distancia: lo que evita las alucinaciones

Una búsqueda vectorial **siempre** devuelve los `k` vecinos más cercanos, aunque
el más cercano no tenga nada que ver con la pregunta. Si eso se le pasa al modelo
tal cual, el modelo construye una respuesta a partir de contexto irrelevante:
es la causa más común de alucinación en un RAG.

Por eso se descarta todo lo que supere `RAG_MAX_DISTANCE` (distancia coseno,
donde 0 es idéntico y 2 es opuesto). Cuando no queda ningún pasaje, la
herramienta devuelve una instrucción explícita —"la base de conocimiento no
contiene información relevante, dilo con naturalidad y no te inventes la
respuesta"— en lugar de una cadena vacía, que el modelo interpretaría como un
fallo de la herramienta.

## Cómo calibrar el umbral para tu corpus

El valor por defecto, **0.68**, sale de medir el corpus de ejemplo. Para otro
corpus hay que repetir la medida: escribe una lista de preguntas que tu corpus
**sí** cubre y otra de preguntas que **no**, y mira dónde caen.

```python
from voice_agent.config import get_settings
from voice_agent.rag.store import abrir_coleccion

s = get_settings()
s.apply_model_cache_env()
col = abrir_coleccion(s)

DENTRO = ["...preguntas que tu corpus responde..."]
FUERA = ["...preguntas de temas ajenos..."]

d = lambda q: col.query(query_texts=[q], n_results=1, include=["distances"])["distances"][0][0]

print("peor caso cubierto:  ", max(d(q) for q in DENTRO))
print("mejor caso NO cubierto:", min(d(q) for q in FUERA))
```

El umbral va entre esos dos números. Estos son los resultados con el corpus de
ejemplo:

| Preguntas cubiertas | Distancia | | Preguntas no cubiertas | Distancia |
|---|---|---|---|---|
| qué voz usa para hablar | 0.276 | | receta de la paella | 0.693 |
| qué modelo de whisper se usa | 0.341 | | billete de tren a Sevilla | 0.820 |
| cuánta memoria RAM tiene | 0.388 | | mundial de fútbol de 2010 | 0.842 |
| cuánto tarda en transcribir | 0.441 | | recomiéndame una película | 0.857 |
| por qué no puede usar 16 kHz | 0.481 | | cambiar el aceite del coche | 0.895 |
| puedo interrumpir al agente | 0.485 | | capital de Mongolia | 0.935 |
| cuántos núcleos tiene la placa | 0.602 | | | |
| qué base de datos vectorial usa | 0.623 | | | |
| qué sistema operativo corre | 0.632 | | | |
| cómo se despliega | 0.726 | | | |

**Los rangos se solapan**: el peor caso cubierto (0.726) queda por encima del
mejor caso no cubierto (0.693). No existe un umbral que los separe
perfectamente, y conviene saberlo en vez de creer que el filtro es infalible.

Con 0.68 se rechazan las seis preguntas ajenas y se pierde una de las diez
cubiertas. Se elige errar por ese lado a propósito: un "no lo sé" es recuperable
—la persona reformula— mientras que una respuesta inventada que suena convincente
no lo es.

Con un corpus más grande el solape tiende a reducirse, porque hay más
probabilidad de que exista un fragmento realmente cercano a cada pregunta. Si
necesitas más precisión que la que da un umbral, el siguiente paso es añadir un
*reranker* que reordene los candidatos con un modelo cruzado; fastembed incluye
alguno, a costa de más latencia.

## Ajustes

| Variable | Efecto |
|---|---|
| `RAG_TOP_K` | Cuántos fragmentos se recuperan. Más contexto, pero también más tokens y más latencia. |
| `RAG_MAX_DISTANCE` | El umbral. Subirlo hace al agente más hablador y más propenso a inventar; bajarlo, más honesto y más "no lo sé". |
| `CHUNK_SIZE` | Tamaño de fragmento. Sube si tus documentos tienen ideas largas; baja si son muy densos. **Requiere reindexar**, pero basta `make ingest`: entra en la huella y se reprocesa solo. |
| `CHUNK_OVERLAP` | Solape. **Requiere reindexar**, igual que el anterior y por lo mismo. |
| `EMBEDDING_MODEL` | El modelo. **Requiere `make reingest`**: cambia además la función con la que ChromaDB construyó el índice. |
| `CHROMA_COLLECTION` | Prefijo de las colecciones. Cada tema es `<prefijo>__<tema>`. **Requiere reindexar**: las colecciones con el prefijo viejo quedan huérfanas. |

## Cuánto cuesta tener muchos temas

Cada colección abierta carga su propio índice HNSW, así que la pregunta razonable
es si repartir el corpus sale caro en una placa con 3.8 GB. Medido, con el pico
de memoria del proceso que abre el buscador y hace dos consultas:

| Temas | Fragmentos | Pico de memoria |
|---|---|---|
| 1 | 4 | 696 MB |
| 4 | 16 | 699 MB |
| 12 | 48 | 725 MB |

Unos 2,4 MB por tema. Casi todo lo que se ve ahí es el modelo de embeddings, que
se carga una sola vez y no depende del número de colecciones. Con dos docenas de
temas el coste sigue siendo despreciable frente a los 2 GB del agente.

## Calibración para el corpus clínico del reto (agosto de 2026)

El corpus del reto mezcla español e inglés y las preguntas llegan en español,
así que el umbral se recalibró con sondas reales sobre los 10 157 fragmentos
indexados (`RAG_MAX_DISTANCE=1.5` temporal para ver las distancias crudas):

| Sonda | Mejor distancia |
|---|---|
| ¿Puedo mojar la herida después de una apendicectomía? | 0.372 |
| ¿Qué síntomas de alarma debo vigilar tras un reemplazo de cadera? | 0.374 |
| ¿Es normal tener fiebre después de una cirugía de colon? | 0.337 |
| ¿Cuándo puedo comer normal después de que me quiten la vesícula? | 0.403 |
| — fuera del corpus — | |
| ¿Qué me tomo para la migraña? | 0.579 |
| ¿Cómo preparo un ajiaco santafereño? | 0.598 |
| ¿Cuál es la capital de Australia? | 0.693 |
| ¿Quién ganó el mundial de fútbol? | 0.720 |

Las nubes quedan separadas (cubiertas ≤ 0.41, ajenas ≥ 0.56) y el umbral se
fijó en **0.52**: por encima de todo lo cubierto con margen, y por debajo de la
sonda ajena más cercana — que además era la más peligrosa, porque "¿qué me tomo
para la migraña?" recuperaba instrucciones de apendicectomía con el 0.68
anterior. `RAG_TOP_K=5` para compensar el corpus grande.

## La puerta de cobertura

Aquella calibración sigue siendo cierta y dejó de ser suficiente. Las sondas
ajenas de la tabla son de **otro dominio** —migraña, ajiaco, Australia—, y ese
es el caso fácil. El difícil es una pregunta **postoperatoria** sobre una
cirugía que el corpus no cubre, porque se parece muchísimo al texto
postoperatorio de cualquier documento clínico. Medido en la placa con el índice
real, para un paciente de cataratas:

| Consulta | Qué recupera |
|---|---|
| `cirugia de cataratas` | 1 pasaje a **0.515**, de `colecistitis/GUIA COLECISTITIS AGUDA.pdf` — una tabla de control de cambios: ruido puro, pero pasa el umbral |
| `cuidados de la herida cirugia de cataratas ojo` | 5 pasajes a **0.457–0.460**, de `colecistitis` y `reemplazo-articular-total`; uno casó por la frase «de los ojos», que en una guía de vesícula es la ictericia escleral |
| `signos de alarma cirugia ocular vision borrosa` | 5 pasajes a **0.445–0.457**, todos de `apendicitis` |

Están entre 0.44 y 0.52, es decir, **dentro** de la nube que la tabla de arriba
daba por cubierta. Bajar el umbral no arregla nada: se llevaría por delante las
consultas buenas, que viven en ese mismo rango.

La razón de fondo es que **el umbral no puede decidir esto**. Mide si un
fragmento se parece a la consulta; la cirugía del paciente no viaja en la
consulta, así que no hay número que separe «esto es de su operación» de «esto
es de otra». Ninguna calibración lo va a conseguir.

Tampoco lo consigue pedírselo al modelo. Se intentó: la herramienta anteponía a
los resultados una línea diciendo qué cirugías cubría la base y prohibiendo
atribuirle guías ajenas al paciente, y el prompt del sistema lo repetía. No
bastó — una advertencia no gana contra cinco bloques de texto clínico con pinta
de autoridad, y el agente contestaba desde el texto.

Así que la cobertura se decide **en código**, en `voice_agent_core.cobertura`:

1. El procedimiento del paciente es un dato estructurado. Viene del evento de
   llamada (`TareaProgramada.procedimiento`, que se rellena en el panel), del
   historial del número, o —solo si no hay ninguno de los dos— de lo que el
   modelo declare en el argumento `cirugia_del_paciente`.
2. `resolver_cirugia` lo casa contra los temas indexados **ahora mismo**, con
   dos brazos léxicos: una tabla de alias por tema («vesícula» → `colecistitis`)
   y prefijo común de 5 caracteres contra el nombre del propio tema, que es lo
   que permite que un tema recién subido se reconozca sin tocar código.
3. Según el veredicto, `buscar_en_documentos` hace una de tres cosas:

| Estado | Qué hace |
|---|---|
| `cubierta` | Busca **restringida a `[tema, TEMA_RAIZ]`**. De paso se acabó la contaminación cruzada: al paciente de apéndice ya no puede salirle un fragmento de la guía de la vesícula. Si ese tema no devuelve nada, **no** ensancha la búsqueda. |
| `no_cubierta` | **No llama al retriever.** Cero extractos, e instrucción de decirlo y remitir al equipo médico. Es el mecanismo entero: la advertencia deja de competir con extractos porque no hay extractos. |
| `ambigua` | Tampoco busca. «Un cáncer» encaja con dos temas; enseñar los dos es elegir mal dos veces. Se sale preguntando de qué órgano. |
| `desconocida` | Único estado permisivo, y tiene que serlo: al principio de una llamada la cirugía no se sabe. Busca en todo, recorta los pasajes a un solo tema y le exige al modelo preguntar la cirugía antes de concretar. |

Por qué léxico y no vectorial: el propio fallo es la refutación. Este modelo de
embeddings le da 0.44–0.52 a texto de vesícula frente a una consulta de ojos, y
los nombres de cirugía sueltos son todos vecinos en el espacio médico genérico.
Además costaría un `embed_query` dentro de la ruta de latencia de la voz y
metería no-determinismo en la única pieza que tiene que ser determinista.

Se prefiere el sobre-rechazo: decir «su cirugía no está entre mis protocolos»
sobre una que sí está es molesto y lo corrige el paciente en un turno; dar
cuidados específicos citando guías de otra cirugía es el fallo clínico. Si una
sonda de una cirugía cubierta sale vacía, se revisan los alias, **no** el umbral.

### Cómo llama la gente a cada cirugía

Es la única pieza configurable de la puerta, y la que decide si ampliar el
corpus obliga o no a tocar código. El nombre de la carpeta ya reconoce lo obvio
—un tema `cataratas` responde a «cataratas»— pero nadie dice «colecistitis» por
que le hayan quitado la vesícula, y a un tema que alguien llame `oftalmologia`
no lo va a nombrar así ningún paciente.

En la página **Conocimiento**, cada tema tiene un campo *Cómo lo llama la gente*
con una lista separada por comas. Se guardan en `data/config/alias_temas.json`,
que escribe el panel y el agente **relee en cada consulta**: declarar un nombre
surte efecto en mitad de una llamada, sin reindexar y sin reiniciar. Medido en
la placa: «me cambiaron la coyuntura» pasaba de `no_cubierta` a
`cubierta / reemplazo-articular-total` en cuanto se declaró el alias.

Los alias del panel se **suman** a la tabla `ALIAS_POR_TEMA` del código, no la
sustituyen: ampliar un tema no puede romper lo que ya funcionaba. Y no
secuestran nada — sigue ganando el tema que más términos casa, así que un alias
demasiado general no se lleva a un paciente de otra cirugía.

El fichero vive en `data/` y no dentro de la carpeta del tema por un motivo muy
concreto: escribir cualquier cosa bajo `corpus/` mueve la fecha de su carpeta, y
`corpus.marca_de_cambio` la usa para avisar de que falta reindexar. Editar un
alias —que no entra en el índice— encendería ese aviso y mandaría a alguien a
esperar una hora de ingesta para nada.

**La puerta en sí no tiene interruptor, y es deliberado.** Es una salvaguarda
clínica: un botón que la apague devuelve en silencio el fallo que existe para
impedir, y lo natural es pulsarlo justo cuando algo parece atascado en una
demostración. Para una instalación que no la quiera, el panel ya tiene la
palanca correcta y a la granularidad correcta: desactivar `buscar_en_documentos`
para ese perfil, con lo que no hay RAG que gobernar.
