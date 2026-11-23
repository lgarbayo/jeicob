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
from concurrent.futures import TimeoutError as FuturesTimeout
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import (
    ResourceError,
    ResourceNotFoundError,
    ToolError,
)
from mcp.shared.exceptions import MCPError
from mcp.types import (
    INVALID_PARAMS,
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
    - Si el lock lo tiene otro proceso, cada petición espera como mucho
      `lock_timeout` segundos **desde que se hizo** (no desde que le toca el
      turno): N peticiones en cola no suman N plazos.
    - Al cerrar el servidor o cancelar una llamada, la ingesta en curso se
      detiene entre documento y documento, y las peticiones en cola no llegan a
      ejecutarse.
    """

    def __init__(self, idle_release: float, lock_timeout: float) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rag")
        self._engine: RAGEngine | None = None
        self._idle_release = idle_release
        self._lock_timeout = lock_timeout
        self._timer: threading.Timer | None = None
        self._timer_lock = threading.Lock()
        self._last_used = 0.0
        self._stopping = threading.Event()

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    # Todo lo de abajo con prefijo _sync corre en el hilo "rag".

    def _sync_open(self, deadline: float) -> "RAGEngine":
        if self._engine is not None:
            return self._engine
        from .rag import RAGEngine  # import diferido: qdrant/fastembed pesan

        while True:
            try:
                self._engine = RAGEngine()
                log.info("Qdrant abierto en %s", config.QDRANT_PATH)
                return self._engine
            except RuntimeError as exc:
                if "already accessed" not in str(exc):
                    raise
                if time.monotonic() >= deadline or self._stopping.is_set():
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

    def _submit_idle_close(self) -> None:
        try:
            self._executor.submit(self._sync_close_if_idle)
        except RuntimeError:  # executor ya cerrado (apagado del servidor)
            pass

    def _schedule_release(self) -> None:
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
            if self._stopping.is_set():
                return
            self._timer = threading.Timer(self._idle_release, self._submit_idle_close)
            self._timer.daemon = True
            self._timer.start()

    async def run(self, fn: Callable[["RAGEngine", Callable[[], bool]], Any]) -> Any:
        """Ejecuta `fn(engine, should_stop)` en el hilo de Qdrant.

        `should_stop()` pasa a True si el cliente cancela la llamada o el
        servidor se está cerrando; las operaciones largas (la ingesta) lo
        consultan para abandonar a tiempo. Las excepciones inesperadas llegan
        al cliente como `ToolError` con su tipo y mensaje.
        """
        cancelada = threading.Event()
        deadline = time.monotonic() + self._lock_timeout

        def should_stop() -> bool:
            return cancelada.is_set() or self._stopping.is_set()

        def job() -> Any:
            if should_stop():
                raise ToolError("Operación cancelada: el servidor se está cerrando.")
            try:
                return fn(self._sync_open(deadline), should_stop)
            except ToolError:
                raise
            except Exception as exc:
                log.warning("Fallo en la base vectorial: %s", type(exc).__name__)
                raise ToolError(
                    f"Error interno de la base vectorial ({type(exc).__name__}): {exc}"
                ) from exc
            finally:
                self._last_used = time.monotonic()
                if self._stopping.is_set():
                    self._sync_close()

        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._executor, job)
        except asyncio.CancelledError:
            cancelada.set()
            raise
        finally:
            self._schedule_release()

    def shutdown(self, timeout: float = 2.0) -> None:
        """Detiene el servicio y libera Qdrant.

        No bloquea el cierre más de `timeout` segundos: si hay una ingesta en
        marcha, esta se detiene al terminar el documento actual y cierra Qdrant
        ella misma (ver `job()`); las peticiones en cola se descartan.
        """
        self._stopping.set()
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
        cierre = self._executor.submit(self._sync_close)
        self._executor.shutdown(wait=False)
        try:
            cierre.result(timeout=timeout)
        except FuturesTimeout:
            log.warning(
                "Cierre con una operación en curso: Qdrant se liberará al "
                "terminar el documento actual."
            )


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

{prompts.tools_guide(network=NETWORK_ENABLED)}

{prompts.WORKFLOW if NETWORK_ENABLED else prompts.WORKFLOW_LOCAL}

Formato de los reportes: {prompts.REPORT_RULES}
"""

KNOWLEDGE_URI = "jeicob://knowledge/"
REPORTS_URI = "jeicob://reports/"

# Un resource lo adjunta el usuario entero (p. ej. una mención @ en Claude
# Code), así que se permite más que un tramo de read_file (~15k tokens).
RESOURCE_CHARS = 60_000


# Formatos que read_file sabe leer: lo que se anuncia como resource tiene que
# poder leerse después.
_KB_SUFFIXES = {".pdf", *tools._TEXT_SUFFIXES}


def _report_files() -> list[Path]:
    if not tools.DOCS_OUTPUT_DIR.is_dir():
        return []
    return sorted(
        p
        for p in tools.DOCS_OUTPUT_DIR.iterdir()
        # write_summary conserva la extensión que le dan (.md, .MD, .Md).
        if p.suffix.lower() == ".md"
        and p.is_file()
        and not p.name.startswith(("_", "."))
    )


def _kb_files() -> list[str]:
    if not tools.KNOWLEDGE_BASE_DIR.is_dir():
        return []
    rutas = []
    for p in sorted(tools.KNOWLEDGE_BASE_DIR.rglob("*")):
        rel = p.relative_to(tools.KNOWLEDGE_BASE_DIR)
        if p.is_dir() or any(parte.startswith(".") for parte in rel.parts):
            continue
        if p.suffix.lower() not in _KB_SUFFIXES:
            continue
        if not tools._is_within(tools.KNOWLEDGE_BASE_DIR, p):
            continue
        rutas.append(rel.as_posix())
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

    async def get_prompt(self, name: str, arguments=None, context=None):
        # El SDK convierte cualquier fallo de un prompt en -32603 (error
        # interno); un prompt desconocido o sin sus argumentos obligatorios es
        # un fallo del cliente y debe llegar como INVALID_PARAMS.
        prompt = self._prompt_manager.get_prompt(name)
        if prompt is None:
            raise MCPError(INVALID_PARAMS, f"Prompt desconocido: {name}")
        faltan = [
            a.name
            for a in (prompt.arguments or [])
            if a.required and not (arguments or {}).get(a.name)
        ]
        if faltan:
            raise MCPError(
                INVALID_PARAMS,
                f"Faltan argumentos obligatorios del prompt '{name}': "
                + ", ".join(faltan),
            )
        return await super().get_prompt(name, arguments, context)


_AUDITED = {"tools/call", "resources/read", "prompts/get"}

# El log de auditoría solo escribe valores de estos conjuntos cerrados: un
# nombre o una URI elegidos por el cliente podrían llevar saltos de línea
# (falsificar entradas) o datos clínicos.
_AUDIT_NAMES = {
    "read_directory",
    "read_file",
    "write_summary",
    "ingest_corpus",
    "search_corpus",
    "search_references",
    "download_reference",
    "research_report",
    "summarize_document",
    "ask_corpus",
}
_AUDIT_RESOURCE_KINDS = {"knowledge", "reports"}


def _audit_target(metodo: str, params: dict) -> str:
    if metodo == "resources/read":
        uri = str(params.get("uri", ""))
        partes = uri.split("/")
        tipo = partes[2] if uri.startswith("jeicob://") and len(partes) > 2 else ""
        return tipo if tipo in _AUDIT_RESOURCE_KINDS else "?"
    nombre = params.get("name")
    return nombre if nombre in _AUDIT_NAMES else "?"


async def _audit(ctx: Any, call_next: Callable) -> Any:
    """Log de auditoría a stderr: qué se usó, con qué resultado y cuánto tardó.

    Nunca registra argumentos ni contenido (consultas, rutas de documentos,
    texto): pueden contener datos clínicos. De un resource solo se anota el
    tipo (knowledge / reports).
    """
    metodo = getattr(ctx, "method", None)
    # Sin request_id es una notificación: el servidor no la ejecuta como
    # petición, así que no hay nada que auditar.
    if metodo not in _AUDITED or getattr(ctx, "request_id", None) is None:
        return await call_next(ctx)
    params = getattr(ctx, "params", None)
    objetivo = _audit_target(metodo, params if isinstance(params, dict) else {})
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


# Prefijos exactos con los que tools.py, rag.py y references.py señalan un
# fallo. Se comparan literalmente: un texto que empiece por "ERRORES" o
# "ERROR 404" no es un fallo. (read_file, que devuelve contenido arbitrario,
# no pasa por aquí: usa la excepción de tools.read_chunk.)
_ERROR_PREFIXES = ("ERROR: ", "ERROR al ")


def _ok(resultado: str) -> str:
    """Convierte la convención `"ERROR: ..."` de las tools en un error MCP."""
    if resultado.startswith(_ERROR_PREFIXES):
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
        max_chars: Tamaño máximo del tramo (por defecto y como máximo 20000).
    """
    # Tope propio de la capa MCP: un tramo mayor desborda el límite de salida
    # de las tools del cliente (tools.MAX_READ_CHARS es para el CLI).
    max_chars = min(max_chars, MCP_READ_CHARS)
    try:
        return tools.read_chunk(path, offset=offset, max_chars=max_chars)
    except tools.ReadError as exc:
        raise ToolError(f"ERROR: {exc}") from exc


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
        # Poda del índice los documentos que ya no están en knowledge_base/.
        destructiveHint=True,
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
        # Corre en el hilo rag: si el servidor se cierra, el loop puede no
        # existir ya; el progreso es best effort.
        if _rag.stopping or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(
                ctx.report_progress(i, total, message=f"Indexando {fichero}"), loop
            )
        except RuntimeError:
            pass

    return _ok(
        await _rag.run(
            lambda rag, should_stop: rag.ingest_directory(
                force=force, progress=progreso, should_stop=should_stop
            )
        )
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
    return _ok(await _rag.run(lambda rag, _stop: rag.query(question, top_k, source)))


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
    # El PDF ya está en disco: aunque falle el indexado, la descarga se
    # confirma (y se avisa del documento nuevo) en vez de perderse en un error.
    try:
        indexado = _ok(await _rag.run(lambda rag, _stop: rag.ingest_file(nombre)))
    except ToolError as exc:
        indexado = (
            f"AVISO: el PDF se guardó pero no se pudo indexar ({exc}). "
            "Ejecuta ingest_corpus más tarde."
        )
    finally:
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
    # `path` llega ya decodificado por el SDK: decodificarlo otra vez rompería
    # los nombres que contienen un "%XX" literal.
    try:
        return tools.read_chunk(path, offset=0, max_chars=RESOURCE_CHARS)
    except tools.ReadError as exc:
        if exc.not_found:
            raise ResourceNotFoundError(str(exc)) from exc
        raise ResourceError(str(exc)) from exc


@mcp.resource(
    REPORTS_URI + "{name}",
    name="report",
    title="Reporte generado",
    description="Reporte Markdown de docs_output/.",
    mime_type="text/markdown",
)
def report_resource(name: str) -> str:
    nombre = name  # ya decodificado por el SDK (ver knowledge_resource)
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
    es_plantilla = isinstance(ref, ResourceTemplateReference)
    if argument.name == "path" and (
        es_plantilla
        or (isinstance(ref, PromptReference) and ref.name == "summarize_document")
    ):
        candidatos = _kb_files()
    elif argument.name == "name" and es_plantilla:
        candidatos = [p.name for p in _report_files()]
    if not candidatos:
        return None
    if es_plantilla:
        # El valor se sustituye tal cual en la URI: un "#" o "?" sin codificar
        # la partiría. {+path} admite "/", {name} no.
        seguro = "/" if argument.name == "path" else ""
        candidatos = [urllib.parse.quote(c, safe=seguro) for c in candidatos]
    prefijo = (argument.value or "").lower()
    valores = [c for c in candidatos if c.lower().startswith(prefijo)]
    return Completion(
        values=valores[:100], total=len(valores), has_more=len(valores) > 100
    )


def main() -> None:
    # force=True: sustituye el RichHandler que instala el SDK (parte las
    # líneas a 80 columnas e ignora este formato).
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    # Los INFO del SDK ("Tool X failed: <mensaje>") repiten rutas, URLs y
    # textos de error de las peticiones; nuestra auditoría ya registra el
    # resultado sin contenido.
    logging.getLogger("mcp").setLevel(logging.WARNING)
    log.info(
        "jeicob MCP · KB=%s · salida=%s · red=%s",
        config.KNOWLEDGE_BASE_DIR,
        config.DOCS_OUTPUT_DIR,
        "sí" if NETWORK_ENABLED else "no",
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
