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
import urllib.parse
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import (
    ResourceError,
    ResourceNotFoundError,
    ToolError,
)
from mcp.types import (
    Completion,
    PromptReference,
    ResourceTemplateReference,
    ToolAnnotations,
)
from mcp.types import Resource as MCPResource

from . import config, prompts, references, tools

if TYPE_CHECKING:
    from .rag import RAGEngine

log = logging.getLogger("jeicob.mcp")
audit_log = logging.getLogger("jeicob.audit")


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

KNOWLEDGE_URI = "jeicob://knowledge/"
REPORTS_URI = "jeicob://reports/"

# Un resource lo adjunta el usuario entero (p. ej. una mención @ en Claude
# Code), así que se permite más que un tramo de read_file (~15k tokens).
RESOURCE_CHARS = 60_000


def _report_files() -> list[Path]:
    if not tools.DOCS_OUTPUT_DIR.is_dir():
        return []
    return sorted(
        p
        for p in tools.DOCS_OUTPUT_DIR.glob("*.md")
        if p.is_file() and not p.name.startswith(("_", "."))
    )


def _kb_files() -> list[str]:
    if not tools.KNOWLEDGE_BASE_DIR.is_dir():
        return []
    rutas = []
    for p in sorted(tools.KNOWLEDGE_BASE_DIR.rglob("*")):
        rel = p.relative_to(tools.KNOWLEDGE_BASE_DIR)
        if p.is_dir() or any(parte.startswith(".") for parte in rel.parts):
            continue
        if not tools._is_within(tools.KNOWLEDGE_BASE_DIR, p):
            continue
        rutas.append(str(rel))
    return rutas


class JeicobServer(MCPServer):
    """MCPServer cuyo listado de resources refleja el disco en cada petición:
    cada documento de knowledge_base/ y cada reporte de docs_output/ aparece
    como un resource concreto (las plantillas resuelven la lectura)."""

    async def list_resources(self) -> list[MCPResource]:
        recursos = await super().list_resources()
        for rel in _kb_files():
            recursos.append(
                MCPResource(
                    uri=KNOWLEDGE_URI + urllib.parse.quote(rel),
                    name=rel,
                    title=Path(rel).stem,
                    description="Documento de knowledge_base/ (texto extraído)",
                    mime_type="text/plain",
                )
            )
        for reporte in _report_files():
            recursos.append(
                MCPResource(
                    uri=REPORTS_URI + urllib.parse.quote(reporte.name),
                    name=f"reports/{reporte.name}",
                    title=reporte.stem,
                    description="Reporte generado en docs_output/",
                    mime_type="text/markdown",
                )
            )
        return recursos


_AUDITED = {"tools/call", "resources/read", "prompts/get"}


async def _audit(ctx: Any, call_next: Callable) -> Any:
    """Log de auditoría a stderr: qué se usó, con qué resultado y cuánto tardó.

    Nunca registra argumentos ni contenido (consultas, rutas de documentos,
    texto): pueden contener datos clínicos. De un resource solo se anota el
    tipo (knowledge / reports).
    """
    metodo = getattr(ctx, "method", None)
    if metodo not in _AUDITED:
        return await call_next(ctx)
    params = getattr(ctx, "params", None)
    params = params if isinstance(params, dict) else {}
    if metodo == "resources/read":
        uri = str(params.get("uri", ""))
        objetivo = uri.split("/")[2] if uri.startswith("jeicob://") else "?"
    else:
        objetivo = str(params.get("name", "?"))
    inicio = time.monotonic()
    estado = "ok"
    try:
        resultado = await call_next(ctx)
        es_error = getattr(resultado, "is_error", None)
        if es_error is None and isinstance(resultado, dict):
            es_error = resultado.get("isError")
        if es_error:
            estado = "error"
        return resultado
    except BaseException:
        estado = "error"
        raise
    finally:
        audit_log.info(
            "%s %s %s %.0fms",
            metodo,
            objetivo,
            estado,
            (time.monotonic() - inicio) * 1000,
        )


mcp = JeicobServer(
    "jeicob", instructions=INSTRUCTIONS, lifespan=_lifespan, middleware=[_audit]
)

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, idempotentHint=True, openWorldHint=False
)


async def _notify_resources_changed(ctx: Context) -> None:
    """Avisa al cliente de que hay un documento o reporte nuevo (best effort:
    un cliente que no lo soporte no debe hacer fallar la tool)."""
    try:
        # Protocolo 2026-07-28: eventos para los clientes suscritos con
        # `subscriptions/listen`.
        await ctx.notify_resources_changed()
    except Exception as exc:  # noqa: BLE001
        log.debug("notify_resources_changed falló: %s", exc)
    try:
        # Clientes con versiones anteriores del protocolo:
        # notifications/resources/list_changed clásica.
        enviar = getattr(
            ctx.request_context.session, "send_resource_list_changed", None
        )
        if enviar is not None:
            await enviar()
    except Exception as exc:  # noqa: BLE001
        log.debug("send_resource_list_changed falló: %s", exc)


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
        destructiveHint=True,  # con overwrite=true reemplaza un reporte
        idempotentHint=True,
        openWorldHint=False,
    ),
    structured_output=False,
)
async def write_summary(
    ctx: Context,
    filename: str,
    title: str,
    abstract: str,
    full_explanation: str,
    key_points: list[str] | None = None,
    source: str | None = None,
    overwrite: bool = False,
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
        overwrite: True para reemplazar un reporte que ya exista con ese
            nombre (p. ej. al mejorar un reporte). Por defecto no sobrescribe.
    """
    resultado = _ok(
        await asyncio.to_thread(
            tools.write_summary,
            filename=filename,
            title=title,
            abstract=abstract,
            full_explanation=full_explanation,
            key_points=key_points,
            source=source,
            overwrite=overwrite,
        )
    )
    await _notify_resources_changed(ctx)
    return resultado


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


async def download_reference(ctx: Context, url: str, filename: str) -> str:
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
    await _notify_resources_changed(ctx)
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
            destructiveHint=False,  # nunca sobrescribe un fichero existente
            idempotentHint=False,
            openWorldHint=True,
        ),
        structured_output=False,
    )(download_reference)


# --- Resources ----------------------------------------------------------------


@mcp.resource(
    KNOWLEDGE_URI + "{+path}",
    name="knowledge",
    title="Documento de knowledge_base",
    description="Texto extraído de un documento de knowledge_base/ (PDF, MD, TXT).",
    mime_type="text/plain",
)
def knowledge_resource(path: str) -> str:
    texto = tools.read_file(
        urllib.parse.unquote(path), offset=0, max_chars=RESOURCE_CHARS
    )
    if texto.startswith("ERROR"):
        if "no existe" in texto:
            raise ResourceNotFoundError(texto)
        raise ResourceError(texto)
    return texto


@mcp.resource(
    REPORTS_URI + "{name}",
    name="report",
    title="Reporte generado",
    description="Reporte Markdown de docs_output/.",
    mime_type="text/markdown",
)
def report_resource(name: str) -> str:
    nombre = urllib.parse.unquote(name)
    try:
        ruta = tools._resolve_within(tools.DOCS_OUTPUT_DIR, nombre)
    except ValueError as exc:
        raise ResourceError(str(exc)) from exc
    if ruta.suffix.lower() != ".md" or not ruta.is_file():
        raise ResourceNotFoundError(f"No existe el reporte '{nombre}' en docs_output/.")
    return ruta.read_text(encoding="utf-8", errors="replace")


# --- Prompts (en Claude Code aparecen como /mcp__jeicob__<nombre>) -------------


@mcp.prompt(title="Reporte de investigación")
def research_report(topic: str, filename: str | None = None) -> str:
    """Investiga un tema con el corpus local (y fuentes externas si hace
    falta) y escribe un reporte en docs_output/."""
    destino = f"`{filename}`" if filename else "un nombre de fichero descriptivo (.md)"
    externas = (
        "Si el corpus no basta, busca literatura nueva con search_references y "
        "descarga los papers relevantes con download_reference (ya los indexa)."
        if NETWORK_ENABLED
        else "Las fuentes externas están desactivadas: trabaja solo con el corpus."
    )
    return f"""\
Investiga este tema y escribe un reporte: {topic}

1. Revisa qué hay en local con read_directory y search_corpus. Si search_corpus \
dice que no hay nada indexado, ejecuta antes ingest_corpus.
2. {externas}
3. Lee en detalle, con read_file (por tramos), los documentos más relevantes.
4. Escribe el reporte con write_summary en {destino}.

{prompts.REPORT_RULES}
Responde en español."""


@mcp.prompt(title="Resumir documento")
def summarize_document(path: str) -> str:
    """Genera el reporte completo de un documento concreto de knowledge_base/."""
    return f"""\
Escribe un reporte completo del documento `{path}` de knowledge_base/.

1. Léelo entero con read_file. Es largo: sigue llamándola con el offset que \
indica "[...continúa ...]" hasta terminar.
2. Escribe el reporte con write_summary (source="{path}").

{prompts.REPORT_RULES}
Responde en español."""


@mcp.prompt(title="Preguntar al corpus")
def ask_corpus(question: str) -> str:
    """Responde una pregunta usando solo la literatura indexada."""
    return f"""\
Responde usando solo la literatura de knowledge_base/: {question}

Usa search_corpus (y read_file si necesitas más contexto de un documento). Si el \
corpus no está indexado, ejecuta antes ingest_corpus. Cita el documento de cada \
afirmación y di claramente si el corpus no contiene la respuesta.
Responde en español."""


@mcp.completion()
async def _complete(ref, argument, _context) -> Completion | None:
    """Autocompleta rutas de documentos y nombres de reportes."""
    candidatos: list[str] = []
    if argument.name == "path" and (
        isinstance(ref, ResourceTemplateReference)
        or (isinstance(ref, PromptReference) and ref.name == "summarize_document")
    ):
        candidatos = _kb_files()
    elif argument.name == "name" and isinstance(ref, ResourceTemplateReference):
        candidatos = [p.name for p in _report_files()]
    if not candidatos:
        return None
    prefijo = (argument.value or "").lower()
    valores = [c for c in candidatos if c.lower().startswith(prefijo)]
    return Completion(
        values=valores[:100], total=len(valores), has_more=len(valores) > 100
    )


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
