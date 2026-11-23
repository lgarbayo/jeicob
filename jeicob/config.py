"""Configuración de rutas de jeicob, leída del entorno.

Centraliza dónde vive cada carpeta de datos para que el agente funcione igual
lo lance quien lo lance (CLI, servidor MCP arrancado por Claude Desktop con
`cwd=/`, CI...) y para poder integrarlo más adelante en un monorepo.

Variables de entorno (todas opcionales):

| Variable               | Por defecto              |
|------------------------|--------------------------|
| `JEICOB_HOME`          | raíz del repo (o ~/.jeicob) |
| `JEICOB_KB_DIR`        | `<home>/knowledge_base`  |
| `JEICOB_OUTPUT_DIR`    | `<home>/docs_output`     |
| `QDRANT_PATH`          | `<home>/.qdrant_data`    |
| `FASTEMBED_CACHE_PATH` | `<home>/.fastembed_cache`|
| `JEICOB_ENABLE_NETWORK`| `1`                      |
| `QDRANT_URL`           | — (Qdrant local en disco)|
| `QDRANT_API_KEY`       | —                        |

Las rutas relativas se resuelven contra `JEICOB_HOME`, **nunca** contra el
directorio de trabajo: así `QDRANT_PATH=./.qdrant_data` apunta al mismo sitio
aunque el proceso arranque en `/`.

Con `QDRANT_URL` (p. ej. `http://localhost:6333`, ver `docker-compose.yml`) la
base vectorial es un Qdrant en servidor en vez del directorio `QDRANT_PATH`:
varios procesos (CLI, sesiones MCP) pueden usarla a la vez.

`JEICOB_ENABLE_NETWORK=0` quita `search_references` y `download_reference` a
los tres orquestadores (CLI con Claude, CLI con Ollama y servidor MCP) y hace
que `netguard` rechace cualquier petición saliente.

Este módulo no importa nada pesado y no escribe en stdout (requisito del
transporte stdio de MCP).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

_log = logging.getLogger("jeicob.config")

_PKG_DIR = Path(__file__).resolve().parent
# Desde un checkout (o una instalación editable) la raíz es el repo; instalado
# como paquete normal en site-packages no hay repo, así que los datos van a
# ~/.jeicob (configurable con JEICOB_HOME).
_REPO_ROOT = (
    _PKG_DIR.parent
    if (_PKG_DIR.parent / "pyproject.toml").is_file()
    else Path.home() / ".jeicob"
)

# Carga el `.env` del repo ANTES de leer las rutas: si no, un QDRANT_PATH o
# JEICOB_KB_DIR definidos en `.env` se ignorarían (los orquestadores llaman a
# load_dotenv() después de importar este módulo). No pisa el entorno real.
try:
    from dotenv import load_dotenv

    load_dotenv(_REPO_ROOT / ".env", override=False)
except ImportError:  # pragma: no cover — python-dotenv es dependencia base
    pass


_TRUE = {"1", "true", "yes", "si", "sí", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def env_flag(name: str, default: bool, on_unknown: bool | None = None) -> bool:
    """Lee un flag booleano del entorno.

    Verdaderos: 1, true, yes, si, sí, on. Falsos: 0, false, no, off o vacío
    (sin distinguir mayúsculas). Cualquier otro valor (p. ej. "desactivado")
    se avisa por stderr y vale `on_unknown` (o `default` si no se indica):
    así un interruptor de seguridad puede fallar cerrado.
    """
    valor = os.getenv(name)
    if valor is None:
        return default
    normalizado = valor.strip().lower()
    if normalizado in _TRUE:
        return True
    if normalizado in _FALSE:
        return False
    resultado = default if on_unknown is None else on_unknown
    _log.warning(
        "%s=%r no es un valor reconocido (usa 1 o 0); se toma %s.",
        name,
        valor,
        "1" if resultado else "0",
    )
    return resultado


def env_float(name: str, default: float) -> float:
    """Lee un número del entorno; si no es válido, avisa y usa `default`."""
    valor = os.getenv(name)
    if valor is None or not valor.strip():
        return default
    try:
        return float(valor)
    except ValueError:
        _log.warning("%s=%r no es un número; se toma %s.", name, valor, default)
        return default


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

# Qdrant en servidor (opcional). Si se define, tiene prioridad sobre QDRANT_PATH.
QDRANT_URL: str | None = (os.getenv("QDRANT_URL") or "").strip() or None
QDRANT_API_KEY: str | None = (os.getenv("QDRANT_API_KEY") or "").strip() or None

# Tools con salida a internet (search_references, download_reference).
# Un valor no reconocido desactiva la red (falla cerrado): si alguien escribe
# "desactivado" o "nein", la intención era claramente apagarla.
NETWORK_ENABLED: bool = env_flag("JEICOB_ENABLE_NETWORK", True, on_unknown=False)

# fastembed lee esta variable al cargar el modelo. Por defecto usaría
# `/tmp/fastembed_cache`, que se borra al reiniciar (otra descarga de ~220 MB)
# y en el que puede escribir cualquier usuario de la máquina.
os.environ["FASTEMBED_CACHE_PATH"] = str(FASTEMBED_CACHE_PATH)
