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
    handshake TLS, así que el DNS rebinding no sirve para colarse. Una IPv6
    que lleve dentro una IPv4 (NAT64, 6to4, Teredo…) se valida también por
    esa IPv4.
  - Sin proxies del entorno (el peer sería el proxy, no el destino).
  - Lista opcional de hosts permitidos.

Variables de entorno:

| Variable                         | Efecto                                            |
|----------------------------------|---------------------------------------------------|
| `JEICOB_DOWNLOAD_ALLOWED_HOSTS`  | Lista separada por comas; si se define, solo esos |
|                                  | dominios (y sus subdominios) son accesibles       |
| `JEICOB_ALLOW_PRIVATE_NETWORK`   | `1` desactiva el bloqueo de IPs no públicas       |
|                                  | (repositorios institucionales en la intranet)     |
| `JEICOB_ENABLE_NETWORK`          | `0` rechaza toda petición saliente (ver config)   |
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

from . import config

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
    if not config.NETWORK_ENABLED:
        # Segunda barrera: los orquestadores ya no registran las tools de red,
        # pero nada de jeicob que pase por aquí debe salir a internet. (La
        # descarga del modelo de embeddings la hace fastembed por su cuenta:
        # en equipos aislados, precárgalo con `python -m src.rag --prefetch`.)
        raise BlockedDestinationError(
            "la salida a internet está desactivada (JEICOB_ENABLE_NETWORK=0)."
        )
    partes = urllib.parse.urlsplit(url)
    if partes.scheme.lower() not in _SCHEMES:
        raise BlockedDestinationError(
            f"esquema no permitido '{partes.scheme}': solo http o https."
        )
    host = (partes.hostname or "").lower()
    if not host:
        raise BlockedDestinationError("la URL no tiene host.")
    try:
        puerto = partes.port
    except ValueError as exc:  # p. ej. ":99999" o ":abc"
        raise BlockedDestinationError(f"puerto no válido en la URL: {exc}") from exc
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
        infos = socket.getaddrinfo(host, puerto or None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise BlockedDestinationError(f"no se pudo resolver '{host}': {exc}") from exc
    for *_resto, sockaddr in infos:
        _check_ip(sockaddr[0])


# Prefijos IPv6 que transportan una IPv4 dentro (traducción o túnel): la IPv6
# puede parecer pública mientras apunta, en realidad, a una IPv4 interna.
_NAT64 = (
    ipaddress.IPv6Network("64:ff9b::/96"),  # NAT64 bien conocido (RFC 6052)
    ipaddress.IPv6Network("64:ff9b:1::/48"),  # NAT64 local (RFC 8215)
)
_IPV4_COMPAT = ipaddress.IPv6Network("::/96")  # IPv4-compatible (obsoleto)


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """IPv4 incrustadas en una IPv6 (mapped, NAT64, compat, 6to4, Teredo)."""
    incrustadas: list[ipaddress.IPv4Address] = []
    if ip.ipv4_mapped is not None:
        incrustadas.append(ip.ipv4_mapped)
    if any(ip in red for red in _NAT64) or ip in _IPV4_COMPAT:
        incrustadas.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    if ip.sixtofour is not None:
        incrustadas.append(ip.sixtofour)
    if ip.teredo is not None:
        incrustadas.extend(ip.teredo)  # (servidor, cliente)
    return incrustadas


def _check_ip(direccion: str) -> None:
    ip = ipaddress.ip_address(direccion.split("%", 1)[0])
    candidatas: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        candidatas += _embedded_ipv4(ip)
    for candidata in candidatas:
        if not candidata.is_global:
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
