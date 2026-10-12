"""Local-only browser interface with an explicit, request-scoped BYOK boundary.

The HTTP server does not save uploads or credentials, read arbitrary paths, or
fall back to environment keys. Analysis is delegated to the same trusted service
used by local clients. Binding to a LAN/public address is deliberately unsupported.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .domain import MAX_BYTES
from .reporting import redact


MAX_REQUEST_BYTES = MAX_BYTES + 1024 * 1024
MAX_REJECT_DRAIN_BYTES = 64 * 1024
REJECT_DRAIN_TIMEOUT_S = 0.25
ASSET_DIRECTORY = Path(__file__).with_name("web_assets")
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/assets/style.css": ("style.css", "text/css; charset=utf-8"),
}
CSP = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
INPUT_FIELDS = {
    "csv_text", "source_name", "column_map", "units", "profile",
    "current_convention", "pack_id", "mode", "question", "api_key", "model",
    "max_turns", "timeout_s", "max_output_tokens", "max_total_tokens",
}
WINDOW_FIELDS = {
    "csv_text", "source_name", "column_map", "units", "profile",
    "current_convention", "pack_id", "start_s", "end_s", "max_samples",
}
SECRET_FIELDS = {"api_key", "openai_api_key", "authorization"}
Callback = Callable[[dict], dict]


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON fields")
        result[key] = value
    return result


def decode_json(body: bytes) -> dict:
    """Reject malformed, duplicate-key and nonfinite JSON before service use."""
    if not body or len(body) > MAX_REQUEST_BYTES:
        raise ValueError("Invalid body length")
    def reject_constant(_value: str) -> None:
        raise ValueError("Nonfinite JSON")
    try:
        result = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object,
                            parse_constant=reject_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Invalid JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("JSON object required")
    return result


def validate_api_payload(payload: dict, operation: str) -> dict:
    """Validate transport boundaries; physical/schema checks stay in service."""
    allowed = INPUT_FIELDS if operation == "analyze" else WINDOW_FIELDS if operation == "window" else set()
    if not isinstance(payload, dict) or not allowed or any(key not in allowed for key in payload):
        raise ValueError("Unsupported request fields")
    content = payload.get("csv_text")
    if not isinstance(content, str) or not content:
        raise ValueError("CSV text required")
    try:
        if len(content.encode("utf-8")) > MAX_BYTES:
            raise ValueError("CSV too large")
    except UnicodeError as exc:
        raise ValueError("Invalid text encoding") from exc
    if "source_name" in payload and (not isinstance(payload["source_name"], str) or len(payload["source_name"]) > 256):
        raise ValueError("Invalid source name")
    result = dict(payload)
    if operation == "analyze":
        mode = result.get("mode", "offline")
        if mode not in {"offline", "agent"}:
            raise ValueError("Invalid mode")
        result["mode"] = mode
        if mode == "agent":
            key, model = result.get("api_key"), result.get("model")
            if not isinstance(key, str) or not key.strip() or len(key) > 1024:
                raise ValueError("Explicit request key required")
            if not isinstance(model, str) or not model.strip() or len(model) > 256:
                raise ValueError("Explicit request model required")
            result["api_key"], result["model"] = key.strip(), model.strip()
        else:
            # A dormant password field must never activate billing in offline mode.
            result.pop("api_key", None)
            result.pop("model", None)
    return result


def _without_credential_fields(value: object) -> object:
    if isinstance(value, dict):
        return {key: _without_credential_fields(item) for key, item in value.items()
                if str(key).casefold() not in SECRET_FIELDS}
    if isinstance(value, list):
        return [_without_credential_fields(item) for item in value]
    return value


def process_api_payload(payload: dict, operation: str, callback: Callback) -> dict:
    selected = validate_api_payload(payload, operation)
    key = payload.get("api_key")
    secrets = (key,) if isinstance(key, str) and key else ()
    result = callback(selected)
    if not isinstance(result, dict):
        raise ValueError("Invalid service response")
    result = _without_credential_fields(redact(result, secrets))
    # No NaN, infinity, or unsupported custom values can enter a response/export.
    json.dumps(result, ensure_ascii=False, allow_nan=False)
    return result


def _safe_host(value: str, port: int) -> bool:
    return value.lower() in {f"127.0.0.1:{port}", f"localhost:{port}"}


def _safe_origin(value: str, port: int) -> bool:
    return value.lower() in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}


def make_handler(analyze_callback: Callback, inspect_callback: Callback) -> type[BaseHTTPRequestHandler]:
    class LocalHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(20)

        def log_message(self, _format: str, *args: object) -> None:
            # URLs, request bodies and provider errors must not enter console logs.
            return

        def _send(self, code: int, content: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(content)

        def _json(self, code: int, payload: dict) -> None:
            self._send(code, json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _error(self, code: int, kind: str, message: str) -> None:
            self._discard_small_rejected_body()
            self._json(code, {"error": {"code": kind, "message": message}})

        def _discard_small_rejected_body(self) -> None:
            """Consume only a small, known body before an early error reply.

            Closing a Windows TCP socket with unread upload bytes can reset the
            connection before the client receives its HTTP error. Draining is
            neither authorization nor parsing: no rejected bytes reach a tool.
            Unknown/chunked/large bodies are never read here; a monotonic deadline
            also bounds deliberately slow clients rather than per-read timeouts.
            """
            if (self.command != "POST" or getattr(self, "_body_consumed", False)
                    or getattr(self, "_reject_drain_attempted", False)):
                return
            self._reject_drain_attempted = True
            lengths = self.headers.get_all("Content-Length", [])
            if (self.headers.get("Transfer-Encoding") or len(lengths) != 1
                    or len(lengths[0]) > 12 or not lengths[0].isdigit()):
                return
            remaining = int(lengths[0])
            if not 0 < remaining <= MAX_REJECT_DRAIN_BYTES:
                return
            previous_timeout = self.connection.gettimeout()
            deadline = time.monotonic() + REJECT_DRAIN_TIMEOUT_S
            try:
                while remaining:
                    available_time = deadline - time.monotonic()
                    if available_time <= 0:
                        break
                    self.connection.settimeout(available_time)
                    # read1 makes at most one raw read, so the overall deadline
                    # is rechecked even when a client continually trickles bytes.
                    chunk = self.rfile.read1(min(8192, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                self._body_consumed = remaining == 0
            except OSError:
                pass
            finally:
                self.connection.settimeout(previous_timeout)

        def _trusted_request(self) -> bool:
            hosts = self.headers.get_all("Host", [])
            origins = self.headers.get_all("Origin", [])
            port = self.server.server_port
            if len(hosts) != 1 or not _safe_host(hosts[0], port) or len(origins) > 1:
                self._error(403, "local_only", "只接受本机页面请求，请使用启动时显示的地址。")
                return False
            if origins and not _safe_origin(origins[0], port):
                self._error(403, "origin_denied", "请求来源不被允许。")
                return False
            return True

        def do_GET(self) -> None:
            if not self._trusted_request():
                return
            try:
                parsed = urlsplit(self.path)
            except ValueError:
                self._error(400, "invalid_path", "无效请求路径。")
                return
            if parsed.query or parsed.fragment or parsed.scheme or parsed.netloc:
                self._error(400, "invalid_path", "无效请求路径。")
            elif parsed.path == "/health":
                self._json(200, {"status": "ok", "local_only": True, "default_mode": "offline"})
            elif parsed.path in ASSETS:
                filename, content_type = ASSETS[parsed.path]
                try:
                    content = (ASSET_DIRECTORY / filename).read_bytes()
                except OSError:
                    self._error(500, "asset_unavailable", "页面资源暂时不可用。")
                    return
                self._send(200, content, content_type)
            else:
                self._error(404, "not_found", "请求资源不存在。")

        def do_POST(self) -> None:
            if not self._trusted_request():
                return
            if self.path not in {"/api/analyze", "/api/window"}:
                self._error(404, "not_found", "请求资源不存在。")
                return
            if self.headers.get("X-BMS-Local") != "1":
                self._error(403, "request_header_required", "缺少本地请求标识。")
                return
            content_types = self.headers.get_all("Content-Type", [])
            lengths = self.headers.get_all("Content-Length", [])
            if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/json":
                self._error(415, "json_required", "仅接受 JSON 请求。")
                return
            if (self.headers.get("Transfer-Encoding") or len(lengths) != 1
                    or len(lengths[0]) > 12 or not lengths[0].isdigit()):
                self._error(411, "length_required", "需要明确的请求长度。")
                return
            length = int(lengths[0])
            if not 0 < length <= MAX_REQUEST_BYTES:
                self._error(413, "body_too_large", "请求为空或超出本地大小限制。")
                return
            if not self.server.api_slots.acquire(blocking=False):
                self._error(429, "busy", "已有分析正在运行，请稍后重试。")
                return
            try:
                body = self.rfile.read(length)
                self._body_consumed = True
                if len(body) != length:
                    raise ValueError("Incomplete body")
                payload = decode_json(body)
                operation = "analyze" if self.path == "/api/analyze" else "window"
                callback = analyze_callback if operation == "analyze" else inspect_callback
                result = process_api_payload(payload, operation, callback)
                self._json(200, result)
            except (ValueError, TypeError, UnicodeError, RecursionError):
                self._error(422, "invalid_input", "输入检查失败，请检查 CSV、列映射、单位、规则配置及本次调用参数。")
            except Exception:
                # Never expose raw provider errors or request material to the browser.
                self._error(500, "analysis_failed", "分析未完成；请检查本地环境或自己的模型权限、余额、网络和调用限制。")
            finally:
                self.server.api_slots.release()

        def do_OPTIONS(self) -> None:
            self._error(403, "cross_origin_denied", "不允许跨来源调用。")

    return LocalHandler


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, server_address: tuple[str, int], handler: type[BaseHTTPRequestHandler]):
        host, port = server_address
        # IPv4-only implementation: accept the explicit, well-known loopback.
        try:
            safe = ipaddress.ip_address(host).is_loopback and host == "127.0.0.1"
        except ValueError:
            safe = False
        if not safe or isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("This local server supports only 127.0.0.1 and a valid port")
        self.api_slots = threading.BoundedSemaphore(2)
        super().__init__(server_address, handler)

    def handle_error(self, request: object, client_address: object) -> None:
        # Disconnected clients must not trigger raw request/exception diagnostics.
        return


def create_server(host: str = "127.0.0.1", port: int = 8765, *,
                  analyze_callback: Callback | None = None,
                  inspect_callback: Callback | None = None) -> LocalServer:
    if analyze_callback is None or inspect_callback is None:
        from .service import analyze_request, inspect_request
        analyze_callback = analyze_callback or analyze_request
        inspect_callback = inspect_callback or inspect_request
    return LocalServer((host, port), make_handler(analyze_callback, inspect_callback))


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="BMS 本地上传页面：默认离线，仅绑定 127.0.0.1。")
    parser.add_argument("--host", default="127.0.0.1", help="仅支持 127.0.0.1")
    parser.add_argument("--port", type=int, default=os.environ.get("PORT", "8765"))
    args = parser.parse_args(argv)
    try:
        server = create_server(args.host, args.port)
    except (OSError, ValueError, ImportError):
        print("本地页面启动失败；请检查 loopback 地址、端口和安装环境。")
        return 2
    print(f"BMS 本地页面：http://127.0.0.1:{server.server_port}", flush=True)
    print("默认离线；上传和 Key 不持久化。按 Ctrl+C 停止。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
