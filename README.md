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
encadenar: mira qué documentos hay, **descubre y descarga papers nuevos de fuentes
académicas**, los indexa en una base vectorial, recupera lo relevante o lee un
paper entero, y escribe un reporte estructurado (abstract + explicación completa)
en `docs_output/`.

---

## 2. Arquitectura

```
                          ┌─────────────────────────────────────────────┐
   Terminal (tú)  ───────▶│  jeicob/main.py — Orquestador (bucle ReAct)  │
        ▲                 │  Claude (claude-opus-4-8) + Tool Runner SDK  │
        │                 └───────────────┬─────────────────────────────┘
        │  respuesta                      │ tool calling nativo
        │  (Markdown)                     ▼
        │       ┌───────────────────────────────────────────────────────┐
        │       │  7 tools expuestas al modelo                           │
        │       ├───────────────────────────────────────────────────────┤
        └───────│  read_directory ─┐                                     │
                │  read_file ──────┼──▶ jeicob/tools.py (sandbox)        │
                │  write_summary ──┘                                     │
                │  ingest_corpus ──┐                                     │
                │  search_corpus ──┴──▶ jeicob/rag.py   (RAG + Qdrant)   │
                │  search_references ─┐                                  │
                │  download_reference ┴▶ jeicob/references.py            │
                │                        (+ netguard, salida a internet) │
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
| Orquestador | `jeicob/main.py` | Bucle conversacional + *tool calling* con Claude |
| Herramientas | `jeicob/tools.py` | Acceso a disco con fronteras de seguridad |
| Motor RAG | `jeicob/rag.py` | Ingesta y búsqueda semántica sobre Qdrant |

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
| Base vectorial | **Qdrant** en local (on-disk) u opcionalmente en servidor (Docker) | RAG sin Docker por defecto; el servidor permite usar CLI y MCP a la vez |
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

# uv crea el entorno virtual, sincroniza dependencias e instala el paquete
# `jeicob` (editable) con sus comandos: jeicob, jeicob-local y jeicob-mcp
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
| `JEICOB_HOME` | raíz del repo | Base contra la que se resuelven las rutas relativas |
| `JEICOB_KB_DIR` | `./knowledge_base` | Carpeta de documentos fuente |
| `JEICOB_OUTPUT_DIR` | `./docs_output` | Carpeta donde se escriben los reportes |
| `FASTEMBED_CACHE_PATH` | `./.fastembed_cache` | Caché del modelo de embeddings |
| `QDRANT_URL` | — | Si se define (p. ej. `http://localhost:6333`), usa un Qdrant en servidor en vez de `QDRANT_PATH` (ver §5.3) |
| `QDRANT_API_KEY` | — | API key del Qdrant en servidor (obligatoria con `docker-compose.yml`) |
| `JEICOB_ENABLE_NETWORK` | `1` | `0` quita `search_references` y `download_reference` a los tres orquestadores (CLI con Claude, CLI con Ollama y servidor MCP), las omite del prompt de sistema y bloquea en `netguard` cualquier petición saliente. El modelo de embeddings se descarga igualmente si no está en `FASTEMBED_CACHE_PATH`; en un equipo aislado, ejecuta antes `uv run python -m jeicob.rag --prefetch` con red |
| `JEICOB_DOWNLOAD_ALLOWED_HOSTS` | — | Lista separada por comas; si se define (p. ej. `arxiv.org,semanticscholar.org`), `download_reference` solo descarga de esos dominios y sus subdominios (CLI y MCP) |
| `JEICOB_ALLOW_PRIVATE_NETWORK` | `0` | `1` o `true` desactiva el bloqueo SSRF de IPs no públicas, para repositorios de la intranet (CLI y MCP) |

Las rutas relativas se resuelven contra `JEICOB_HOME` (la raíz del repo), **no**
contra el directorio desde el que lances el proceso (ver `jeicob/config.py`). Si
instalas jeicob como paquete normal (no editable, fuera del repo), `JEICOB_HOME`
pasa a ser `~/.jeicob` y el `.env` se lee de ahí.

Los flags booleanos aceptan `1`/`true`/`yes`/`sí`/`on` y `0`/`false`/`no`/`off`
(o vacío). Un valor distinto se avisa por stderr; en `JEICOB_ENABLE_NETWORK`
desactiva la red (falla cerrado) y en los demás se usa el valor por defecto.

> **Tip:** `uv run python -m jeicob.rag --prefetch` descarga el modelo de embeddings
> (~0.22 GB) de antemano, para que la primera consulta no tenga que esperar.

---

## 5. Uso

1. Deposita uno o varios documentos en `knowledge_base/` (`.pdf`, `.md`, `.txt`).
2. Arranca el agente:

```bash
uv run jeicob          # equivale a: uv run python -m jeicob.main
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

### 5.1. Modo local gratuito (Ollama, sin API key)

Existe una segunda variante del orquestador, `jeicob/main_local.py`, que usa un
**modelo local servido por [Ollama](https://ollama.com)** en lugar de Claude:
**0 € por token, sin API key y sin enviar tus documentos a una API de LLM**.
Ojo: `search_references` y `download_reference` sí hacen peticiones a internet
(Semantic Scholar, arXiv, la URL de descarga); con datos clínicos, desactívalas
con `JEICOB_ENABLE_NETWORK=0` (ver §8). Reutiliza exactamente las mismas tools y el
mismo motor RAG; solo cambia el "cerebro".

Requisitos: [Ollama](https://ollama.com) instalado + un modelo con soporte de
*tool calling* (recomendado `qwen2.5`).

```bash
# 1. Arranca el servidor Ollama (en una terminal aparte)
ollama serve

# 2. Descarga un modelo con tools (~4.7 GB la primera vez)
ollama pull qwen2.5:7b

# 3. Lanza el agente local
uv run jeicob-local    # o: uv run python -m jeicob.main_local
```

Variables de entorno propias del modo local:

| Variable | Por defecto | Descripción |
|----------|-------------|-------------|
| `JEICOB_LOCAL_MODEL` | `qwen2.5:7b` | Modelo de Ollama a usar (p. ej. `qwen2.5:14b`) |
| `OLLAMA_HOST` | `http://localhost:11434` | URL del servidor Ollama |

**Contrapartidas** frente a la versión Claude: el *tool calling* de los modelos
locales es menos fiable (puede equivocarse más al encadenar herramientas) y la
calidad de síntesis es menor. A cambio, es gratis y privado. Como Ollama no ofrece
un *tool runner*, `main_local.py` implementa el bucle ReAct a mano.

| Variante | Fichero | Coste | Calidad | Privacidad |
|----------|---------|-------|---------|------------|
| Claude | `jeicob/main.py` | De pago (API key) | Alta | Datos van a la API |
| Ollama | `jeicob/main_local.py` | **Gratis** | Media | LLM local; las tools de referencias salen a internet salvo con `JEICOB_ENABLE_NETWORK=0` |
| Servidor MCP | `jeicob/mcp_server.py` | El de tu cliente MCP | La del modelo del cliente | Datos van al LLM del cliente |

### 5.2. Como servidor MCP (Claude Code, Claude Desktop…)

`jeicob/mcp_server.py` expone las mismas tools por el
[Model Context Protocol](https://modelcontextprotocol.io), de modo que **el
"cerebro" pasa a ser tu cliente MCP** (Claude Code, Claude Desktop u otro): no
necesitas `ANTHROPIC_API_KEY` ni el bucle de `main.py`. Reutiliza sin cambios
`tools.py`, `rag.py` y `references.py`.

```bash
# Instala el extra opcional `mcp` (SDK oficial de MCP)
uv sync --extra mcp

# Recomendado: descarga el modelo de embeddings antes de la primera consulta
uv run python -m jeicob.rag --prefetch
```

**Claude Code** — el repo ya trae un `.mcp.json` con el servidor `jeicob`. Abre
`claude` en la raíz del repo, aprueba el servidor cuando te lo pida y comprueba
con `/mcp` que está conectado. Ese `.mcp.json` lanza `uv run ... jeicob-mcp`,
y `uv` busca el proyecto en el directorio actual, así que **solo funciona si
Claude Code arranca en la raíz del repo**. Para usarlo desde cualquier otro
directorio, regístralo con ruta absoluta:

```bash
claude mcp add jeicob -- uv --directory /ruta/absoluta/a/jeicob run --extra mcp jeicob-mcp
```

**Claude Desktop** — añade a `claude_desktop_config.json` (Desktop no hereda el
`PATH` de tu shell, así que usa la ruta absoluta de `uv`; `which uv`):

```json
{
  "mcpServers": {
    "jeicob": {
      "command": "/ruta/absoluta/a/uv",
      "args": ["--directory", "/ruta/absoluta/a/jeicob", "run", "--extra", "mcp",
               "jeicob-mcp"]
    }
  }
}
```

Lo que ve el cliente:

| Tool | Notas MCP |
|------|-----------|
| `read_directory` | Solo lectura |
| `read_file(path, offset=0, max_chars=20000)` | Solo lectura; tramos de 20 000 caracteres como máximo, para no superar el límite de salida del cliente |
| `ingest_corpus(force=False)` | Idempotente; envía progreso por documento; marcada como destructiva porque poda del índice lo que ya no está en disco. Si fallan algunos documentos, el resultado lo resume sin ser un error |
| `search_corpus(question, top_k=5, source=None)` | Solo lectura; `source` limita a un documento |
| `search_references(query, limit=5)` | Red (Semantic Scholar / arXiv) |
| `download_reference(url, filename)` | Red; descarga e indexa; nunca sobrescribe; protección SSRF. Si el indexado falla, confirma la descarga con un aviso |
| `write_summary(..., overwrite=False)` | Escribe en `docs_output/`; solo reemplaza con `overwrite=true` |

Además de tools, el servidor ofrece **resources** y **prompts**:

| Tipo | Nombre | Qué es |
|------|--------|--------|
| Resource | `jeicob://knowledge/{+path}` | Texto extraído de cada documento de `knowledge_base/` (hasta 60 000 caracteres; para el resto, `read_file` con `offset`) |
| Resource | `jeicob://reports/{name}` | Cada reporte Markdown de `docs_output/` |
| Prompt | `research_report(topic, filename?)` | Flujo completo: corpus → fuentes externas → lectura → reporte |
| Prompt | `summarize_document(path)` | Reporte completo de un documento concreto |
| Prompt | `ask_corpus(question)` | Respuesta citada usando solo el corpus |

En Claude Code los resources se adjuntan con `@jeicob:` y los prompts aparecen
como comandos `/mcp__jeicob__research_report`, etc. Los argumentos `path` y
`name` se autocompletan con los ficheros existentes, y el listado de resources se
actualiza (con notificación al cliente) cuando `download_reference` o
`write_summary` añaden un fichero.

Cada tool declara sus *annotations* MCP (`readOnlyHint`, `destructiveHint`,
`openWorldHint`…), que el cliente usa para decidir cuándo pedirte confirmación,
y los errores llegan como `isError` para que el modelo los corrija.

**Concurrencia con el CLI.** Qdrant en modo local bloquea `.qdrant_data/` para un
único proceso (con Qdrant en servidor no hay lock: ver §5.3). El servidor abre la base solo cuando una tool la necesita y la
**libera tras 30 s sin uso**, así que puedes alternar entre el servidor MCP y
`jeicob.main`. Si otro proceso la tiene abierta, la tool espera unos segundos (como
mucho `JEICOB_QDRANT_LOCK_TIMEOUT` desde que se pidió, aunque haya varias en
cola) y devuelve un error claro. Al cerrar el servidor, una ingesta en curso se
detiene al terminar el documento actual y libera la base. Usarlos *a la vez*
requiere Qdrant en servidor (§5.3).

Variables propias del servidor (`JEICOB_ENABLE_NETWORK` y las de descargas,
`JEICOB_DOWNLOAD_ALLOWED_HOSTS` y `JEICOB_ALLOW_PRIVATE_NETWORK`, están en la
tabla general de §4 porque afectan también al CLI):

| Variable | Por defecto | Descripción |
|----------|-------------|-------------|
| `JEICOB_QDRANT_IDLE_RELEASE` | `30` | Segundos sin uso antes de liberar el lock de Qdrant |
| `JEICOB_QDRANT_LOCK_TIMEOUT` | `10` | Segundos esperando si otro proceso tiene el lock |
| `JEICOB_MCP_PREFETCH` | `1` | Descarga el modelo de embeddings en segundo plano al arrancar |
| `JEICOB_MCP_TOKEN` | — | Token bearer, obligatorio con `--transport http` (≥ 32 caracteres) |
| `JEICOB_MCP_TRANSPORT` / `_HOST` / `_PORT` | `stdio` / `127.0.0.1` / `8765` | Equivalen a `--transport`, `--host` y `--port` |

El servidor escribe en stderr un **log de auditoría** por cada tool, resource o
prompt usado: método MCP, nombre de la tool o del prompt (de un resource, solo su
tipo, `knowledge` o `reports`; `?` si no es uno conocido), `ok`/`error` y
duración. No registra argumentos, URIs ni contenido, así que no indica qué
documento concreto se leyó.

> **Privacidad:** con MCP, lo que lean las tools (documentos, fragmentos) va al
> LLM de tu cliente. Si `knowledge_base/` contiene datos clínicos, ten en cuenta
> a dónde envía los datos ese cliente; que nada salga de tu máquina solo lo
> garantiza `main_local.py` con `JEICOB_ENABLE_NETWORK=0` y el modelo de
> embeddings ya descargado.

Para depurar a mano: `npx @modelcontextprotocol/inspector uv run --extra mcp jeicob-mcp`.

**Por HTTP (streamable-http).** En vez de que cada cliente lance su propio
proceso por stdio, puedes dejar un servidor escuchando y conectar varios
clientes a él (con Qdrant en servidor, §5.3, además pueden convivir con el CLI):

```bash
export JEICOB_MCP_TOKEN=$(openssl rand -hex 32)   # obligatorio, ≥ 32 caracteres
uv run --extra mcp jeicob-mcp --transport http     # http://127.0.0.1:8765/mcp
claude mcp add --transport http jeicob http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer $JEICOB_MCP_TOKEN"
```

- Cada petición debe llevar `Authorization: Bearer <JEICOB_MCP_TOKEN>`; si no,
  responde 401. El servidor no arranca sin token.
- Por defecto solo escucha en `127.0.0.1`, con la protección contra *DNS
  rebinding* del SDK (`Host` ajeno a localhost → 421; `Origin` ajeno → 403),
  activa para cualquier dirección de loopback que pases en `--host`.
- Un `JEICOB_MCP_TRANSPORT` o `JEICOB_MCP_PORT` no válidos son un error al
  arrancar, igual que en la línea de comandos.
- `--host 0.0.0.0` (o `JEICOB_MCP_HOST`) lo expone a la red **sin TLS**: el
  token y los documentos viajarían en claro. Hazlo solo detrás de un proxy
  inverso con https.
- `--port` / `JEICOB_MCP_PORT` cambian el puerto (8765 por defecto).

### 5.3. Qdrant en servidor (CLI y MCP a la vez)

Por defecto el índice vive en `.qdrant_data/` (Qdrant local, un proceso a la
vez). Para que el CLI y una o varias sesiones MCP lo compartan, arranca el Qdrant
del `docker-compose.yml` incluido:

```bash
# 1. API key en el .env (docker compose la lee de ahí)
echo "QDRANT_API_KEY=$(openssl rand -hex 32)" >> .env
# 2. Arranca Qdrant (solo escucha en 127.0.0.1:6333; telemetría desactivada)
docker compose up -d
# 3. Apunta jeicob al servidor
echo "QDRANT_URL=http://localhost:6333" >> .env
```

Con `QDRANT_URL` definida, jeicob ignora `QDRANT_PATH`. No hace falta migrar el
índice local: la ingesta es idempotente, así que basta con volver a ejecutar
`ingest_corpus` para reconstruirlo desde `knowledge_base/`. Los embeddings se
siguen calculando en tu máquina; al servidor llegan los vectores, el texto de
cada fragmento, la ruta relativa del documento (`source`, que puede contener
datos identificativos si así se llaman tus ficheros) y su SHA-256. Todo ello
queda en el volumen de Docker `qdrant_storage`.

Para comprobarlo: `uv run python -m jeicob.rag --url http://localhost:6333` ejecuta
la prueba de ingesta y consulta en una colección temporal del servidor.

---

## 6. Cómo funciona por dentro (el "CÓMO")

### 6.1. El orquestador (`jeicob/main.py`)

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

Las **7 tools** que ve Claude:

| Tool | Envuelve | Cuándo la usa el agente |
|------|----------|-------------------------|
| `read_directory()` | `tools.read_directory` | Descubrir qué documentos hay |
| `read_file(path, offset=0)` | `tools.read_file` | Leer un documento concreto (por tramos si es largo) |
| `ingest_corpus()` | `rag.ingest_directory` | Sincronizar el índice con `knowledge_base/` (idempotente) |
| `search_corpus(question)` | `rag.query` | Buscar por significado en el corpus |
| `search_references(query)` | `references.search_references` | Descubrir papers en fuentes académicas externas |
| `download_reference(url, filename)` | `references.download_reference` + RAG | Descargar un PDF a `knowledge_base/` **e indexarlo** |
| `write_summary(...)` | `tools.write_summary` | Escribir el reporte final |

### 6.2. Las herramientas de sistema (`jeicob/tools.py`)

Funciones documentadas con **fronteras de seguridad estrictas**. Las tools nunca
lanzan excepción al llamador: devuelven el error como texto (`ERROR: ...`) para
que el agente pueda reaccionar y reintentar. La excepción es `read_chunk`, la
variante de `read_file` que usa el servidor MCP: lanza `ReadError`
(`not_found=True` si el documento no existe), porque el texto de un documento
puede empezar legítimamente por "ERROR".

- **`read_directory()`** → lista (recursivamente) los ficheros de `knowledge_base/`
  con su tamaño, en rutas relativas listas para pasar a `read_file`. Si
  `knowledge_base/` no existe o no es un directorio, devuelve `ERROR: ...`.

- **`read_file(path, offset=0, max_chars=100_000)`** → lee un documento de `knowledge_base/`:
  - PDF vía `pypdf`; texto/Markdown (`.md`, `.markdown`, `.txt`, `.rst`) directo.
  - **Confinado a `knowledge_base/`** por el helper `_resolve_within`, que
    neutraliza *path traversal* (`../`), rutas absolutas y symlinks que escapen.
  - Lee **por tramos**: devuelve como mucho `max_chars` caracteres (tope
    `MAX_READ_CHARS` = 100 000) a partir de `offset`; si queda texto, termina con
    `[...continúa: ... offset=N ...]` para que el agente pida el siguiente tramo.
    El texto extraído se cachea (por fichero, mtime y tamaño), así que paginar un
    PDF no lo vuelve a parsear entero.

- **`write_summary(filename, title, abstract, full_explanation, key_points=None,
  source=None, overwrite=False)`** → escribe el reporte final. **Garantiza la estructura en el
  propio límite de la tool**:
  - **Valida** que existan `title`, `abstract` y `full_explanation`, y que la
    explicación sea **más extensa que el abstract** (si no, devuelve `ERROR` y el
    agente regenera el contenido). Así "explicar el paper completo" no degenera en
    otro resumen breve.
  - **Ensambla** el Markdown con secciones fijas (ver §7).
  - **Solo escribe en `docs_output/`**: reduce `filename` a su nombre base (bloquea
    `../` y subrutas) y fuerza la extensión `.md`.
  - **No sobrescribe** un reporte existente (lo crea de forma atómica con
    `open("x")`): devuelve `ERROR: ya existe el reporte ...` salvo con
    `overwrite=True`, que también acepta texto como `"true"`, `"1"` o `"sí"`
    (los LLM locales a veces mandan el booleano como cadena).

### 6.3. El motor RAG (`jeicob/rag.py`)

Clase **`RAGEngine`** que encapsula Qdrant + embeddings.

- **Ingesta** (`ingest_file` / `ingest_directory`):
  1. Extrae el texto **completo** del documento (sin truncar, a diferencia de
     `read_file`).
  2. Lo **trocea** (`_chunk`) en ventanas de **1000 caracteres con 150 de solape**
     (`CHUNK_SIZE` / `CHUNK_OVERLAP`), normalizando espacios. El solape evita
     cortar ideas a mitad entre fragmentos.
  3. Crea la colección con la dimensión del modelo si no existe, y hace **`upsert`**
     de cada fragmento como `models.Document` (inferencia de embeddings **local**),
     con metadatos por fragmento: `source` (fichero origen), `chunk` (índice),
     `document` (el texto) y `sha256` (hash del fichero).
  - **Idempotente**: el id de cada fragmento es determinista
    (`uuid5("<fuente>#<n>")`). Si el fichero no cambió (mismo SHA-256) no se
    reprocesa; si cambió, se borran sus fragmentos antiguos y se reindexa; y
    `ingest_directory` elimina del índice los documentos que ya no están en
    `knowledge_base/`. `force=True` obliga a reindexar.
  - `ingest_directory(force=False, progress=None, should_stop=None)`: si
    `knowledge_base/` no existe devuelve `ERROR: ...` y **no toca el índice**
    (evita vaciarlo por una ruta mal configurada). `progress(i, total, fichero)` se
    llama antes de cada documento; `should_stop()` también, y si devuelve True la
    sincronización se interrumpe con `ERROR: sincronización interrumpida...` sin
    podar. El resultado empieza por `ERROR:` solo en esos casos o si no se pudo
    indexar ningún documento; un fallo parcial se resume en la primera línea
    (`Sincronizado con N error(es) de M documentos (detalle abajo).`) y no cuenta
    como error.
  - Reutiliza el sandbox de lectura de `tools.py` (misma frontera `knowledge_base/`).

- **Consulta** (`query(question, top_k=5, source=None)`; `source` limita la
  búsqueda a un documento):
  - Vectoriza la pregunta y hace **`query_points`** contra la colección.
  - Devuelve los `top_k` fragmentos más relevantes con su **fuente** y su **score**
    de similitud (coseno) — recuperación transversal a todo el corpus, aunque la
    respuesta esté repartida en varios documentos.

- **`count()`** (nº de fragmentos indexados) y **`close()`** (libera el lock del
  directorio on-disk).

- Usa la **API no deprecada** de qdrant-client (`upsert`/`query_points` con
  `models.Document`), no la interfaz de conveniencia `add()`/`query()`.

### 6.4. Recopilación de referencias (`jeicob/references.py`)

Cierra el ciclo de la misión ("**buscar**, ingerir y resumir"): además de trabajar
con lo que hay en `knowledge_base/`, el agente puede **descubrir y descargar**
literatura nueva de fuentes académicas. Todo con la librería estándar, **sin
dependencias ni API keys nuevas**.

- **`search_references(query, limit=5)`** → busca papers en **Semantic Scholar**
  (cubre CS + biomédico/clínico) y, si esa API falla o va saturada (su pool anónimo
  está limitado), **cae automáticamente a la API de arXiv** (muy fiable). Devuelve
  título, autores, año y la **URL de PDF descargable**.

- **`download_reference(url, filename)`** → descarga un PDF a `knowledge_base/`
  **y lo indexa automáticamente** en la base vectorial (no hace falta un
  `ingest_corpus` aparte: queda consultable al instante). **Confinado a esa
  carpeta** (mismo sandbox que `write_summary`: nombre base + `.pdf`), solo acepta
  `http(s)` hacia **IPs públicas** (protección SSRF de `netguard.py`, ver §8),
  **valida que sea un PDF** (cabecera `%PDF-` en el primer KB), limita el tamaño a 50 MB y **no
  sobrescribe** un fichero existente.
  La descarga vive en `references.py` (desacoplada); el auto-indexado se compone en
  el orquestador, que ya tiene el `RAGEngine` a mano.

Flujo completo que habilita: **descubrir → descargar+indexar → recuperar →
resumir**, todo autónomo. Disponible en las dos variantes (Claude y Ollama).

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
- `write_summary` además reduce el nombre de fichero a su base, fuerza `.md` y no
  sobrescribe un reporte existente salvo con `overwrite=true`.
- **Descargas** (`jeicob/netguard.py`): solo `http`/`https`; el host debe resolver a
  IPs **públicas** (se rechazan loopback, redes privadas, link-local como
  `169.254.169.254`…, salvo con `JEICOB_ALLOW_PRIVATE_NETWORK=1`), y la IP se vuelve a comprobar en el socket ya conectado,
  antes del TLS, para que el *DNS rebinding* no sirva. Cada redirección (máx. 5)
  se revalida, no se usan proxies del entorno y se puede restringir a una lista
  de dominios (`JEICOB_DOWNLOAD_ALLOWED_HOSTS`). También se rechazan las IPv6 que
  llevan dentro una IPv4 no pública (NAT64, 6to4, Teredo, IPv4-compatible). El
  fichero debe llevar la cabecera `%PDF-` en su primer KB (como admiten los
  lectores de PDF), pesar ≤ 50 MB y descargarse en menos de 120 s, y **no
  sobrescribe** nada de `knowledge_base/`.
- **PDFs**: como mucho 3000 páginas por documento, para que un PDF hostil no deje
  al servidor parseando sin fin.
- **Tools de red desactivables** (`JEICOB_ENABLE_NETWORK=0`, en el CLI y en el
  servidor MCP): imprescindible si `knowledge_base/` contiene datos clínicos,
  porque la combinación "datos privados + contenido no confiable (PDFs
  descargados) + salida a internet" permite exfiltrar datos con una inyección de
  prompt. Con la red desactivada el modelo no ve esas tools y, como segunda
  barrera, `netguard` rechaza cualquier petición saliente.
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
├── jeicob/
│   ├── __init__.py
│   ├── config.py           # rutas configurables por entorno
│   ├── prompts.py          # system prompt compartido por los orquestadores
│   ├── tools.py            # Fase 2 — tools de sistema (sandbox)
│   ├── rag.py              # Fase 3 — motor RAG (Qdrant + fastembed)
│   ├── references.py       # Recopilación de referencias (Semantic Scholar/arXiv)
│   ├── netguard.py         # HTTP saliente con protección SSRF
│   ├── main.py             # Fase 4 — orquestador CLI con Claude (ReAct)
│   ├── main_local.py       # Variante local gratuita con Ollama (§5.1)
│   ├── mcp_server.py       # Servidor MCP stdio y streamable-http (§5.2)
│   └── mcp_smoke.py        # Prueba de humo del servidor MCP
├── .mcp.json               # registro del servidor MCP para Claude Code
├── docker-compose.yml      # Qdrant en servidor (opcional, §5.3)
├── knowledge_base/         # entrada: papers a analizar
└── docs_output/            # salida: reportes Markdown
```

---

## 10. Probar cada capa por separado

Cada módulo trae una prueba de humo ejecutable:

```bash
uv run python -m jeicob.tools    # lista KB, valida escritura y sandbox
uv run python -m jeicob.rag      # ingesta idempotente + consulta end-to-end (descarga el modelo)
uv run --extra mcp python -m jeicob.mcp_smoke         # servidor MCP por stdio y HTTP (sin modelo)
uv run --extra mcp python -m jeicob.mcp_smoke --full  # + RAG, progreso y lock de Qdrant
uv run python -m jeicob.rag --url http://localhost:6333  # RAG contra Qdrant en servidor (§5.3)
```

---

## 11. Extensiones futuras

- **Chunking por secciones** (títulos/párrafos) en vez de por caracteres.
- **`top_k` configurable** en `search_corpus` del CLI (el servidor MCP ya lo
  admite, de 1 a 20).
- Embeddings SOTA vía **Voyage AI** (partner de Anthropic) si se prioriza calidad
  sobre aislamiento.
- Generación de reportes en **PDF/DOCX** (el sandbox de Anthropic trae
  `python-docx`, `matplotlib`, etc.).
