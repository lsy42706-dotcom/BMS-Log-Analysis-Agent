"""Local HTTP/BYOK tests with injected callbacks, never a real model API."""

import http.client
import contextlib
from email.message import Message
import io
import json
import os
import threading
import unittest
from unittest.mock import Mock, patch

from bms_agent import web


CSV = "timestamp_s,cell_voltage_v,current_a,temperature_c\n0,3.7,10,25\n"
TEST_KEY = "TEST_ONLY_EXPLICIT_REQUEST_CREDENTIAL"


class WebBoundaryTests(unittest.TestCase):
    def payload(self, **extra):
        return {"csv_text": CSV, "source_name": "test.csv", **extra}

    def test_offline_default_does_not_forward_dormant_credentials(self):
        callback = Mock(return_value={"mode": "offline"})
        with patch.dict(os.environ, {"OPENAI_API_KEY": TEST_KEY, "OPENAI_MODEL": "environment-model"}):
            result = web.process_api_payload(self.payload(api_key=TEST_KEY, model="ignored"), "analyze", callback)
        self.assertEqual(result, {"mode": "offline"})
        request = callback.call_args.args[0]
        self.assertEqual(request["mode"], "offline")
        self.assertNotIn("api_key", request)
        self.assertNotIn("model", request)

    def test_agent_requires_explicit_key_and_model_without_environment_fallback(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": TEST_KEY, "OPENAI_MODEL": "environment-model"}):
            for extra in ({"mode": "agent"}, {"mode": "agent", "api_key": TEST_KEY}, {"mode": "agent", "model": "test-model"}):
                with self.subTest(extra=extra), self.assertRaises(ValueError):
                    web.validate_api_payload(self.payload(**extra), "analyze")

    def test_key_is_request_scoped_and_removed_from_nested_response(self):
        callback = Mock(return_value={"report": {"api_key": TEST_KEY, "summary": "marker " + TEST_KEY},
                                     "calls": [{"Authorization": TEST_KEY}], "note": "sk-testcredential123"})
        result = web.process_api_payload(self.payload(mode="agent", api_key=TEST_KEY, model="test-model"), "analyze", callback)
        self.assertEqual(callback.call_args.args[0]["api_key"], TEST_KEY)
        encoded = json.dumps(result)
        self.assertNotIn(TEST_KEY, encoded)
        self.assertNotIn("sk-testcredential123", encoded)
        self.assertNotIn("api_key", result["report"])
        self.assertEqual(result["calls"], [{}])

    def test_window_cannot_accept_credentials_or_arbitrary_file_paths(self):
        for operation, fields in (("window", {"api_key": TEST_KEY}), ("analyze", {"path": "/private/log.csv"}), ("analyze", {"output": "/private/report"})):
            with self.subTest(operation=operation, fields=fields), self.assertRaises(ValueError):
                web.validate_api_payload(self.payload(**fields), operation)

    def test_csv_size_encoding_and_schema_boundaries(self):
        for payload in ({"csv_text": "x" * (web.MAX_BYTES + 1)}, {"csv_text": "\ud800"}, {"csv_text": ""}, {"csv_text": 123}, self.payload(mode="shared")):
            with self.subTest(kind=str(type(payload.get("csv_text")))), self.assertRaises(ValueError):
                web.validate_api_payload(payload, "analyze")

    def test_json_rejects_duplicates_nonfinite_wrong_root_and_encoding(self):
        for body in (b'{"csv_text":"first","csv_text":"second"}', b'{"value":NaN}', b'[]', b'null', b'\xff', b''):
            with self.subTest(body=body), self.assertRaises(ValueError):
                web.decode_json(body)

    def test_result_must_be_finite_json_object(self):
        for result in ([], {"value": float("inf")}, {"value": object()}):
            with self.subTest(kind=type(result)), self.assertRaises((ValueError, TypeError)):
                web.process_api_payload(self.payload(), "analyze", Mock(return_value=result))

    def test_host_origin_match_explicit_loopback_port(self):
        self.assertTrue(web._safe_host("127.0.0.1:8765", 8765))
        self.assertTrue(web._safe_origin("http://localhost:8765", 8765))
        for host in ("evil.example:8765", "127.0.0.1:8766", "localhost", "127.0.0.1.evil:8765"):
            self.assertFalse(web._safe_host(host, 8765))
        for origin in ("null", "https://localhost:8765", "http://localhost:8765/", "http://evil.example:8765"):
            self.assertFalse(web._safe_origin(origin, 8765))

    def test_bind_rejects_lan_and_public_hosts_before_socket_creation(self):
        for host in ("0.0.0.0", "192.168.1.2", "8.8.8.8", "localhost", "::1"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                web.LocalServer((host, 8765), web.make_handler(Mock(), Mock()))

    def test_static_assets_do_not_use_untrusted_html_or_browser_storage(self):
        script = (web.ASSET_DIRECTORY / "app.js").read_text(encoding="utf-8")
        page = (web.ASSET_DIRECTORY / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("localStorage", script)
        self.assertNotIn("sessionStorage", script)
        self.assertIn("textContent", script)
        self.assertIn('$("api-key").value = ""', script)
        self.assertIn("PAGE_SIZE = 100", script)
        self.assertIn('src="/assets/app.js"', page)
        self.assertNotIn("<script>", page)
        self.assertIn("frame-ancestors 'none'", web.CSP)

    def test_port_environment_default_and_explicit_override(self):
        server = Mock(server_port=9012)
        server.serve_forever.side_effect = KeyboardInterrupt
        with patch.dict(os.environ, {"PORT": "9012"}), patch("bms_agent.web.create_server", return_value=server) as create, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(web.main([]), 0)
            create.assert_called_once_with("127.0.0.1", 9012)
        server.server_close.assert_called_once()
        with patch.dict(os.environ, {"PORT": "9012"}), patch("bms_agent.web.create_server", return_value=server) as create, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(web.main(["--port", "8766"]), 0)
            create.assert_called_once_with("127.0.0.1", 8766)

    def rejected_handler(self, length=None, transfer_encoding=None, body=b""):
        handler_type = web.make_handler(Mock(), Mock())
        handler = handler_type.__new__(handler_type)
        handler.command = "POST"
        handler.headers = Message()
        if length is not None:
            handler.headers["Content-Length"] = str(length)
        if transfer_encoding is not None:
            handler.headers["Transfer-Encoding"] = transfer_encoding
        handler.connection = Mock()
        handler.connection.gettimeout.return_value = 20
        handler.rfile = Mock(wraps=io.BytesIO(body))
        return handler

    def test_early_rejection_drains_only_small_known_body_once(self):
        handler = self.rejected_handler(length=3, body=b"abc")
        handler._discard_small_rejected_body()
        self.assertTrue(handler._body_consumed)
        self.assertEqual(handler.rfile.read1.call_count, 1)
        handler._discard_small_rejected_body()
        self.assertEqual(handler.rfile.read1.call_count, 1)
        handler.connection.settimeout.assert_any_call(20)

    def test_rejection_drain_does_not_read_unknown_chunked_or_large_body(self):
        for length, transfer in ((None, None), (web.MAX_REJECT_DRAIN_BYTES + 1, None), (web.MAX_REQUEST_BYTES + 1, None), (3, "chunked"), ("x", None), ("9" * 20, None)):
            with self.subTest(length=length, transfer=transfer):
                handler = self.rejected_handler(length, transfer, b"abc")
                handler._discard_small_rejected_body()
                handler.rfile.read1.assert_not_called()

    def test_rejection_drain_has_overall_deadline_not_per_read_deadline(self):
        handler = self.rejected_handler(length=6, body=b"abc")
        with patch("bms_agent.web.time.monotonic", side_effect=[10, 10.1, 10.3]):
            handler._discard_small_rejected_body()
        self.assertFalse(handler._body_consumed)
        self.assertEqual(handler.rfile.read1.call_count, 1)
        handler.connection.settimeout.assert_any_call(20)


class WebHTTPTests(unittest.TestCase):
    """Loopback sockets are needed; run outside restricted socket sandboxes."""

    def setUp(self):
        self.analyze = Mock(return_value={"report": {"mode": "offline"}, "waveform": {"samples": []}, "report_markdown": "# report\n"})
        self.inspect = Mock(return_value={"samples": [], "matched_samples": 0})
        self.server = web.create_server(port=0, analyze_callback=self.analyze, inspect_callback=self.inspect)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, method="GET", path="/health", payload=None, headers=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        selected = {"Content-Type": "application/json", "X-BMS-Local": "1"} if body is not None else {}
        selected.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=selected)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_health_page_assets_and_security_headers(self):
        for path in ("/health", "/", "/assets/app.js", "/assets/style.css"):
            with self.subTest(path=path):
                status, headers, body = self.request(path=path)
                self.assertEqual(status, 200)
                self.assertTrue(body)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["Content-Security-Policy"], web.CSP)

    def test_analyze_and_window_contracts(self):
        status, _, body = self.request("POST", "/api/analyze", {"csv_text": CSV})
        self.assertEqual(status, 200)
        self.assertIn("report_markdown", json.loads(body))
        self.assertEqual(self.analyze.call_args.args[0]["mode"], "offline")
        status, _, _ = self.request("POST", "/api/window", {"csv_text": CSV, "start_s": 0, "end_s": 1})
        self.assertEqual(status, 200)
        self.inspect.assert_called_once()

    def test_host_origin_and_missing_local_header_are_rejected(self):
        for headers in ({"Host": "evil.example"}, {"Origin": "http://evil.example"}, {"X-BMS-Local": ""}):
            with self.subTest(headers=headers):
                status, _, _ = self.request("POST", "/api/analyze", {"csv_text": CSV}, headers)
                self.assertEqual(status, 403)
        self.analyze.assert_not_called()

    def test_cross_origin_preflight_and_bad_content_type_rejected(self):
        self.assertEqual(self.request("OPTIONS", "/api/analyze")[0], 403)
        status, _, _ = self.request("POST", "/api/analyze", {"csv_text": CSV}, {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_repeated_early_rejection_replies_are_receivable_on_windows(self):
        for attempt in range(6):
            for headers, expected in (({"Content-Type": "text/plain"}, 415), ({"Origin": "http://evil.example"}, 403)):
                with self.subTest(attempt=attempt, headers=headers):
                    status, _, body = self.request("POST", "/api/analyze", {"csv_text": CSV}, headers)
                    self.assertEqual(status, expected)
                    self.assertIn("error", json.loads(body))
        self.analyze.assert_not_called()

    def test_paths_do_not_allow_traversal_or_secret_query(self):
        for path in ("/../domain.py", "/assets/../../domain.py", "/assets/%2e%2e/domain.py", "/?api_key=" + TEST_KEY):
            with self.subTest(path=path):
                status, _, body = self.request(path=path)
                self.assertIn(status, {400, 404})
                self.assertNotIn(TEST_KEY.encode(), body)

    def test_provider_error_is_fixed_and_does_not_echo_credentials(self):
        self.analyze.side_effect = RuntimeError("Raw provider exception " + TEST_KEY)
        status, _, body = self.request("POST", "/api/analyze", {"csv_text": CSV, "mode": "agent", "api_key": TEST_KEY, "model": "test-model"})
        self.assertEqual(status, 500)
        self.assertNotIn(TEST_KEY.encode(), body)
        self.assertNotIn(b"Raw provider", body)

    def test_transport_invalid_input_never_invokes_service(self):
        status, _, _ = self.request("POST", "/api/analyze", {"csv_text": CSV, "mode": "agent"})
        self.assertEqual(status, 422)
        self.analyze.assert_not_called()

    def test_response_redacts_callback_secret_even_without_real_api(self):
        self.analyze.return_value = {"report": {"summary": TEST_KEY, "api_key": TEST_KEY}}
        status, _, body = self.request("POST", "/api/analyze", {"csv_text": CSV, "mode": "agent", "api_key": TEST_KEY, "model": "test-model"})
        self.assertEqual(status, 200)
        self.assertNotIn(TEST_KEY.encode(), body)
        self.assertNotIn(b"api_key", body)


if __name__ == "__main__":
    unittest.main()
