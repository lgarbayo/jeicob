"""Orquestador del agente jeicob (CLI interactivo).

Conecta las tools de sistema (Fase 2) y el motor RAG (Fase 3) a Claude
mediante *tool calling* nativo. Usa el Tool Runner del SDK de Anthropic, que
ejecuta el bucle ReAct automáticamente: el modelo decide qué tool llamar, el
runner la ejecuta, le devuelve el resultado y repite hasta terminar el turno.

Ejecución:
    uv run python -m jeicob.main
"""

from __future__ import annotations

import os
import sys

from anthropic import Anthropic, beta_tool
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from . import config, references, tools
from .prompts import system_prompt
from .rag import RAGEngine

console = Console()

MODEL = os.getenv("JEICOB_MODEL", "claude-opus-4-8")
MAX_TOKENS = 16000


# Motor RAG perezoso: no abre Qdrant hasta que una tool lo necesita, para que
# importar este módulo no bloquee el directorio on-disk.
_rag: RAGEngine | None = None


def _get_rag() -> RAGEngine:
    global _rag
    if _rag is None:
        _rag = RAGEngine()
    return _rag


# --- Tools expuestas al modelo (envuelven Fases 2 y 3) ------------------------


@beta_tool
def read_directory() -> str:
    """Lista los documentos de referencia disponibles en knowledge_base/.

    Úsala para descubrir qué material hay antes de leer, indexar o consultar.
    """
    return tools.read_directory()


@beta_tool
def read_file(path: str, offset: int = 0) -> str:
    """Lee un documento de knowledge_base/ (por tramos si es largo).

    Soporta PDF y texto/Markdown. Úsala cuando necesites el detalle íntegro de
    un documento concreto. Si la respuesta acaba en "[...continúa ...
    offset=N ...]", vuelve a llamarla con ese offset para leer el resto.

    Args:
        path: Ruta del documento relativa a knowledge_base/.
        offset: Carácter desde el que seguir leyendo (0 = inicio).
    """
    return tools.read_file(path, offset=offset)


@beta_tool
def ingest_corpus() -> str:
    """Indexa todos los documentos de knowledge_base/ en la base vectorial.

    Ejecútala una vez antes de usar search_corpus. Es idempotente: solo
    procesa documentos nuevos o modificados y quita los que ya no existen.
    """
    return _get_rag().ingest_directory()


@beta_tool
def search_corpus(question: str) -> str:
    """Busca semánticamente en el corpus indexado y devuelve los fragmentos
    más relevantes con su fuente.

    Úsala para responder preguntas que cruzan varios documentos.

    Args:
        question: Pregunta o tema a buscar en la literatura indexada.
    """
    return _get_rag().query(question)


@beta_tool
def write_summary(
    filename: str,
    title: str,
    abstract: str,
    full_explanation: str,
    key_points: list[str] | None = None,
    source: str | None = None,
    overwrite: bool = False,
) -> str:
    """Escribe el reporte final en Markdown dentro de docs_output/.

    El reporte SIEMPRE debe llevar, en este orden, un abstract conciso y una
    explicación completa del paper (problema, metodología, resultados,
    limitaciones y relevancia clínica).

    Args:
        filename: Nombre del fichero de salida, p. ej. "resumen_dicom.md".
        title: Título del reporte (normalmente el título del paper).
        abstract: Resumen conciso del paper, de 3 a 6 frases.
        full_explanation: Explicación completa y detallada del paper. Debe ser
            sustancialmente más extensa que el abstract.
        key_points: Lista opcional de puntos clave o hallazgos.
        source: Referencia opcional al documento fuente.
        overwrite: True para reemplazar un reporte existente con ese nombre
            (p. ej. al mejorar un reporte que ya escribiste).
    """
    return tools.write_summary(
        filename=filename,
        title=title,
        abstract=abstract,
        full_explanation=full_explanation,
        key_points=key_points,
        source=source,
        overwrite=overwrite,
    )


@beta_tool
def search_references(query: str, limit: int = 5) -> str:
    """Busca papers en fuentes académicas externas (Semantic Scholar / arXiv) y
    devuelve candidatos con su URL de PDF descargable.

    Úsala para descubrir literatura que aún no está en knowledge_base/.

    Args:
        query: Términos de búsqueda (tema, título, autores).
        limit: Número máximo de resultados (por defecto 5).
    """
    return references.search_references(query, limit)


@beta_tool
def download_reference(url: str, filename: str) -> str:
    """Descarga un PDF (de search_references) a knowledge_base/ y lo indexa
    automáticamente en la base vectorial. No necesitas llamar a ingest_corpus
    después: ya queda consultable con search_corpus.

    Args:
        url: URL http(s) directa al PDF.
        filename: Nombre con el que guardarlo, p. ej. "kerbl2023.pdf".
    """
    resultado = references.download_reference(url, filename)
    if resultado.startswith("ERROR"):
        return resultado
    indexado = _get_rag().ingest_file(references.normalized_pdf_name(filename))
    return f"{resultado}\n{indexado}"


# Con JEICOB_ENABLE_NETWORK=0 el modelo no ve las tools con salida a internet.
_NETWORK_TOOLS = (
    [search_references, download_reference] if config.NETWORK_ENABLED else []
)

AGENT_TOOLS = [
    read_directory,
    read_file,
    ingest_corpus,
    search_corpus,
    *_NETWORK_TOOLS,
    write_summary,
]

SYSTEM_PROMPT = system_prompt(network=config.NETWORK_ENABLED)


# --- Bucle del agente ---------------------------------------------------------


def _run_turn(client: Anthropic, messages: list) -> None:
    """Ejecuta un turno completo (con posibles llamadas a tools) e imprime la
    respuesta del agente. Mantiene `messages` sincronizado con el historial."""
    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        thinking={"type": "adaptive"},
        tools=AGENT_TOOLS,
        messages=messages,
    )

    for message in runner:
        for block in message.content:
            if block.type == "text" and block.text.strip():
                console.print(Markdown(block.text))
            elif block.type == "tool_use":
                console.print(f"[dim]· usando {block.name}…[/dim]")

        # Espejar el turno en el historial (incluye bloques thinking/tool_use).
        messages.append({"role": "assistant", "content": message.content})
        tool_response = runner.generate_tool_call_response()
        if tool_response is not None:
            messages.append(tool_response)


def main() -> None:
    # El .env lo carga jeicob.config al importarse (el del repo o ~/.jeicob),
    # igual que en jeicob-local y jeicob-mcp; un load_dotenv() sin ruta
    # buscaría hacia arriba y podría coger el .env de otro proyecto.

    if not os.getenv("ANTHROPIC_API_KEY"):
        console.print(
            "[bold red]Falta ANTHROPIC_API_KEY.[/] "
            "Copia .env.example a .env y añade tu clave, o expórtala en el entorno."
        )
        sys.exit(1)

    client = Anthropic()
    messages: list = []

    console.print(
        Panel.fit(
            "[bold]jeicob[/] — agente de investigación (3DGS · DICOM · normativas)\n"
            f"Modelo: [cyan]{MODEL}[/]  ·  "
            f"Red: {'sí' if config.NETWORK_ENABLED else '[yellow]desactivada[/]'}  ·  "
            "Escribe [bold]salir[/] para terminar.",
            border_style="green",
        )
    )

    try:
        while True:
            try:
                user_input = console.input("\n[bold green]tú[/] › ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not user_input:
                continue
            if user_input.lower() in {"salir", "exit", "quit"}:
                break

            messages.append({"role": "user", "content": user_input})
            console.print("\n[bold cyan]jeicob[/]:")
            try:
                _run_turn(client, messages)
            except Exception as exc:  # noqa: BLE001
                console.print(f"[bold red]Error durante el turno:[/] {exc}")
    finally:
        if _rag is not None:
            _rag.close()
        console.print("\n[dim]Hasta luego.[/]")


if __name__ == "__main__":
    main()
