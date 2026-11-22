"""Acceso HTTP saliente con protección SSRF.

`download_reference` acepta una URL que decide el modelo, y el modelo puede
haber leído antes contenido no confiable (un PDF descargado, un abstract): una
inyección de prompt podría intentar que el agente pida
`http://169.254.169.254/...` (metadatos cloud), `http://localhost:6333`
(Qdrant en servidor) o un host de la intranet del hospital. Este módulo
abre URLs con estas garantías:

  - Solo `http`/`https` (nada de `file://`, `ftp://`, `data:`…), también en
    cada redirección, con un máximo de `MAX_REDIRECTS` saltos.
  - La IP a la que **realmente** se conecta el socket debe ser pública
    (`ipaddress.is_global`). Se comprueba tras el `connect()` y antes del
    handshake TLS, así que el DNS rebinding no sirve para colarse.
  - Sin proxies del entorno (el peer sería el proxy, no el destino).
  - Lista opcional de hosts permitidos.

Variables de entorno:

| Variable                         | Efecto                                            |
|----------------------------------|---------------------------------------------------|
| `JEICOB_DOWNLOAD_ALLOWED_HOSTS`  | Lista separada por comas; si se define, solo esos |
|                                  | dominios (y sus subdominios) son accesibles       |
| `JEICOB_ALLOW_PRIVATE_NETWORK`   | `1` desactiva el bloqueo de IPs no públicas       |
|                                  | (repositorios institucionales en la intranet)     |
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

MAX_REDIRECTS = 5
_SCHEMES = {"http", "https"}


class BlockedDestinationError(OSError):
    """La URL o la IP de destino no están permitidas."""


def _allowed_hosts() -> list[str]:
    valor = os.getenv("JEICOB_DOWNLOAD_ALLOWED_HOSTS", "")
    return [h.strip().lower().lstrip(".") for h in valor.split(",") if h.strip()]


def _private_allowed() -> bool:
    return os.getenv("JEICOB_ALLOW_PRIVATE_NETWORK", "").strip() in {"1", "true"}


def check_url(url: str) -> None:
    """Valida esquema, host y lista de permitidos. Lanza
    `BlockedDestinationError` si la URL no está permitida."""
    partes = urllib.parse.urlsplit(url)
    if partes.scheme.lower() not in _SCHEMES:
        raise BlockedDestinationError(
            f"esquema no permitido '{partes.scheme}': solo http o https."
        )
    host = (partes.hostname or "").lower()
    if not host:
        raise BlockedDestinationError("la URL no tiene host.")
    permitidos = _allowed_hosts()
    if permitidos and not any(host == h or host.endswith("." + h) for h in permitidos):
        raise BlockedDestinationError(
            f"host '{host}' fuera de JEICOB_DOWNLOAD_ALLOWED_HOSTS."
        )
    if _private_allowed():
        return
    # Primera barrera: si el nombre resuelve a alguna IP no pública, ni se
    # intenta la conexión (evita escanear puertos internos por timing). La
    # segunda barrera, en el socket, cubre el DNS rebinding.
    try:
        infos = socket.getaddrinfo(host, partes.port or None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise BlockedDestinationError(f"no se pudo resolver '{host}': {exc}") from exc
    for *_resto, sockaddr in infos:
        _check_ip(sockaddr[0])


def _check_ip(direccion: str) -> None:
    ip = ipaddress.ip_address(direccion.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not ip.is_global:
        raise BlockedDestinationError(
            f"destino bloqueado: {ip} no es una dirección pública "
            "(protección SSRF; ver JEICOB_ALLOW_PRIVATE_NETWORK)."
        )


def _check_peer(sock: socket.socket) -> None:
    if not _private_allowed():
        _check_ip(sock.getpeername()[0])


def _guarded_create_connection(*args, **kwargs) -> socket.socket:
    sock = socket.create_connection(*args, **kwargs)
    try:
        _check_peer(sock)
    except BaseException:
        sock.close()
        raise
    return sock


class _GuardedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _guarded_create_connection


class _GuardedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # HTTPSConnection.connect() abre el TCP con _create_connection y
        # después envuelve en TLS: la IP se valida antes del handshake.
        self._create_connection = _guarded_create_connection


class _GuardedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_GuardedHTTPConnection, req)


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_GuardedHTTPSConnection, req, context=self._context)


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    # OpenerDirector a mano (no build_opener): sin ProxyHandler, FileHandler,
    # FTPHandler ni DataHandler.
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.UnknownHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
        _GuardedHTTPHandler(),
        _GuardedHTTPSHandler(),
        _GuardedRedirectHandler(),
    ):
        opener.add_handler(handler)
    return opener


def open_url(url: str, headers: dict[str, str] | None = None, timeout: float = 45):
    """Abre `url` con las protecciones descritas arriba. Devuelve la respuesta
    (úsala como context manager). Lanza `BlockedDestinationError` o
    `urllib.error.URLError` si el destino no está permitido."""
    check_url(url)
    req = urllib.request.Request(url, headers=headers or {})
    return _opener().open(req, timeout=timeout)
