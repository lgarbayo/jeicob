"""Configuración de rutas de jeicob, leída del entorno.

Centraliza dónde vive cada carpeta de datos para que el agente funcione igual
lo lance quien lo lance (CLI, servidor MCP arrancado por Claude Desktop con
`cwd=/`, CI...) y para poder integrarlo más adelante en un monorepo.

Variables de entorno (todas opcionales):

| Variable               | Por defecto              |
|------------------------|--------------------------|
| `JEICOB_HOME`          | raíz del repositorio     |
| `JEICOB_KB_DIR`        | `<home>/knowledge_base`  |
| `JEICOB_OUTPUT_DIR`    | `<home>/docs_output`     |
| `QDRANT_PATH`          | `<home>/.qdrant_data`    |
| `FASTEMBED_CACHE_PATH` | `<home>/.fastembed_cache`|
| `JEICOB_ENABLE_NETWORK`| `1`                      |

Las rutas relativas se resuelven contra `JEICOB_HOME`, **nunca** contra el
directorio de trabajo: así `QDRANT_PATH=./.qdrant_data` apunta al mismo sitio
aunque el proceso arranque en `/`.

`JEICOB_ENABLE_NETWORK=0` quita `search_references` y `download_reference` a
los tres orquestadores (CLI con Claude, CLI con Ollama y servidor MCP) y hace
que `netguard` rechace cualquier petición saliente.

Este módulo no importa nada pesado y no escribe en stdout (requisito del
transporte stdio de MCP).
"""

from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Carga el `.env` del repo ANTES de leer las rutas: si no, un QDRANT_PATH o
# JEICOB_KB_DIR definidos en `.env` se ignorarían (los orquestadores llaman a
# load_dotenv() después de importar este módulo). No pisa el entorno real.
try:
    from dotenv import load_dotenv

    load_dotenv(_REPO_ROOT / ".env", override=False)
except ImportError:  # pragma: no cover — python-dotenv es dependencia base
    pass


def env_flag(name: str, default: bool) -> bool:
    """Lee un flag booleano del entorno ("0", "false", "no", "off" o vacío
    son falsos; cualquier otro valor, verdadero)."""
    valor = os.getenv(name)
    if valor is None:
        return default
    return valor.strip().lower() not in {"0", "false", "no", "off", ""}


def _resolve(value: str | None, default: Path, base: Path) -> Path:
    """Convierte una ruta de entorno en absoluta (relativa a `base`)."""
    if not value:
        return default.resolve()
    ruta = Path(value).expanduser()
    if not ruta.is_absolute():
        ruta = base / ruta
    return ruta.resolve()


PROJECT_ROOT: Path = _resolve(os.getenv("JEICOB_HOME"), _REPO_ROOT, _REPO_ROOT)
KNOWLEDGE_BASE_DIR: Path = _resolve(
    os.getenv("JEICOB_KB_DIR"), PROJECT_ROOT / "knowledge_base", PROJECT_ROOT
)
DOCS_OUTPUT_DIR: Path = _resolve(
    os.getenv("JEICOB_OUTPUT_DIR"), PROJECT_ROOT / "docs_output", PROJECT_ROOT
)
QDRANT_PATH: Path = _resolve(
    os.getenv("QDRANT_PATH"), PROJECT_ROOT / ".qdrant_data", PROJECT_ROOT
)
FASTEMBED_CACHE_PATH: Path = _resolve(
    os.getenv("FASTEMBED_CACHE_PATH"), PROJECT_ROOT / ".fastembed_cache", PROJECT_ROOT
)

# Tools con salida a internet (search_references, download_reference).
NETWORK_ENABLED: bool = env_flag("JEICOB_ENABLE_NETWORK", True)

# fastembed lee esta variable al cargar el modelo. Por defecto usaría
# `/tmp/fastembed_cache`, que se borra al reiniciar (otra descarga de ~220 MB)
# y en el que puede escribir cualquier usuario de la máquina.
os.environ["FASTEMBED_CACHE_PATH"] = str(FASTEMBED_CACHE_PATH)
