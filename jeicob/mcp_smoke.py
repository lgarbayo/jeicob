"""Prueba de humo del servidor MCP con un cliente real por stdio.

Arranca `python -m jeicob.mcp_server` como subproceso, con knowledge_base,
docs_output y Qdrant temporales (no toca tus datos), y comprueba:

  - que lista las tools esperadas y que stdout solo lleva JSON-RPC (cualquier
    línea que no lo sea llega al `message_handler` como excepción);
  - lectura, paginación y sandbox de read_file;
  - validación y escritura de write_summary;
  - que los errores llegan como `isError=true`;
  - que con `JEICOB_ENABLE_NETWORK=0` desaparecen las tools de red;
  - el transporte streamable-http: token obligatorio (401), protección DNS
    rebinding (421 Host, 403 Origin) y tools accesibles con el token correcto.

Con `--full` además ejercita el RAG de verdad (necesita el modelo de
embeddings; `uv run python -m jeicob.rag --prefetch` lo descarga): ingesta
idempotente, búsqueda, y el comportamiento cuando otro proceso tiene el lock
de Qdrant.

Ejecución:
    uv run --extra mcp python -m jeicob.mcp_smoke [--full]
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import anyio
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, ResourceTemplateReference

from . import config

NETWORK_TOOLS = {"search_references", "download_reference"}
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
        # Contenido legítimo que empieza por "ERROR": no es un fallo.
        (kb / "errores.md").write_text(
            "ERROR: así titula el paper su sección sobre errores de medida.",
            encoding="utf-8",
        )
        # Un "%41" literal en el nombre (no debe decodificarse dos veces).
        (kb / "lit%41.md").write_text("Nombre con porcentaje.", encoding="utf-8")
        # Formato no legible: no debe anunciarse como resource.
        (kb / "imagen.png").write_bytes(b"\x89PNG")
        out.mkdir()
        (out / "MAYUS.MD").write_text("# Reporte en mayúsculas\n", encoding="utf-8")

        ajenos: list[Exception] = []

        async def on_message(mensaje) -> None:
            if isinstance(mensaje, Exception):
                ajenos.append(mensaje)

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "jeicob.mcp_server"],
            cwd=str(Path(__file__).resolve().parent.parent),
            env={
                "JEICOB_KB_DIR": str(kb),
                "JEICOB_OUTPUT_DIR": str(out),
                "QDRANT_PATH": str(qdrant),
                "FASTEMBED_CACHE_PATH": str(config.FASTEMBED_CACHE_PATH),
                "JEICOB_MCP_PREFETCH": "0",
                "JEICOB_QDRANT_IDLE_RELEASE": "1",
                "JEICOB_QDRANT_LOCK_TIMEOUT": "1",
                # Explícitos: si no, heredaría del .env un JEICOB_ENABLE_NETWORK=0
                # o, peor, un QDRANT_URL, y la prueba escribiría (y podaría) en
                # la colección real del Qdrant en servidor. Vacías = sin definir.
                "JEICOB_ENABLE_NETWORK": "1",
                "QDRANT_URL": "",
                "QDRANT_API_KEY": "",
                "JEICOB_MCP_TRANSPORT": "",
            },
        )

        async with Client(
            params, read_timeout_seconds=300, message_handler=on_message
        ) as c:
            nombres = {t.name for t in (await c.list_tools()).tools}
            assert nombres == EXPECTED_TOOLS, nombres
            print("tools:", ", ".join(sorted(nombres)))

            r = await c.call_tool("read_directory", {})
            assert not r.is_error and "dicom.md" in _text(r), _text(r)

            r = await c.call_tool("read_file", {"path": "dicom.md", "max_chars": 100})
            assert not r.is_error and "offset=100" in _text(r), _text(r)

            r = await c.call_tool("read_file", {"path": "errores.md"})
            assert not r.is_error and _text(r).startswith("ERROR: así"), _text(r)
            r = await c.call_tool("read_file", {"path": "dicom.md", "max_chars": 10**6})
            assert not r.is_error and len(_text(r)) <= 20_000 + 200, len(_text(r))

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
            r = await c.call_tool(
                "write_summary",
                {
                    "filename": "escape.md",
                    "title": "DICOM",
                    "abstract": "Resumen breve.",
                    "full_explanation": "Otra explicación más larga que el abstract.",
                },
            )
            assert r.is_error and "overwrite" in _text(r), _text(r)
            r = await c.call_tool(
                "write_summary",
                {
                    "filename": "escape.md",
                    "title": "DICOM",
                    "abstract": "Resumen breve.",
                    "full_explanation": "Otra explicación más larga que el abstract.",
                    "overwrite": True,
                },
            )
            assert not r.is_error and "Otra" in (out / "escape.md").read_text(), _text(
                r
            )
            print("write_summary: validación, sandbox y no sobrescritura OK")

            # SSRF: ninguno de estos destinos llega a abrir conexión.
            for url in (
                "http://169.254.169.254/latest/meta-data/",
                "http://127.0.0.1:6333/collections",
                "http://[::1]/a.pdf",
                "http://[64:ff9b::a9fe:a9fe]/a.pdf",  # NAT64 -> 169.254.169.254
                "http://[2002:7f00:1::]/a.pdf",  # 6to4 -> 127.0.0.1
                "http://[::7f00:1]/a.pdf",  # IPv4-compatible -> 127.0.0.1
                "http://example.org:99999/a.pdf",  # puerto inválido
                "file:///etc/passwd",
            ):
                r = await c.call_tool(
                    "download_reference", {"url": url, "filename": "x.pdf"}
                )
                assert r.is_error and (
                    "no es una dirección pública" in _text(r)
                    or "esquema no permitido" in _text(r)
                    or "puerto no válido" in _text(r)
                ), (url, _text(r))
            assert not (kb / "x.pdf").exists()
            print("download_reference: protección SSRF OK")

            uris = {str(r.uri) for r in (await c.list_resources()).resources}
            assert uris == {
                "jeicob://knowledge/dicom.md",
                "jeicob://knowledge/errores.md",
                "jeicob://knowledge/lit%2541.md",
                "jeicob://reports/escape.md",
                "jeicob://reports/MAYUS.MD",
            }, uris
            rr = await c.read_resource("jeicob://knowledge/dicom.md")
            assert "DICOM" in rr.contents[0].text
            rr = await c.read_resource("jeicob://knowledge/lit%2541.md")
            assert "porcentaje" in rr.contents[0].text
            rr = await c.read_resource("jeicob://reports/MAYUS.MD")
            assert "mayúsculas" in rr.contents[0].text
            comp = await c.complete(
                ResourceTemplateReference(uri="jeicob://knowledge/{+path}"),
                {"name": "path", "value": "lit"},
            )
            assert comp.completion.values == ["lit%2541.md"], comp.completion
            try:
                await c.read_resource("jeicob://reports/..%2F..%2Fpyproject.toml")
                raise AssertionError("un resource ha escapado de docs_output/")
            except MCPError:
                pass
            nombres_p = {p.name for p in (await c.list_prompts()).prompts}
            assert nombres_p == {"research_report", "summarize_document", "ask_corpus"}
            g = await c.get_prompt("summarize_document", {"path": "dicom.md"})
            assert "dicom.md" in g.messages[0].content.text
            for nombre, args in (("summarize_document", {}), ("no_existe", {})):
                try:
                    await c.get_prompt(nombre, args)
                    raise AssertionError(f"get_prompt({nombre}) debería fallar")
                except MCPError as exc:
                    assert exc.error.code == INVALID_PARAMS, exc.error
            print("resources y prompts OK")

            if full:
                avances: list[tuple] = []

                async def on_progress(progress, total, message):
                    avances.append((progress, total, message))

                r = await c.call_tool(
                    "ingest_corpus", {}, progress_callback=on_progress
                )
                assert not r.is_error and "Ingerido 'dicom.md'" in _text(r), _text(r)
                assert "Ingerido 'errores.md'" in _text(r), _text(r)
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

        assert not ajenos, f"stdout llevó algo que no es JSON-RPC: {ajenos}"
        print("stdout: solo JSON-RPC OK")

        # Sin red: ni las tools ni las instrucciones mencionan las de red.
        sin_red = StdioServerParameters(
            command=params.command,
            args=params.args,
            cwd=params.cwd,
            env={**params.env, "JEICOB_ENABLE_NETWORK": "0"},
        )
        async with Client(sin_red, read_timeout_seconds=60) as c:
            nombres = {t.name for t in (await c.list_tools()).tools}
            assert nombres == EXPECTED_TOOLS - NETWORK_TOOLS, nombres
            for red in NETWORK_TOOLS:
                assert red not in (c.instructions or ""), c.instructions
        print("JEICOB_ENABLE_NETWORK=0: sin tools de red OK")

        await _check_http(params)

    print("OK: servidor MCP verificado (stdio y HTTP).")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _post_status(url: str, headers: dict[str, str]) -> int:
    req = urllib.request.Request(
        url,
        data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **headers,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _start_http_server(params: StdioServerParameters, token: str, port: int):
    """Arranca el servidor por HTTP y espera a que acepte conexiones."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "jeicob.mcp_server", "--transport", "http"]
        + ["--port", str(port)],
        cwd=params.cwd,
        env={**os.environ, **params.env, "JEICOB_MCP_TOKEN": token},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(150):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return proc
        except OSError:
            if proc.poll() is not None:
                break
            time.sleep(0.2)
    proc.kill()
    raise AssertionError("el servidor HTTP no arrancó")


async def _check_http(params: StdioServerParameters) -> None:
    """Servidor por streamable-http: token, DNS rebinding y uso real."""
    token = "smoke-" + "x" * 40
    port = _free_port()
    proc = await anyio.to_thread.run_sync(_start_http_server, params, token, port)
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        bueno = {"Authorization": f"Bearer {token}"}
        estados = await anyio.to_thread.run_sync(
            lambda: (
                _post_status(url, {}),
                _post_status(url, {"Authorization": "Bearer " + "y" * 46}),
                _post_status(url, {**bueno, "Host": "evil.example"}),
                _post_status(url, {**bueno, "Origin": "http://evil.example"}),
                _post_status(url, {"Authorization": f"bearer {token}"}),
            )
        )
        # 401 sin token o con uno erróneo; 421 Host ajeno; 403 Origin ajeno; y
        # el esquema "bearer" no distingue mayúsculas (RFC 7235).
        assert estados[:4] == (401, 401, 421, 403), estados
        assert estados[4] not in (401, 403, 421), estados
        http = create_mcp_http_client(headers=bueno)
        async with Client(streamable_http_client(url, http_client=http)) as c:
            nombres = {t.name for t in (await c.list_tools()).tools}
            assert nombres == EXPECTED_TOOLS, nombres
            r = await c.call_tool("read_file", {"path": "dicom.md", "max_chars": 50})
            assert not r.is_error and "DICOM" in _text(r), _text(r)
        print("HTTP: token obligatorio, DNS rebinding y tools OK")
    finally:
        proc.terminate()
        await anyio.to_thread.run_sync(lambda: proc.wait(timeout=15))


if __name__ == "__main__":
    anyio.run(_run, "--full" in sys.argv)
