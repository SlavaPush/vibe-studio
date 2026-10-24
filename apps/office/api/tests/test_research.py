import httpcore
import pytest
from conftest import UI_TASK, wait_for

from app.research import MAX_BYTES, FetchError, fetch_public
from app.schemas import ModelProfile, TaskStatus
from app.tasks import IntakeOutcome

PUBLIC = "93.184.216.34"
OTHER = "151.101.1.69"


def http(status=200, body=b"<html><head><title>Docs</title></head><body><p>Hello</p></body></html>",
         content_type="text/html; charset=utf-8", headers=()):
    lines = [f"HTTP/1.1 {status} X", f"Content-Type: {content_type}", f"Content-Length: {len(body)}", *headers]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


class Stream(httpcore.NetworkStream):
    def __init__(self, data, log):
        self.data = data
        self.offset = 0
        self.log = log

    def read(self, max_bytes, timeout=None):
        chunk = self.data[self.offset:self.offset + max_bytes]
        self.offset += len(chunk)
        return chunk

    def write(self, buffer, timeout=None):
        self.log["sent"].append(bytes(buffer))

    def close(self):
        pass

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.log["sni"].append(server_hostname)
        return self

    def get_extra_info(self, info):
        return None


class Connector(httpcore.NetworkBackend):
    def __init__(self, responses):
        self.responses = responses
        self.log = {"sent": [], "sni": [], "connected": []}

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.log["connected"].append((host, port))
        return Stream(self.responses[host], self.log)


class Resolver:
    def __init__(self, table):
        self.table = table
        self.calls = []

    def __call__(self, host, port):
        self.calls.append(host)
        value = self.table[host]
        return value.pop(0) if isinstance(value, list) and value and isinstance(value[0], list) else value


def test_public_page_is_fetched_with_pinned_address_and_sni():
    connector = Connector({PUBLIC: http()})
    resolver = Resolver({"docs.example": [PUBLIC]})
    page = fetch_public("https://docs.example/guide", resolver=resolver, connector=connector)
    assert page.title == "Docs" and page.text == "Docs Hello" and page.url == "https://docs.example/guide"
    assert connector.log["connected"] == [(PUBLIC, 443)]
    assert connector.log["sni"] == ["docs.example"]
    assert b"Host: docs.example" in b"".join(connector.log["sent"])


@pytest.mark.parametrize("url", ["http://docs.example/", "ftp://docs.example/", "https://user:pw@docs.example/", "file:///etc/hosts"])
def test_only_plain_https_is_allowed(url):
    resolver = Resolver({})
    with pytest.raises(FetchError):
        fetch_public(url, resolver=resolver, connector=Connector({}))
    assert resolver.calls == []


@pytest.mark.parametrize(
    "addresses",
    [["127.0.0.1"], ["10.1.2.3"], ["192.168.0.10"], ["169.254.169.254"], ["::1"], ["fe80::1"],
     ["::ffff:93.184.216.34"], ["224.0.0.1"], ["0.0.0.0"], ["100.64.0.1"], [PUBLIC, "10.0.0.1"]],
)
def test_non_public_addresses_are_rejected(addresses):
    connector = Connector({})
    with pytest.raises(FetchError):
        fetch_public("https://docs.example/", resolver=Resolver({"docs.example": addresses}), connector=connector)
    assert connector.log["connected"] == []


def test_redirect_to_private_address_is_rejected():
    connector = Connector({PUBLIC: http(302, b"", headers=["Location: https://internal.example/admin"])})
    resolver = Resolver({"docs.example": [PUBLIC], "internal.example": ["10.0.0.5"]})
    with pytest.raises(FetchError, match="публичным"):
        fetch_public("https://docs.example/", resolver=resolver, connector=connector)
    assert connector.log["connected"] == [(PUBLIC, 443)]


def test_redirects_are_followed_and_limited():
    connector = Connector({
        PUBLIC: http(301, b"", headers=["Location: https://mirror.example/page"]),
        OTHER: http(body=b"plain text", content_type="text/plain"),
    })
    resolver = Resolver({"docs.example": [PUBLIC], "mirror.example": [OTHER]})
    page = fetch_public("https://docs.example/", resolver=resolver, connector=connector)
    assert page.url == "https://mirror.example/page" and page.text == "plain text"
    loop = Connector({PUBLIC: http(302, b"", headers=["Location: https://docs.example/next"])})
    with pytest.raises(FetchError, match="перенаправлений"):
        fetch_public("https://docs.example/", resolver=Resolver({"docs.example": [PUBLIC]}), connector=loop)
    assert len(loop.log["connected"]) == 4


def test_large_and_binary_answers_are_rejected():
    big = Connector({PUBLIC: http(body=b"a" * (MAX_BYTES + 1), content_type="text/plain")})
    with pytest.raises(FetchError, match="200 КБ"):
        fetch_public("https://docs.example/", resolver=Resolver({"docs.example": [PUBLIC]}), connector=big)
    binary = Connector({PUBLIC: http(body=b"{}", content_type="application/json")})
    with pytest.raises(FetchError, match="text/html"):
        fetch_public("https://docs.example/", resolver=Resolver({"docs.example": [PUBLIC]}), connector=binary)


def test_environment_proxy_is_not_used(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    connector = Connector({PUBLIC: http()})
    fetch_public("https://docs.example/", resolver=Resolver({"docs.example": [PUBLIC]}), connector=connector)
    assert connector.log["connected"] == [(PUBLIC, 443)]


def test_dns_answer_change_does_not_change_connection():
    resolver = Resolver({"docs.example": [[PUBLIC], ["127.0.0.1"]]})
    connector = Connector({PUBLIC: http()})
    fetch_public("https://docs.example/", resolver=resolver, connector=connector)
    assert resolver.calls == ["docs.example"]
    assert connector.log["connected"] == [(PUBLIC, 443)]


def test_research_sources_reach_spec(office, origin, tmp_path):
    app = office.graph_app(origin, tmp_path)
    client, me = office.login(app=app)
    gateway = app.state.gateway
    gateway.handlers["supervisor"] = lambda call: {
        "summary": "Кнопка копирования.",
        "acceptance_criteria": ["Кнопка видна."],
        "needs_research": True,
        "research_questions": ["Как работает Clipboard API?"],
    }
    gateway.handlers["researcher"] = lambda call: (
        {"urls": ["https://docs.example/clipboard", "http://insecure.example/", "https://internal.example/"]}
        if call.step == "research_plan"
        else {"notes": "Используйте navigator.clipboard.writeText.", "sources": [
            {"title": "Clipboard", "url": "https://docs.example/clipboard"},
            {"title": "Чужой", "url": "https://unknown.example/"},
        ]}
    )
    app.state.runner.fetch_options = {
        "resolver": Resolver({"docs.example": [PUBLIC], "internal.example": ["10.0.0.7"]}),
        "connector": Connector({PUBLIC: http()}),
    }
    task_id = app.state.store.create_task(me["id"], UI_TASK, ModelProfile.hybrid, IntakeOutcome(True, None)).id
    app.state.store.promote_queue()
    wait_for(lambda: app.state.store.get_task(task_id).spec, 30, "spec")
    spec = app.state.store.get_task(task_id).spec
    assert [source.model_dump() for source in spec.sources] == [{"title": "Clipboard", "url": "https://docs.example/clipboard"}]
    fetches = [
        (event.payload["arguments"], event.payload["result"])
        for event in app.state.store.events_after(task_id, 0, 1000)
        if event.tool == "fetch_public"
    ]
    assert fetches == [
        ("https://docs.example/clipboard", "ok"),
        ("http://insecure.example/", "denied"),
        ("https://internal.example/", "denied"),
    ]
    notes_call = gateway.role_calls("researcher")[-1]
    assert "Docs Hello" in notes_call.user
    assert app.state.store.get_task(task_id).status == TaskStatus.awaiting_spec_approval
