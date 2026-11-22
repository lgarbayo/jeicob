"""Prueba de humo del servidor MCP con un cliente real por stdio.

Arranca `python -m src.mcp_server` como subproceso, con knowledge_base,
docs_output y Qdrant temporales (no toca tus datos), y comprueba:

  - que lista las tools esperadas y que stdout solo lleva JSON-RPC;
  - lectura, paginación y sandbox de read_file;
  - validación y escritura de write_summary;
  - que los errores llegan como `isError=true`.

Con `--full` además ejercita el RAG de verdad (necesita el modelo de
embeddings; `uv run python -m src.rag --prefetch` lo descarga): ingesta
idempotente, búsqueda, y el comportamiento cuando otro proceso tiene el lock
de Qdrant.

Ejecución:
    uv run --extra mcp python -m src.mcp_smoke [--full]
"""

import sys
import tempfile
from pathlib import Path

import anyio
from mcp import Client, StdioServerParameters

from . import config

EXPECTED_TOOLS = {
    "read_directory",
    "read_file",
    "write_summary",
    "ingest_corpus",
    "search_corpus",
    "search_references",
    "download_reference",
}


def _text(resultado) -> str:
    return "".join(getattr(c, "text", "") for c in resultado.content)


async def _run(full: bool) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        kb, out, qdrant = tmp_path / "kb", tmp_path / "out", tmp_path / "qdrant"
        kb.mkdir()
        (kb / "dicom.md").write_text(
            "El estándar DICOM define el formato de imágenes médicas. " * 40,
            encoding="utf-8",
        )

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "src.mcp_server"],
            cwd=str(Path(__file__).resolve().parent.parent),
            env={
                "JEICOB_KB_DIR": str(kb),
                "JEICOB_OUTPUT_DIR": str(out),
                "QDRANT_PATH": str(qdrant),
                "FASTEMBED_CACHE_PATH": str(config.FASTEMBED_CACHE_PATH),
                "JEICOB_MCP_PREFETCH": "0",
                "JEICOB_QDRANT_IDLE_RELEASE": "1",
                "JEICOB_QDRANT_LOCK_TIMEOUT": "1",
            },
        )

        async with Client(params, read_timeout_seconds=300) as c:
            nombres = {t.name for t in (await c.list_tools()).tools}
            assert nombres == EXPECTED_TOOLS, nombres
            print("tools:", ", ".join(sorted(nombres)))

            r = await c.call_tool("read_directory", {})
            assert not r.is_error and "dicom.md" in _text(r), _text(r)

            r = await c.call_tool("read_file", {"path": "dicom.md", "max_chars": 100})
            assert not r.is_error and "offset=100" in _text(r), _text(r)

            r = await c.call_tool("read_file", {"path": "../pyproject.toml"})
            assert r.is_error and "fuera del directorio" in _text(r), _text(r)
            print("read_file: paginación y sandbox OK")

            r = await c.call_tool(
                "write_summary",
                {
                    "filename": "x.md",
                    "title": "T",
                    "abstract": "A",
                    "full_explanation": "",
                },
            )
            assert r.is_error, _text(r)
            r = await c.call_tool(
                "write_summary",
                {
                    "filename": "../escape.md",
                    "title": "DICOM",
                    "abstract": "Resumen breve.",
                    "full_explanation": "Explicación bastante más larga que el abstract.",
                },
            )
            assert not r.is_error and (out / "escape.md").exists(), _text(r)
            print("write_summary: validación y sandbox OK")

            if full:
                avances: list[tuple] = []

                async def on_progress(progress, total, message):
                    avances.append((progress, total, message))

                r = await c.call_tool(
                    "ingest_corpus", {}, progress_callback=on_progress
                )
                assert not r.is_error and "Ingerido 'dicom.md'" in _text(r), _text(r)
                assert avances and "dicom.md" in (avances[0][2] or ""), avances
                r = await c.call_tool("ingest_corpus", {})
                assert "Sin cambios 'dicom.md'" in _text(r), _text(r)
                r = await c.call_tool("search_corpus", {"question": "¿qué es DICOM?"})
                assert not r.is_error and "fuente: dicom.md" in _text(r), _text(r)
                print("RAG: ingesta idempotente y búsqueda OK")

                # Tras JEICOB_QDRANT_IDLE_RELEASE el servidor suelta el lock...
                await anyio.sleep(2.5)
                from qdrant_client import QdrantClient

                otro = QdrantClient(path=str(qdrant))  # ...y otro proceso lo coge
                try:
                    r = await c.call_tool("search_corpus", {"question": "DICOM"})
                    assert r.is_error and "en uso por otro proceso" in _text(r), _text(
                        r
                    )
                finally:
                    otro.close()
                r = await c.call_tool("search_corpus", {"question": "DICOM"})
                assert not r.is_error, _text(r)
                print("RAG: liberación del lock y error de lock ocupado OK")

    print("OK: servidor MCP verificado por stdio.")


if __name__ == "__main__":
    anyio.run(_run, "--full" in sys.argv)
