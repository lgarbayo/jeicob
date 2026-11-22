"""Herramientas de sistema (tools) que el agente LLM puede invocar.

Cada función está documentada para que Claude decida cuándo usarla vía
*tool calling* nativo (Fase 4). Las funciones devuelven siempre `str`
(el formato que espera un `tool_result`) y nunca lanzan excepciones al
llamador: los errores se devuelven como texto para que el modelo pueda
reaccionar y reintentar.

Fronteras de seguridad:
  - Lectura confinada a `knowledge_base/` (documentos fuente).
  - Escritura confinada a `docs_output/` (reportes generados).
Cualquier intento de salir de esas carpetas (p. ej. `../../etc/passwd`)
se bloquea antes de tocar el disco.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pypdf

from . import config

# --- Rutas base del proyecto (configurables por entorno, ver config.py) -------

PROJECT_ROOT = config.PROJECT_ROOT
KNOWLEDGE_BASE_DIR = config.KNOWLEDGE_BASE_DIR
DOCS_OUTPUT_DIR = config.DOCS_OUTPUT_DIR

# Extensiones legibles como texto plano / Markdown.
_TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".rst"}

# Tope de caracteres por lectura, para no desbordar el contexto del LLM.
MAX_READ_CHARS = 100_000


# --- Utilidades internas de seguridad -----------------------------------------


def _resolve_within(base: Path, user_path: str) -> Path:
    """Resuelve `user_path` y garantiza que queda dentro de `base`.

    Neutraliza *path traversal* (`..`), rutas absolutas y symlinks que
    apunten fuera del directorio permitido. Lanza `ValueError` si escapa.
    """
    candidate = (base / user_path).resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError(f"Ruta '{user_path}' fuera del directorio permitido ({base}).")
    return candidate


def _is_within(base: Path, ruta: Path) -> bool:
    """True si `ruta` (resuelta, siguiendo symlinks) queda dentro de `base`."""
    real = ruta.resolve()
    return real == base or base in real.parents


def _display(ruta: Path) -> str:
    """Ruta legible: relativa al proyecto si cuelga de él, absoluta si no."""
    try:
        return str(ruta.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(ruta)


# --- Extracción de texto (compartida con rag.py) ------------------------------


def _extract(p: Path) -> str:
    """Extrae el texto de un PDF o fichero de texto (sin caché)."""
    sufijo = p.suffix.lower()
    if sufijo == ".pdf":
        lector = pypdf.PdfReader(str(p))
        return "\n".join(pagina.extract_text() or "" for pagina in lector.pages)
    if sufijo in _TEXT_SUFFIXES:
        return p.read_text(encoding="utf-8", errors="replace")
    raise ValueError(
        f"formato no soportado ('{sufijo}'). "
        "Formatos válidos: .pdf, .md, .markdown, .txt, .rst."
    )


@lru_cache(maxsize=8)
def _extract_cached(ruta: str, _mtime_ns: int, _size: int) -> str:
    """`_extract` cacheado. La clave incluye mtime y tamaño para que la caché
    se invalide sola si el fichero cambia."""
    return _extract(Path(ruta))


def extract_text(ruta: Path, use_cache: bool = True) -> str:
    """Texto completo (sin truncar) de un PDF o fichero de texto.

    Cachea las últimas extracciones: leer un PDF por páginas (`read_file` con
    `offset`) no vuelve a parsearlo entero en cada llamada. La ingesta usa
    `use_cache=False`, porque mtime+tamaño no garantizan que el contenido sea
    el mismo (p. ej. `cp -p` o sistemas de ficheros con mtime grueso).
    """
    if not use_cache:
        return _extract(ruta)
    st = ruta.stat()
    return _extract_cached(str(ruta), st.st_mtime_ns, st.st_size)


# --- Tools expuestas al LLM ---------------------------------------------------


def read_directory() -> str:
    """Lista los documentos de referencia disponibles en `knowledge_base/`.

    Úsala al principio para descubrir qué material hay antes de leer o
    resumir. Devuelve rutas relativas (para pasarlas a `read_file`) junto
    con el tamaño de cada fichero.

    Returns:
        Un listado en texto con una línea por documento, o un aviso si la
        carpeta está vacía.
    """
    if not KNOWLEDGE_BASE_DIR.exists():
        return f"La carpeta de conocimiento no existe: {KNOWLEDGE_BASE_DIR}"

    entradas: list[str] = []
    for ruta in sorted(KNOWLEDGE_BASE_DIR.rglob("*")):
        if ruta.is_dir() or ruta.name.startswith("."):
            continue
        if not _is_within(KNOWLEDGE_BASE_DIR, ruta):
            continue  # symlink que apunta fuera del sandbox: ni se lista
        rel = ruta.relative_to(KNOWLEDGE_BASE_DIR)
        kb = ruta.stat().st_size / 1024
        entradas.append(f"- {rel}  ({kb:.1f} KB)")

    if not entradas:
        return (
            "No hay documentos en knowledge_base/. "
            "Añade ficheros .pdf, .md o .txt para poder ingerirlos."
        )
    return "Documentos disponibles en knowledge_base/:\n" + "\n".join(entradas)


def read_file(path: str, offset: int = 0, max_chars: int = MAX_READ_CHARS) -> str:
    """Lee el contenido de un documento de `knowledge_base/`, por tramos.

    Soporta PDF (extracción de texto vía pypdf) y ficheros de texto plano
    o Markdown (.md, .markdown, .txt, .rst). La ruta debe ser relativa a
    `knowledge_base/` (tal como la devuelve `read_directory`).

    Los documentos largos se leen por tramos: si queda texto, la respuesta
    termina con una línea `[...continúa: ... offset=N ...]` que indica el
    `offset` de la siguiente llamada.

    Args:
        path: Ruta del documento relativa a `knowledge_base/`,
            por ejemplo `"gaussian_splatting.pdf"` o `"notas/dicom.md"`.
        offset: Carácter desde el que empezar a leer (0 = inicio).
        max_chars: Máximo de caracteres a devolver en esta llamada.

    Returns:
        El tramo de texto pedido, o un mensaje de error si no existe o no se
        puede leer.
    """
    if offset < 0:
        return "ERROR: offset no puede ser negativo."
    if max_chars < 1:
        return "ERROR: max_chars debe ser al menos 1."
    max_chars = min(max_chars, MAX_READ_CHARS)

    try:
        ruta = _resolve_within(KNOWLEDGE_BASE_DIR, path)
    except ValueError as exc:
        return f"ERROR: {exc}"

    if not ruta.exists() or not ruta.is_file():
        return f"ERROR: el documento '{path}' no existe en knowledge_base/."

    try:
        texto = extract_text(ruta)
    except ValueError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:  # noqa: BLE001 — devolvemos el error al LLM, no lo propagamos
        return f"ERROR al leer '{path}': {exc}"

    if not texto:
        return f"[El documento '{path}' no contiene texto extraíble.]"

    total = len(texto)
    if offset >= total:
        return f"ERROR: offset={offset} fuera de rango; el documento tiene {total} caracteres."

    fin = min(offset + max_chars, total)
    tramo = texto[offset:fin]
    if fin < total:
        tramo += (
            f"\n\n[...continúa: leídos {offset}-{fin} de {total} caracteres. "
            f"Para seguir, llama de nuevo con offset={fin}...]"
        )
    return tramo


def write_summary(
    filename: str,
    title: str,
    abstract: str,
    full_explanation: str,
    key_points: list[str] | None = None,
    source: str | None = None,
) -> str:
    """Escribe un reporte estructurado en Markdown dentro de `docs_output/`.

    Es la única forma que tiene el agente de persistir un resultado. Para
    garantizar la calidad, el reporte SIEMPRE se compone, en este orden, de:

      1. Un **abstract**: resumen conciso del paper (3-6 frases).
      2. Una **explicación completa**: desarrollo exhaustivo del paper —no un
         resumen superficial— cubriendo problema/contexto, metodología,
         resultados, limitaciones y relevancia clínica.

    La tool valida que ambas partes estén presentes y que la explicación sea
    más extensa que el abstract; si no, devuelve un error para que el agente
    regenere el contenido.

    Por seguridad, SOLO puede escribir en `docs_output/`: se ignora cualquier
    componente de directorio en `filename` (se usa el nombre base) y se fuerza
    la extensión `.md`.

    Args:
        filename: Nombre del fichero de salida, p. ej. `"resumen_dicom.md"`.
            Cualquier ruta se reduce a su nombre base.
        title: Título del reporte (habitualmente el título del paper).
        abstract: Resumen conciso del paper, de 3 a 6 frases.
        full_explanation: Explicación completa y detallada del paper. Debe
            cubrir problema/contexto, metodología, resultados, limitaciones y
            relevancia, y ser sustancialmente más extensa que el abstract.
        key_points: Lista opcional de puntos clave o hallazgos destacados.
        source: Referencia opcional al documento fuente (fichero de
            `knowledge_base/`, DOI, etc.).

    Returns:
        Confirmación con la ruta escrita, o un mensaje de error.
    """
    # --- Validación de estructura: abstract + explicación completa -----------
    if not (title and title.strip()):
        return "ERROR: falta el título del reporte."
    if not (abstract and abstract.strip()):
        return (
            "ERROR: falta el abstract. El reporte debe empezar con un resumen "
            "conciso (3-6 frases) del paper."
        )
    if not (full_explanation and full_explanation.strip()):
        return (
            "ERROR: falta la explicación completa. Tras el abstract, el reporte "
            "debe explicar el paper en detalle: problema, metodología, "
            "resultados, limitaciones y relevancia."
        )
    if len(full_explanation.strip()) <= len(abstract.strip()):
        return (
            "ERROR: la explicación completa es más corta o igual que el abstract. "
            "Debe ser un desarrollo exhaustivo del paper, no otro resumen breve."
        )

    # --- Sandbox del nombre de fichero ---------------------------------------
    nombre_base = Path(filename).name
    if not nombre_base or nombre_base in {".", ".."}:
        return f"ERROR: nombre de fichero inválido: '{filename}'."
    if not nombre_base.lower().endswith(".md"):
        nombre_base += ".md"

    try:
        destino = _resolve_within(DOCS_OUTPUT_DIR, nombre_base)
    except ValueError as exc:
        return f"ERROR: {exc}"

    # --- Ensamblado del reporte Markdown -------------------------------------
    partes: list[str] = [f"# {title.strip()}", ""]
    if source and source.strip():
        partes += [f"> **Fuente:** {source.strip()}", ""]
    partes += ["## Abstract", "", abstract.strip(), ""]
    partes += ["## Explicación completa", "", full_explanation.strip(), ""]
    if key_points:
        puntos = [f"- {p.strip()}" for p in key_points if p and p.strip()]
        if puntos:
            partes += ["## Puntos clave", "", *puntos, ""]
    contenido = "\n".join(partes)

    try:
        DOCS_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        destino.write_text(contenido, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return f"ERROR al escribir '{nombre_base}': {exc}"

    return (
        f"Reporte escrito correctamente en {_display(destino)} "
        f"({len(contenido)} caracteres)."
    )


# --- Prueba de humo rápida ----------------------------------------------------

if __name__ == "__main__":
    print(read_directory())

    # Reporte bien formado -> debe escribirse.
    print(
        write_summary(
            filename="_smoke_test.md",
            title="Paper de prueba",
            abstract="Resumen conciso del paper de prueba en un par de frases.",
            full_explanation=(
                "Explicación completa y extensa del paper: contexto y problema, "
                "metodología empleada, resultados obtenidos, limitaciones del "
                "estudio y relevancia clínica del enfoque descrito."
            ),
            key_points=["Hallazgo A", "Hallazgo B"],
            source="_smoke_test.pdf",
        )
    )

    # Estructura incompleta (sin explicación completa) -> debe rechazarse.
    print(
        write_summary(
            filename="_smoke_test_bad.md",
            title="Sin explicación",
            abstract="Solo abstract, sin desarrollo.",
            full_explanation="",
        )
    )

    # El sandboxing debe rechazar el intento de escape en lectura.
    print(read_file("../pyproject.toml"))
