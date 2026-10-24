import html
import ipaddress
import re
import socket
import ssl
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import certifi
import httpcore

MAX_BYTES = 200 * 1024
TIMEOUT = 10.0
MAX_REDIRECTS = 3
TEXT_TYPES = ("text/html", "text/plain")
SCRIPTS = re.compile(r"<(script|style|noscript)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
TAGS = re.compile(r"<[^>]+>")
TITLE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)
SPACES = re.compile(r"\s+")

Resolver = Callable[[str, int], list[str]]


class FetchError(Exception):
    pass


@dataclass(frozen=True)
class Page:
    url: str
    title: str
    text: str


def system_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise FetchError("Имя сервера не разрешается.") from error
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def check_address(value: str) -> None:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError as error:
        raise FetchError("Недопустимый адрес сервера.") from error
    if isinstance(address, ipaddress.IPv6Address) and (address.ipv4_mapped or address.sixtofour):
        raise FetchError("Адрес IPv4-mapped запрещён.")
    if (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise FetchError("Адрес сервера не является публичным.")


class PinnedBackend(httpcore.NetworkBackend):
    def __init__(self, resolver: Resolver, connector: httpcore.NetworkBackend | None = None) -> None:
        self.resolver = resolver
        self.connector = connector or httpcore.SyncBackend()
        self.connections: list[tuple[str, str]] = []

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable | None = None,
    ) -> httpcore.NetworkStream:
        addresses = self.resolver(host, port)
        if not addresses:
            raise FetchError("Имя сервера не разрешается.")
        for address in addresses:
            check_address(address)
        self.connections.append((host, addresses[0]))
        return self.connector.connect_tcp(
            addresses[0], port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options: Iterable | None = None):
        raise FetchError("Unix socket запрещён.")


def check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise FetchError("Разрешены только адреса https.")
    if parts.username or parts.password:
        raise FetchError("Адрес с учётными данными запрещён.")


def page_text(raw: bytes, content_type: str) -> tuple[str, str]:
    charset = "utf-8"
    match = re.search(r"charset=([\w.-]+)", content_type, re.IGNORECASE)
    if match:
        charset = match.group(1)
    try:
        text = raw.decode(charset, "replace")
    except LookupError:
        text = raw.decode("utf-8", "replace")
    if not content_type.startswith("text/html"):
        return "", SPACES.sub(" ", text).strip()
    found = TITLE.search(text)
    title = html.unescape(TAGS.sub("", found.group(1))).strip() if found else ""
    body = html.unescape(TAGS.sub(" ", SCRIPTS.sub(" ", text)))
    return SPACES.sub(" ", title)[:200], SPACES.sub(" ", body).strip()


def fetch_public(
    url: str, *, resolver: Resolver = system_resolver, connector: httpcore.NetworkBackend | None = None
) -> Page:
    current = url
    deadline = time.monotonic() + TIMEOUT
    timeouts = {"connect": TIMEOUT, "read": TIMEOUT, "write": TIMEOUT, "pool": TIMEOUT}
    for _ in range(MAX_REDIRECTS + 1):
        check_url(current)
        backend = PinnedBackend(resolver, connector)
        context = ssl.create_default_context(cafile=certifi.where())
        try:
            with (
                httpcore.ConnectionPool(ssl_context=context, network_backend=backend, retries=0) as pool,
                pool.stream(
                    "GET",
                    current,
                    headers=[(b"Accept", b"text/html, text/plain"), (b"User-Agent", b"back-office-research")],
                    extensions={"timeout": timeouts},
                ) as response,
            ):
                headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in response.headers}
                if 300 <= response.status < 400 and "location" in headers:
                    current = urljoin(current, headers["location"])
                    continue
                if response.status != 200:
                    raise FetchError(f"Сервер ответил кодом {response.status}.")
                content_type = headers.get("content-type", "").lower()
                if not content_type.startswith(TEXT_TYPES):
                    raise FetchError("Разрешены только ответы text/html и text/plain.")
                body = bytearray()
                for chunk in response.iter_stream():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise FetchError("Ответ больше 200 КБ.")
                    if time.monotonic() > deadline:
                        raise FetchError("Превышено время загрузки.")
        except httpcore.TimeoutException as error:
            raise FetchError("Превышено время загрузки.") from error
        except httpcore.ConnectError as error:
            raise FetchError("Не удалось подключиться к серверу.") from error
        except (httpcore.NetworkError, httpcore.ProtocolError) as error:
            raise FetchError("Ошибка соединения с сервером.") from error
        title, text = page_text(bytes(body), content_type)
        return Page(url=current, title=title or current, text=text)
    raise FetchError("Больше трёх перенаправлений.")
