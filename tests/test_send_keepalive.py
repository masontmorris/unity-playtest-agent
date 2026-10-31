import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from playtest.bench import replay


@pytest.fixture
def server(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.server.connections.add(self.client_address)
            self.server.requests += 1
            self.rfile.read(int(self.headers["Content-Length"]))
            mode = self.server.mode
            if mode == "drop":
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b"partial")
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            payload = b"not json" if mode == "bad" else b'{"ok":true}'
            self.send_response(500 if mode == "error" else 200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.connections = set()
    httpd.requests = 0
    httpd.mode = "ok"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("LIQUID_API_KEY", "test")
    monkeypatch.setenv("LIQUID_D1.1_URL", f"http://127.0.0.1:{httpd.server_port}/test")
    monkeypatch.delenv("UPA_S1_KEEPALIVE", raising=False)
    yield httpd
    with replay._pool_lock:
        for connection, _ in replay._connection_pool.values():
            connection.close()
        replay._connection_pool.clear()
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def send():
    return replay.send_systemone("liquid-d1-ultra", {"hello": "world"})


def test_reuse_and_responses(server):
    assert send()[:2] == (200, {"ok": True})
    assert send()[:2] == (200, {"ok": True})
    assert len(server.connections) == 1
    server.mode = "error"
    status, data, _, problem = send()
    assert (status, data, problem) == (500, None, "HTTP error")
    server.mode = "bad"
    status, data, _, problem = send()
    assert (status, data, problem) == (200, None, "response was not JSON")


def test_idle_connection_replaced(server, monkeypatch):
    monkeypatch.setattr(replay, "_KEEPALIVE_IDLE_SECONDS", 0)
    send()
    time.sleep(0.01)
    send()
    assert len(server.connections) == 2


def test_urllib_escape_hatch(server, monkeypatch):
    monkeypatch.setenv("UPA_S1_KEEPALIVE", "0")
    original = replay.urllib.request.urlopen
    calls = []

    def wrapped(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(replay.urllib.request, "urlopen", wrapped)
    assert send()[:2] == (200, {"ok": True})
    assert calls == [1]


def test_mid_response_drop_is_not_retried(server):
    server.mode = "drop"
    status, data, _, problem = send()
    assert (status, data, problem) == (None, None, "transport error")
    assert server.requests == 1
