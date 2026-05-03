import unittest
from unittest.mock import patch

from labgpu.remote import ai_gateway


TOKEN_ONE = "labgpu-session-abcdefghijklmnopqrstuvwxyz012345"
TOKEN_TWO = "labgpu-session-uvwxyzabcdefghijklmnopqrstuvwxyz"


class FakeResponse:
    status = 200
    reason = "OK"

    def read(self):
        return b'{"ok":true}'

    def getheaders(self):
        return [("Content-Type", "application/json")]


class FakeStreamingResponse:
    status = 200
    reason = "OK"

    def __init__(self):
        self.chunks = [b"data: one\n\n", b"data: two\n\n", b""]

    def read(self, _size=None):
        return b"".join(iter(self.chunks.pop, b""))

    def read1(self, _size=None):
        return self.chunks.pop(0)

    def getheaders(self):
        return [("Content-Type", "text/event-stream"), ("Transfer-Encoding", "chunked"), ("Content-Length", "999")]


class FakeWriter:
    def __init__(self):
        self.chunks = []
        self.flush_count = 0

    def write(self, chunk):
        self.chunks.append(chunk)

    def flush(self):
        self.flush_count += 1


class FakeConnection:
    calls = []

    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout

    def request(self, method, path, body=None, headers=None):
        self.calls.append(
            {
                "host": self.host,
                "port": self.port,
                "method": method,
                "path": path,
                "body": body,
                "headers": headers or {},
            }
        )

    def getresponse(self):
        return FakeResponse()

    def close(self):
        return None


class FakeHttpsConnection(FakeConnection):
    pass


class FakeServer:
    instances = []

    def __init__(self, address, handler):
        self.requested_address = address
        self.handler = handler
        self.server_address = (address[0], 49231)
        self.shutdown_called = False
        self.close_called = False
        self.instances.append(self)

    def serve_forever(self):
        return None

    def shutdown(self):
        self.shutdown_called = True

    def server_close(self):
        self.close_called = True


class FakeThread:
    instances = []

    def __init__(self, target=None, args=(), name="", daemon=False):
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.started = False
        self.joined = False
        self.instances.append(self)

    def start(self):
        self.started = True

    def join(self, timeout=None):
        self.joined = True


class AIGatewayTest(unittest.TestCase):
    def setUp(self):
        FakeConnection.calls = []
        FakeServer.instances = []
        FakeThread.instances = []

    def test_token_validation_accepts_x_api_key_and_authorization(self):
        self.assertTrue(ai_gateway.request_has_token({"x-api-key": TOKEN_ONE}, TOKEN_ONE))
        self.assertTrue(ai_gateway.request_has_token({"authorization": f"Bearer {TOKEN_ONE}"}, TOKEN_ONE))
        self.assertTrue(ai_gateway.request_has_token({"Authorization": f"Bearer {TOKEN_ONE}"}, TOKEN_ONE))
        self.assertFalse(ai_gateway.request_has_token({}, TOKEN_ONE))
        self.assertFalse(ai_gateway.request_has_token({"x-api-key": TOKEN_TWO}, TOKEN_ONE))

    def test_forward_request_strips_session_token_before_proxy(self):
        headers = {
            "Host": "127.0.0.1:27183",
            "x-api-key": TOKEN_ONE,
            "Authorization": f"Bearer {TOKEN_ONE}",
            "Content-Type": "application/json",
        }
        with patch("labgpu.remote.ai_gateway.http.client.HTTPConnection", FakeConnection):
            status, reason, response_headers, response_body = ai_gateway.forward_request(
                method="POST",
                path="/v1/messages?beta=true",
                headers=headers,
                body=b"{}",
                target_host="127.0.0.1",
                target_port=15721,
            )

        self.assertEqual(status, 200)
        self.assertEqual(reason, "OK")
        self.assertEqual(response_headers, [("Content-Type", "application/json")])
        self.assertEqual(response_body, b'{"ok":true}')
        self.assertEqual(FakeConnection.calls[0]["path"], "/v1/messages?beta=true")
        outbound = FakeConnection.calls[0]["headers"]
        self.assertEqual(outbound["Host"], "127.0.0.1:15721")
        self.assertNotIn("x-api-key", outbound)
        self.assertNotIn("Authorization", outbound)
        self.assertNotIn(TOKEN_ONE, str(outbound))

    def test_forward_request_can_use_direct_provider_upstream(self):
        headers = {
            "Host": "127.0.0.1:27183",
            "Authorization": f"Bearer {TOKEN_ONE}",
            "Content-Type": "application/json",
        }
        with patch("labgpu.remote.ai_gateway.http.client.HTTPSConnection", FakeHttpsConnection):
            status, reason, _response_headers, _response_body = ai_gateway.forward_request(
                method="POST",
                path="/v1/responses",
                headers=headers,
                body=b"{}",
                target_host="127.0.0.1",
                target_port=15721,
                target_base_url="https://api.example.test/v1",
                upstream_headers={"Authorization": "Bearer sk-provider"},
            )

        self.assertEqual(status, 200)
        self.assertEqual(reason, "OK")
        self.assertEqual(FakeHttpsConnection.calls[0]["host"], "api.example.test")
        self.assertEqual(FakeHttpsConnection.calls[0]["port"], 443)
        self.assertEqual(FakeHttpsConnection.calls[0]["path"], "/v1/responses")
        outbound = FakeHttpsConnection.calls[0]["headers"]
        self.assertEqual(outbound["Host"], "api.example.test")
        self.assertEqual(outbound["Authorization"], "Bearer sk-provider")
        self.assertNotIn(TOKEN_ONE, str(outbound))

    def test_rewrite_upstream_path_prepends_base_path_once(self):
        self.assertEqual(ai_gateway.rewrite_upstream_path("/responses", "/v1"), "/v1/responses")
        self.assertEqual(ai_gateway.rewrite_upstream_path("/v1/responses", "/v1"), "/v1/responses")
        self.assertEqual(ai_gateway.rewrite_upstream_path("/responses?stream=true", "/v1"), "/v1/responses?stream=true")

    def test_start_gateway_is_loopback_only_and_closeable(self):
        with (
            patch("labgpu.remote.ai_gateway.ThreadingHTTPServer", FakeServer),
            patch("labgpu.remote.ai_gateway.threading.Thread", FakeThread),
        ):
            session = ai_gateway.start_ai_gateway(target_port=15721, token=TOKEN_ONE)

        self.assertEqual(session.listen_host, "127.0.0.1")
        self.assertEqual(session.listen_port, 49231)
        self.assertEqual(FakeServer.instances[0].requested_address, ("127.0.0.1", 0))
        self.assertTrue(FakeThread.instances[0].daemon)
        self.assertTrue(FakeThread.instances[0].started)
        self.assertTrue(FakeThread.instances[1].daemon)
        self.assertTrue(FakeThread.instances[1].started)

        session.close()
        self.assertTrue(FakeServer.instances[0].shutdown_called)
        self.assertTrue(FakeServer.instances[0].close_called)
        self.assertTrue(FakeThread.instances[0].joined)
        self.assertTrue(FakeThread.instances[1].joined)

        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            ai_gateway.start_ai_gateway(target_port=15721, token=TOKEN_ONE, listen_host="0.0.0.0")

    def test_session_tokens_are_validated_and_do_not_cross_authorize(self):
        self.assertTrue(ai_gateway.is_session_token(TOKEN_ONE))
        self.assertTrue(ai_gateway.is_session_token(TOKEN_TWO))
        self.assertFalse(ai_gateway.is_session_token("sk-real-provider-key"))
        self.assertFalse(ai_gateway.request_has_token({"x-api-key": TOKEN_ONE}, TOKEN_TWO))

    def test_streaming_response_keeps_sse_streaming_and_flushes_chunks(self):
        headers = [("Content-Type", "text/event-stream"), ("Transfer-Encoding", "chunked"), ("Content-Length", "999")]
        self.assertTrue(ai_gateway.is_streaming_response(headers))
        self.assertEqual(ai_gateway.filtered_response_headers(headers, include_content_length=False), [("Content-Type", "text/event-stream")])

        writer = FakeWriter()
        ai_gateway.stream_response(FakeStreamingResponse(), writer, chunk_size=4)

        self.assertEqual(writer.chunks, [b"data: one\n\n", b"data: two\n\n"])
        self.assertEqual(writer.flush_count, 2)

    def test_gateway_state_tracks_idle_and_lifetime_expiration(self):
        state = ai_gateway.GatewayState(
            token=TOKEN_ONE,
            created_at=10,
            last_accessed=20,
            idle_timeout_seconds=30,
            max_lifetime_seconds=100,
        )
        self.assertFalse(state.is_expired(now=40))
        self.assertTrue(state.is_expired(now=51))
        state.touch(now=60)
        self.assertFalse(state.is_expired(now=89))
        self.assertTrue(state.is_expired(now=111))

    def test_session_health_payload_is_token_safe_and_includes_metadata(self):
        state = ai_gateway.GatewayState(
            token=TOKEN_ONE,
            created_at=10,
            last_accessed=20,
            idle_timeout_seconds=30,
            max_lifetime_seconds=100,
            metadata=ai_gateway.safe_session_metadata(
                {
                    "mode": "proxy_tunnel",
                    "app": "claude",
                    "provider": "PackyCode",
                    "server": "alpha_liu",
                    "remote_cwd": "/data/lsg/work/OPSD",
                    "ccswitch_proxy_port": 15721,
                    "ignored_secret": TOKEN_ONE,
                }
            ),
        )
        with patch("labgpu.remote.ai_gateway.time.monotonic", return_value=25):
            payload = ai_gateway.session_health_payload(state, target_host="127.0.0.1", target_port=15721)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "proxy_tunnel")
        self.assertEqual(payload["app"], "claude")
        self.assertEqual(payload["provider"], "PackyCode")
        self.assertEqual(payload["server"], "alpha_liu")
        self.assertEqual(payload["remote_cwd"], "/data/lsg/work/OPSD")
        self.assertEqual(payload["ccswitch_proxy_port"], "15721")
        self.assertEqual(payload["target_port"], 15721)
        self.assertEqual(payload["token_fingerprint"], ai_gateway.token_fingerprint(TOKEN_ONE))
        self.assertNotIn(TOKEN_ONE, str(payload))
        self.assertNotIn("ignored_secret", payload)


if __name__ == "__main__":
    unittest.main()
