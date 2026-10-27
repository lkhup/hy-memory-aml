import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from tempfile import TemporaryDirectory

import httpx
from fastapi.testclient import TestClient

from multitrack.gateway import create_gateway


def check_real_socket_forwarding():
    from socketserver import UnixStreamServer

    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.server.track, self.path, self.headers["Authorization"], body))
            status = 200 if self.server.track == "coding" else 422
            response = b'{"original":true}  '
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.send_header("Retry-After", "5")
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    def tcp(request):
        raise AssertionError("Track request attempted a TCP connection")

    with TemporaryDirectory(prefix="aml-uds-") as directory:
        sockets = {track: f"{directory}/{track}.sock" for track in ["coding", "multimodal"]}
        servers = []
        threads = []
        try:
            for track, path in sockets.items():
                server = UnixStreamServer(path, Handler)
                server.track = track
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                servers.append(server)
                threads.append(thread)
            targets = {track: "http://private-backend" for track in sockets}
            with TestClient(create_gateway(targets, httpx.MockTransport(tcp), sockets)) as client:
                cases = [("coding", "add", 200), ("multimodal", "search", 422)]
                for track, operation, expected in cases:
                    response = client.post(
                        f"/{track}/{operation}",
                        content=b'{"content":"original image bytes"}',
                        headers={"Authorization": "Bearer original-token"},
                    )
                    assert response.status_code == expected
                    assert response.content == b'{"original":true}  '
                    assert response.headers["retry-after"] == "5"
            assert received == [
                ("coding", "/add", "Bearer original-token", b'{"content":"original image bytes"}'),
                (
                    "multimodal",
                    "/search",
                    "Bearer original-token",
                    b'{"content":"original image bytes"}',
                ),
            ]
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=5)


def check_missing_socket():
    def tcp(request):
        raise AssertionError("Missing private socket must not fall back to an old TCP port")

    with (
        TemporaryDirectory(prefix="aml-uds-") as directory,
        TestClient(
            create_gateway(
                {"coding": "http://unused-tcp"},
                httpx.MockTransport(tcp),
                {"coding": f"{directory}/missing.sock"},
            )
        ) as client,
    ):
        response = client.post("/coding/add", content=b"{}")
        assert response.status_code == 503
        assert response.headers["retry-after"] == "5"


@unittest.skipUnless(os.name == "posix", "Production socket transport runs on Linux")
class GatewaySocketTests(unittest.TestCase):
    def test_original_bytes_auth_and_backend_errors(self):
        check_real_socket_forwarding()

    def test_missing_socket_has_no_tcp_fallback(self):
        check_missing_socket()
