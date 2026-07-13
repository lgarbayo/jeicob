# jeicob

**Agente de investigación autónomo (CLI)** que busca, ingiere mediante **RAG** y
resume **literatura científica** —sobre 3D Gaussian Splatting, el estándar DICOM
y normativas clínicas— para generar **reportes en Markdown**.

Está construido como prototipo aislado, pensado para integrarse más adelante en un
monorepo mayor de salud digital. Por eso cada capa (tools, RAG, orquestador) está
desacoplada y es usable de forma independiente.

---

## 1. Qué hace, en una frase

Le hablas por terminal ("resúmeme el paper de DICOM en `dicom.md`") y el agente,
usando **Claude con *tool calling* nativo**, decide por sí mismo qué herramientas
encadenar: mira qué documentos hay, los indexa en una base vectorial, recupera lo
relevante o lee un paper entero, y escribe un reporte estructurado (abstract +
explicación completa) en `docs_output/`.

---

## 2. Arquitectura

```
                          ┌─────────────────────────────────────────────┐
   Terminal (tú)  ───────▶│  src/main.py — Orquestador (bucle ReAct)     │
        ▲                 │  Claude (claude-opus-4-8) + Tool Runner SDK  │
        │                 └───────────────┬─────────────────────────────┘
        │  respuesta                      │ tool calling nativo
        │  (Markdown)                     ▼
        │       ┌───────────────────────────────────────────────────────┐
        │       │  5 tools expuestas al modelo                           │
        │       ├───────────────────────────────────────────────────────┤
        └───────│  read_directory ─┐                                     │
                │  read_file ──────┼──▶ src/tools.py  (sandbox de disco) │
                │  write_summary ──┘                                     │
                │  ingest_corpus ──┐                                     │
                │  search_corpus ──┴──▶ src/rag.py    (RAG + Qdrant)     │
                └───────────────────────────────────────────────────────┘
                                    │                    │
                          lee de    ▼          indexa en ▼
                    knowledge_base/            .qdrant_data/  (base vectorial)
                    (PDF / MD / TXT)                │
                          escribe en ▼              ▼ embeddings (fastembed)
                          docs_output/       modelo multilingüe ES+EN
                          (reportes .md)
```

**Tres capas:**

| Capa | Fichero | Responsabilidad |
|------|---------|-----------------|
| Orquestador | `src/main.py` | Bucle conversacional + *tool calling* con Claude |
| Herramientas | `src/tools.py` | Acceso a disco con fronteras de seguridad |
| Motor RAG | `src/rag.py` | Ingesta y búsqueda semántica sobre Qdrant |

**Carpetas de datos:**

| Carpeta | Rol |
|---------|-----|
| `knowledge_base/` | **Entrada**: aquí depositas los papers a analizar (PDF, MD, TXT) |
| `docs_output/` | **Salida**: aquí (y solo aquí) el agente escribe los reportes `.md` |
| `.qdrant_data/` | Base vectorial Qdrant persistente (ignorada por git) |

---

## 3. Stack técnico

| Componente | Elección | Por qué |
|------------|----------|---------|
| Gestor de proyecto | **`uv`** (Python 3.13) | Entorno y dependencias reproducibles y rápidos |
| LLM | **`anthropic` SDK**, modelo `claude-opus-4-8` | *Tool calling* nativo + razonamiento largo; sin parsear ReAct a mano |
| Base vectorial | **Qdrant** en local (on-disk) | RAG sin Docker; el índice persiste entre ejecuciones |
| Embeddings | **`fastembed`**, `paraphrase-multilingual-MiniLM-L12-v2` (384 dim) | Local, sin API externa; multilingüe (papers EN + normativas ES) |
| Parseo PDF | **`pypdf`** | Extracción de texto de PDFs |
| CLI | **`rich`** | Salida de terminal legible (Markdown, paneles) |
| Config | **`python-dotenv`** | Carga la API key desde `.env` |

---

## 4. Instalación

Requisitos: [`uv`](https://docs.astral.sh/uv/) instalado y una **API key de Anthropic**.

```bash
git clone git@github.com:lgarbayo/jeicob.git
cd jeicob

# uv crea el entorno virtual y sincroniza dependencias automáticamente
uv sync

# Configura tu clave
cp .env.example .env
# edita .env y pon tu ANTHROPIC_API_KEY
```

Variables de entorno (`.env`):

| Variable | Por defecto | Descripción |
|----------|-------------|-------------|
| `ANTHROPIC_API_KEY` | — | **Obligatoria.** Tu clave de la API de Anthropic |
| `JEICOB_MODEL` | `claude-opus-4-8` | Modelo de Claude a usar |
| `QDRANT_PATH` | `./.qdrant_data` | Ruta de persistencia de la base vectorial |

---

## 5. Uso

1. Deposita uno o varios documentos en `knowledge_base/` (`.pdf`, `.md`, `.txt`).
2. Arranca el agente:

```bash
uv run python -m src.main
```

3. Conversa. Ejemplos de instrucciones:

```
tú › ¿qué documentos tienes disponibles?
tú › indexa todo el corpus
tú › ¿qué dicen los papers sobre compresión DICOM sin pérdida?
tú › haz un reporte completo del paper de gaussian splatting en gs.md
```

4. Escribe `salir` (o `Ctrl-C`) para terminar. Los reportes aparecen en `docs_output/`.

> **Primera ejecución:** la primera vez que se indexa o consulta, `fastembed`
> descarga el modelo de embeddings (~0.22 GB) y lo cachea localmente.

---

## 6. Cómo funciona por dentro (el "CÓMO")

### 6.1. El orquestador (`src/main.py`)

- Define un **system prompt** que describe la misión del agente, cuándo usar cada
  tool y las reglas (citar fuentes, responder en español, actuar cuando tiene
  información suficiente).
- Envuelve las funciones de `tools.py` y `rag.py` con el decorador **`@beta_tool`**
  del SDK. Ese decorador **genera automáticamente el JSON Schema** de cada tool a
  partir de la firma y el docstring de la función — cero esquemas escritos a mano.
- Usa **`client.beta.messages.tool_runner(...)`**, que ejecuta el **bucle ReAct**
  automáticamente:

  ```
  usuario → Claude decide una tool → el runner la ejecuta →
  devuelve el resultado a Claude → Claude decide la siguiente →
  ... → Claude responde en texto (fin del turno)
  ```

- Mantiene el **historial multi-turno** (`messages`) para que la conversación tenga
  memoria entre preguntas.
- Config: `claude-opus-4-8`, **adaptive thinking** activado (razonamiento
  multi-paso), `max_tokens=16000`.
- El motor RAG se instancia de forma **perezosa** (`_get_rag()`): Qdrant no se abre
  hasta que una tool lo necesita, y se cierra (`close()`) al salir.

Las **5 tools** que ve Claude:

| Tool | Envuelve | Cuándo la usa el agente |
|------|----------|-------------------------|
| `read_directory()` | `tools.read_directory` | Descubrir qué documentos hay |
| `read_file(path)` | `tools.read_file` | Leer un documento concreto entero |
| `ingest_corpus()` | `rag.ingest_directory` | Indexar todo el corpus (una vez) |
| `search_corpus(question)` | `rag.query` | Buscar por significado en el corpus |
| `write_summary(...)` | `tools.write_summary` | Escribir el reporte final |

### 6.2. Las herramientas de sistema (`src/tools.py`)

Funciones documentadas con **fronteras de seguridad estrictas**. Nunca lanzan
excepción al llamador: devuelven el error como texto para que el agente pueda
reaccionar y reintentar.

- **`read_directory()`** → lista (recursivamente) los ficheros de `knowledge_base/`
  con su tamaño, en rutas relativas listas para pasar a `read_file`.

- **`read_file(path)`** → lee el contenido de un documento de `knowledge_base/`:
  - PDF vía `pypdf`; texto/Markdown (`.md`, `.markdown`, `.txt`, `.rst`) directo.
  - **Confinado a `knowledge_base/`** por el helper `_resolve_within`, que
    neutraliza *path traversal* (`../`), rutas absolutas y symlinks que escapen.
  - Trunca a **100 000 caracteres** (`MAX_READ_CHARS`) para no desbordar el contexto.

- **`write_summary(filename, title, abstract, full_explanation, key_points=None,
  source=None)`** → escribe el reporte final. **Garantiza la estructura en el
  propio límite de la tool**:
  - **Valida** que existan `title`, `abstract` y `full_explanation`, y que la
    explicación sea **más extensa que el abstract** (si no, devuelve `ERROR` y el
    agente regenera el contenido). Así "explicar el paper completo" no degenera en
    otro resumen breve.
  - **Ensambla** el Markdown con secciones fijas (ver §7).
  - **Solo escribe en `docs_output/`**: reduce `filename` a su nombre base (bloquea
    `../` y subrutas) y fuerza la extensión `.md`.

### 6.3. El motor RAG (`src/rag.py`)

Clase **`RAGEngine`** que encapsula Qdrant + embeddings.

- **Ingesta** (`ingest_file` / `ingest_directory`):
  1. Extrae el texto **completo** del documento (sin truncar, a diferencia de
     `read_file`).
  2. Lo **trocea** (`_chunk`) en ventanas de **1000 caracteres con 150 de solape**
     (`CHUNK_SIZE` / `CHUNK_OVERLAP`), normalizando espacios. El solape evita
     cortar ideas a mitad entre fragmentos.
  3. Crea la colección con la dimensión del modelo si no existe, y hace **`upsert`**
     de cada fragmento como `models.Document` (inferencia de embeddings **local**),
     con metadatos por fragmento: `source` (fichero origen), `chunk` (índice) y
     `document` (el texto).
  - Reutiliza el sandbox de lectura de `tools.py` (misma frontera `knowledge_base/`).

- **Consulta** (`query(question, top_k=5)`):
  - Vectoriza la pregunta y hace **`query_points`** contra la colección.
  - Devuelve los `top_k` fragmentos más relevantes con su **fuente** y su **score**
    de similitud (coseno) — recuperación transversal a todo el corpus, aunque la
    respuesta esté repartida en varios documentos.

- **`count()`** (nº de fragmentos indexados) y **`close()`** (libera el lock del
  directorio on-disk).

- Usa la **API no deprecada** de qdrant-client (`upsert`/`query_points` con
  `models.Document`), no la interfaz de conveniencia `add()`/`query()`.

---

## 7. Formato del reporte generado

`write_summary` produce siempre esta estructura, en este orden:

```markdown
# {título}

> **Fuente:** {documento origen}

## Abstract

{resumen conciso, 3-6 frases}

## Explicación completa

{desarrollo exhaustivo: problema/contexto, metodología,
 resultados, limitaciones y relevancia clínica}

## Puntos clave

- {hallazgo 1}
- {hallazgo 2}
```

El **abstract** y la **explicación completa** son obligatorios (validados por la
tool); "Fuente" y "Puntos clave" son opcionales.

---

## 8. Seguridad

- **Lectura** confinada a `knowledge_base/`; **escritura** confinada a `docs_output/`.
  Ambas pasan por `_resolve_within`, que resuelve la ruta y comprueba que no escape
  del directorio permitido (bloquea `../`, rutas absolutas y symlinks).
- `write_summary` además reduce el nombre de fichero a su base y fuerza `.md`.
- Las claves y datos locales (`.env`, `.qdrant_data/`, `.venv/`) están en
  `.gitignore` y nunca se versionan.

---

## 9. Estructura del repositorio

```
jeicob/
├── README.md
├── pyproject.toml          # dependencias (uv)
├── uv.lock
├── .env.example            # plantilla de configuración
├── .gitignore
├── src/
│   ├── __init__.py
│   ├── tools.py            # Fase 2 — tools de sistema (sandbox)
│   ├── rag.py              # Fase 3 — motor RAG (Qdrant + fastembed)
│   └── main.py             # Fase 4 — orquestador CLI (ReAct)
├── knowledge_base/         # entrada: papers a analizar
└── docs_output/            # salida: reportes Markdown
```

---

## 10. Probar cada capa por separado

Cada módulo trae una prueba de humo ejecutable:

```bash
uv run python -m src.tools    # lista KB, valida escritura y sandbox
uv run python -m src.rag      # ingesta + consulta end-to-end (descarga el modelo)
```

---

## 11. Extensiones futuras

- **Chunking por secciones** (títulos/párrafos) en vez de por caracteres.
- **`top_k` configurable** en `search_corpus`.
- **Qdrant en servidor** (Docker) para concurrencia — la interfaz de `RAGEngine`
  no cambiaría.
- Embeddings SOTA vía **Voyage AI** (partner de Anthropic) si se prioriza calidad
  sobre aislamiento.
- Generación de reportes en **PDF/DOCX** (el sandbox de Anthropic trae
  `python-docx`, `matplotlib`, etc.).
