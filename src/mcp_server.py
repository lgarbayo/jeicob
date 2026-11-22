"""Servidor MCP de jeicob (transporte stdio).

Expone las mismas herramientas que el orquestador CLI (`src/main.py`) a
cualquier cliente MCP —Claude Code, Claude Desktop…—, que pasa a ser el
"cerebro" del agente. Reutiliza sin cambios `tools.py`, `rag.py` y
`references.py`; aquí solo se adapta cada función al protocolo:

  - Los errores de las tools (cadenas `"ERROR: ..."`) se convierten en
    `ToolError`, para que el cliente reciba `isError=true`.
  - Cada tool declara sus *annotations* (solo lectura, red, destructiva…),
    que el cliente usa para decidir cuándo pedir confirmación.
  - Todo acceso a Qdrant pasa por `RagService`: un único hilo dedicado
    (QdrantClient local no es seguro entre hilos), apertura perezosa y
    liberación del lock on-disk tras un rato sin uso, para que el CLI u otra
    sesión MCP puedan usar el índice.

Ejecución (requiere el extra `mcp`):
    uv run --extra mcp python -m src.mcp_server

Variables de entorno propias (además de las de `src/config.py`):

| Variable                     | Por defecto | Efecto                                         |
|------------------------------|-------------|------------------------------------------------|
| `JEICOB_ENABLE_NETWORK`      | `1`         | `0` no registra search/download_reference      |
| `JEICOB_QDRANT_IDLE_RELEASE` | `30`        | segundos sin uso antes de liberar el lock      |
| `JEICOB_QDRANT_LOCK_TIMEOUT` | `10`        | segundos esperando un lock ocupado             |
| `JEICOB_MCP_PREFETCH`        | `1`         | `0` no descarga el modelo al arrancar          |

Importante (stdio): nada de este módulo puede escribir en stdout; los logs van
a stderr.
"""

import asyncio
import logging
import os
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import config, prompts, references, tools

if TYPE_CHECKING:
    from .rag import RAGEngine

log = logging.getLogger("jeicob.mcp")


def _env_flag(name: str, default: bool) -> bool:
    valor = os.getenv(name)
    if valor is None:
        return default
    return valor.strip().lower() not in {"0", "false", "no", "off", ""}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


NETWORK_ENABLED = _env_flag("JEICOB_ENABLE_NETWORK", True)
IDLE_RELEASE_S = _env_float("JEICOB_QDRANT_IDLE_RELEASE", 30.0)
LOCK_TIMEOUT_S = _env_float("JEICOB_QDRANT_LOCK_TIMEOUT", 10.0)
PREFETCH = _env_flag("JEICOB_MCP_PREFETCH", True)

# Tramo por defecto de read_file vía MCP. El límite de salida de Claude Code
# ronda los 25k tokens; 20k caracteres (~5-6k tokens) deja margen de sobra.
MCP_READ_CHARS = 20_000


# --- Acceso serializado a Qdrant ----------------------------------------------


class RagService:
    """Dueño único del `RAGEngine` dentro del proceso.

    - Un solo hilo ejecuta todo lo que toca Qdrant (el cliente local no es
      thread-safe y en macOS sqlite exige el mismo hilo).
    - El motor se abre en la primera llamada y se cierra tras
      `idle_release` segundos sin uso: así se libera el lock de
      `.qdrant_data` para el CLI u otras sesiones.
    - Si el lock lo tiene otro proceso, reintenta hasta `lock_timeout`
      segundos y después falla con un mensaje accionable.
    """

    def __init__(self, idle_release: float, lock_timeout: float) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rag")
        self._engine: RAGEngine | None = None
        self._idle_release = idle_release
        self._lock_timeout = lock_timeout
        self._timer: threading.Timer | None = None
        self._timer_lock = threading.Lock()
        self._last_used = 0.0

    # Todo lo de abajo con prefijo _sync corre en el hilo "rag".

    def _sync_open(self) -> "RAGEngine":
        if self._engine is not None:
            return self._engine
        from .rag import RAGEngine  # import diferido: qdrant/fastembed pesan

        limite = time.monotonic() + self._lock_timeout
        while True:
            try:
                self._engine = RAGEngine()
                log.info("Qdrant abierto en %s", config.QDRANT_PATH)
                return self._engine
            except RuntimeError as exc:
                if "already accessed" not in str(exc):
                    raise
                if time.monotonic() >= limite:
                    raise ToolError(
                        "La base vectorial está en uso por otro proceso (el CLI "
                        "`src.main` u otra sesión MCP). Ciérralo y reintenta; para "
                        "uso concurrente real hace falta Qdrant en servidor."
                    ) from exc
                time.sleep(0.5)

    def _sync_close_if_idle(self) -> None:
        if self._engine is None:
            return
        if time.monotonic() - self._last_used < self._idle_release:
            return
        self._engine.close()
        self._engine = None
        log.info("Qdrant liberado tras %.0fs sin uso", self._idle_release)

    def _sync_close(self) -> None:
        if self._engine is not None:
            self._engine.close()
            self._engine = None

    def _schedule_release(self) -> None:
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(
                self._idle_release,
                lambda: self._executor.submit(self._sync_close_if_idle),
            )
            self._timer.daemon = True
            self._timer.start()

    async def run(self, fn: Callable[["RAGEngine"], Any]) -> Any:
        """Ejecuta `fn(engine)` en el hilo de Qdrant y devuelve su resultado."""

        def job() -> Any:
            try:
                return fn(self._sync_open())
            finally:
                self._last_used = time.monotonic()

        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._executor, job)
        finally:
            self._schedule_release()

    def shutdown(self) -> None:
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
        self._executor.submit(self._sync_close).result(timeout=30)
        self._executor.shutdown(wait=True)


_rag = RagService(IDLE_RELEASE_S, LOCK_TIMEOUT_S)


@asynccontextmanager
async def _lifespan(_server: MCPServer) -> AsyncIterator[None]:
    if PREFETCH:
        # Descarga el modelo de embeddings en segundo plano (no toca Qdrant):
        # así la primera consulta no supera el timeout del cliente.
        def _prefetch() -> None:
            try:
                from .rag import prefetch_model

                log.info(prefetch_model())
            except Exception as exc:  # noqa: BLE001
                log.warning("No se pudo precargar el modelo de embeddings: %s", exc)

        threading.Thread(target=_prefetch, name="prefetch", daemon=True).start()
    try:
        yield
    finally:
        await asyncio.to_thread(_rag.shutdown)


# --- Servidor -----------------------------------------------------------------

INSTRUCTIONS = f"""\
jeicob: herramientas para investigar literatura científica (3D Gaussian \
Splatting, DICOM, normativas clínicas) con RAG local y escribir reportes en \
Markdown.

{prompts.TOOLS_GUIDE}

{prompts.WORKFLOW}

Formato de los reportes: {prompts.REPORT_RULES}
"""

mcp = MCPServer("jeicob", instructions=INSTRUCTIONS, lifespan=_lifespan)

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, idempotentHint=True, openWorldHint=False
)


def _ok(resultado: str) -> str:
    """Convierte la convención `"ERROR: ..."` de las tools en un error MCP."""
    if resultado.startswith("ERROR"):
        raise ToolError(resultado)
    return resultado


# --- Tools de ficheros --------------------------------------------------------


@mcp.tool(title="Listar documentos", annotations=_READ_ONLY, structured_output=False)
def read_directory() -> str:
    """Lista los documentos disponibles en knowledge_base/ (rutas relativas y
    tamaño). Úsala para descubrir qué material hay antes de leer o consultar."""
    return _ok(tools.read_directory())


@mcp.tool(title="Leer documento", annotations=_READ_ONLY, structured_output=False)
def read_file(path: str, offset: int = 0, max_chars: int = MCP_READ_CHARS) -> str:
    """Lee un documento de knowledge_base/ (PDF o texto/Markdown) por tramos.

    Si la respuesta acaba en "[...continúa ... offset=N ...]", vuelve a
    llamarla con ese offset para leer el siguiente tramo.

    Args:
        path: Ruta del documento relativa a knowledge_base/ (la que da
            read_directory).
        offset: Carácter desde el que leer (0 = inicio).
        max_chars: Tamaño máximo del tramo (por defecto 20000).
    """
    return _ok(tools.read_file(path, offset=offset, max_chars=max_chars))


@mcp.tool(
    title="Escribir reporte",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,  # sobrescribe un reporte con el mismo nombre
        idempotentHint=True,
        openWorldHint=False,
    ),
    structured_output=False,
)
def write_summary(
    filename: str,
    title: str,
    abstract: str,
    full_explanation: str,
    key_points: list[str] | None = None,
    source: str | None = None,
) -> str:
    """Escribe el reporte final en Markdown dentro de docs_output/.

    El reporte SIEMPRE lleva un abstract conciso (3-6 frases) y una
    explicación completa del paper —más extensa que el abstract— que cubra
    problema, metodología, resultados, limitaciones y relevancia clínica. Si
    no se cumple, devuelve un error para que regeneres el contenido.

    Args:
        filename: Nombre del fichero de salida, p. ej. "resumen_dicom.md".
        title: Título del reporte (normalmente el título del paper).
        abstract: Resumen conciso del paper, de 3 a 6 frases.
        full_explanation: Explicación completa y detallada del paper.
        key_points: Puntos clave o hallazgos destacados (opcional).
        source: Documento fuente, DOI o URL (opcional).
    """
    return _ok(
        tools.write_summary(
            filename=filename,
            title=title,
            abstract=abstract,
            full_explanation=full_explanation,
            key_points=key_points,
            source=source,
        )
    )


# --- Tools de RAG -------------------------------------------------------------


@mcp.tool(
    title="Indexar corpus",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    structured_output=False,
)
async def ingest_corpus(ctx: Context, force: bool = False) -> str:
    """Sincroniza la base vectorial con knowledge_base/: indexa los documentos
    nuevos o modificados y quita los que ya no existen. Es idempotente;
    ejecútala antes de usar search_corpus.

    Args:
        force: Reindexar todos los documentos aunque no hayan cambiado.
    """
    loop = asyncio.get_running_loop()

    def progreso(i: int, total: int, fichero: str) -> None:
        asyncio.run_coroutine_threadsafe(
            ctx.report_progress(i, total, message=f"Indexando {fichero}"), loop
        )

    return _ok(
        await _rag.run(lambda rag: rag.ingest_directory(force=force, progress=progreso))
    )


@mcp.tool(title="Buscar en el corpus", annotations=_READ_ONLY, structured_output=False)
async def search_corpus(
    question: str, top_k: int = 5, source: str | None = None
) -> str:
    """Búsqueda semántica en el corpus indexado: devuelve los fragmentos más
    relevantes con su documento de origen y puntuación. Úsala para preguntas
    que cruzan varios documentos o cuando no sabes dónde está la respuesta.

    Args:
        question: Pregunta o tema a buscar.
        top_k: Número de fragmentos (1-20, por defecto 5).
        source: Limitar la búsqueda a un documento (ruta relativa a
            knowledge_base/).
    """
    return _ok(await _rag.run(lambda rag: rag.query(question, top_k, source)))


# --- Tools de red (desactivables con JEICOB_ENABLE_NETWORK=0) -----------------


def search_references(query: str, limit: int = 5) -> str:
    """Busca papers en fuentes académicas externas (Semantic Scholar, con
    arXiv como respaldo) y devuelve candidatos con su URL de PDF. Úsala para
    descubrir literatura que aún no está en knowledge_base/.

    La consulta se envía a un servicio externo: no incluyas datos de pacientes.

    Args:
        query: Términos de búsqueda (tema, título, autores).
        limit: Número máximo de resultados (1-20, por defecto 5).
    """
    return _ok(references.search_references(query, limit))


async def download_reference(url: str, filename: str) -> str:
    """Descarga a knowledge_base/ un PDF encontrado con search_references y lo
    indexa automáticamente (no hace falta ingest_corpus después).

    Args:
        url: URL http(s) directa al PDF.
        filename: Nombre con el que guardarlo, p. ej. "kerbl2023.pdf".
    """
    descarga = _ok(
        await asyncio.to_thread(references.download_reference, url, filename)
    )
    nombre = references.normalized_pdf_name(filename)
    indexado = _ok(await _rag.run(lambda rag: rag.ingest_file(nombre)))
    return f"{descarga}\n{indexado}"


if NETWORK_ENABLED:
    mcp.tool(
        title="Buscar referencias",
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=True, openWorldHint=True
        ),
        structured_output=False,
    )(search_references)
    mcp.tool(
        title="Descargar referencia",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,  # sobrescribe un PDF con el mismo nombre
            idempotentHint=True,
            openWorldHint=True,
        ),
        structured_output=False,
    )(download_reference)


def main() -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info(
        "jeicob MCP · KB=%s · salida=%s · red=%s",
        config.KNOWLEDGE_BASE_DIR,
        config.DOCS_OUTPUT_DIR,
        "sí" if NETWORK_ENABLED else "no",
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
