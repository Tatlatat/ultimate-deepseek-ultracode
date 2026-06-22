#!/usr/bin/env python3
"""Claude Code gateway compatibility proxy in front of Claude Code Router.

Claude Code can discover gateway models through GET /v1/models.  CCR currently
does not expose that Anthropic-compatible endpoint, so this proxy answers model
discovery locally and forwards all other requests to the scoped CCR service.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sys
import time
import traceback
from typing import Any
import urllib.error
import urllib.request


JSON = dict[str, Any]
TRUE_VALUES = {"1", "true", "yes", "on"}


def forward_timeout() -> float:
    """Upstream-request timeout for forward().

    Kept just above the gateway's codex-exec budget (CLAUDE_CODEX_GATEWAY_CODEX_TIMEOUT,
    default 600s) so the gateway returns a clean 504 before this outer proxy cuts the
    socket. The old hard-coded 3600s let a wedged upstream hang a subagent for an hour.
    """
    return float(os.getenv("CLAUDE_CODEX_CCR_PROXY_TIMEOUT", "660"))


def json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def trace_enabled() -> bool:
    # On by default so forward/response/timeout events land in the proxy log; the
    # records are one compact JSON line each. Set CLAUDE_CODEX_CCR_PROXY_TRACE=0 to silence.
    return os.getenv("CLAUDE_CODEX_CCR_PROXY_TRACE", "1").lower() in TRUE_VALUES


def system_texts(system: Any) -> list[str]:
    texts: list[str] = []
    if isinstance(system, str):
        texts.append(system)
    elif isinstance(system, dict):
        text = system.get("text")
        if isinstance(text, str):
            texts.append(text)
    elif isinstance(system, list):
        for block in system:
            if isinstance(block, str):
                texts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    texts.append(text)
    return texts


def system_has_subagent_tag(system: Any) -> bool:
    texts = system_texts(system)
    return any(
        line.lstrip().startswith("<CCR-SUBAGENT-MODEL>")
        for text in texts
        for line in text.splitlines()
    )


def system_has_claude_subagent_context(system: Any) -> bool:
    for text in system_texts(system):
        lowered = text.lower()
        if "cc_is_subagent=true" in lowered:
            return True
        if "subagent spawned by a workflow orchestration script" in lowered:
            return True
    return False


def request_route_summary(body: bytes | None) -> JSON:
    payload: JSON = {}
    if body:
        try:
            parsed = json.loads(body.decode("utf-8") or "{}")
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            return {
                "model": "",
                "has_subagent_tag": False,
                "has_subagent_context": False,
                "parse_error": True,
            }

    return {
        "model": str(payload.get("model") or ""),
        "has_subagent_tag": system_has_subagent_tag(payload.get("system")),
        "has_subagent_context": system_has_claude_subagent_context(payload.get("system")),
        "parse_error": False,
    }


class ProxyError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Handler(BaseHTTPRequestHandler):
    server_version = "claude-codex-ccr-proxy/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    @property
    def target(self) -> str:
        return str(self.server.target).rstrip("/")  # type: ignore[attr-defined]

    @property
    def main_target(self) -> str:
        return str(self.server.main_target).rstrip("/")  # type: ignore[attr-defined]

    @property
    def direct_alias_target(self) -> str:
        return str(self.server.direct_alias_target).rstrip("/")  # type: ignore[attr-defined]

    @property
    def api_key(self) -> str:
        return str(self.server.api_key)  # type: ignore[attr-defined]

    @property
    def model_ids(self) -> list[str]:
        return list(self.server.model_ids)  # type: ignore[attr-defined]

    @property
    def alias_model_ids(self) -> set[str]:
        return set(self.server.alias_model_ids)  # type: ignore[attr-defined]

    @property
    def direct_alias_model_ids(self) -> set[str]:
        return set(self.server.direct_alias_model_ids)  # type: ignore[attr-defined]

    @property
    def forced_subagent_model(self) -> str:
        return str(self.server.forced_subagent_model)  # type: ignore[attr-defined]

    @property
    def passthrough_main(self) -> bool:
        return bool(self.server.passthrough_main)  # type: ignore[attr-defined]

    def send_json(self, status: int, data: Any, headers: dict[str, str] | None = None) -> None:
        body = json_bytes(data)
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def trace(self, event: str, **fields: Any) -> None:
        if not trace_enabled():
            return
        record = {
            "time": time.time(),
            "event": event,
            "method": self.command,
            "path": self.path.split("?", 1)[0],
            **fields,
        }
        print(json.dumps(record, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health":
            self.send_json(200, {"ok": True, "time": time.time(), "target": self.target, "main_target": self.main_target})
            return
        if path == "/v1/models":
            models = [
                {"id": model, "type": "model", "display_name": model, "created_at": 0}
                for model in self.model_ids
            ]
            self.send_json(200, {"object": "list", "data": models})
            return
        self.forward()

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or "0")
        body = self.rfile.read(length) if length else None
        self.forward(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-headers", "authorization,x-api-key,content-type,anthropic-version,anthropic-beta")
        self.send_header("access-control-allow-methods", "GET,POST,OPTIONS")
        self.end_headers()

    def forward(self, body: bytes | None = None) -> None:
        route_summary = request_route_summary(body)
        route_target = "unknown"
        try:
            target, to_ccr, body = self.route_for_body(body)
            route_target = "ccr" if to_ccr else "main"
            forward_summary = request_route_summary(body)
            if forward_summary.get("model") != route_summary.get("model"):
                route_summary["forward_model"] = forward_summary.get("model")
            self.trace("forward", route_target=route_target, target=target, **route_summary)
            headers = self.forward_headers(to_ccr)
            req = urllib.request.Request(
                target + self.path,
                data=body,
                headers=headers,
                method=self.command,
            )
            with urllib.request.urlopen(req, timeout=forward_timeout()) as response:
                self.trace("response", route_target=route_target, status=response.status, **route_summary)
                self.send_response(response.status)
                for key, value in response.headers.items():
                    if key.lower() in {"connection", "transfer-encoding", "content-encoding"}:
                        continue
                    self.send_header(key, value)
                self.end_headers()
                # Stream line-by-line, NOT read(65536): a fixed-size read blocks until
                # the buffer fills OR the upstream closes, which holds back the SSE
                # heartbeat trickle (a few bytes every 10s) for minutes. That buffering
                # is exactly why the workflow watchdog still killed lanes at 180s even
                # after the gateway emitted heartbeats — they never reached the client.
                # readline() returns as soon as a `\n`-terminated SSE line is available,
                # so each event (and each heartbeat delta) is forwarded immediately.
                stream = getattr(response, "fp", None) or response
                while True:
                    try:
                        line = stream.readline(65536)
                    except Exception:
                        break
                    if not line:
                        break
                    try:
                        self.wfile.write(line)
                        self.wfile.flush()
                    except BrokenPipeError:
                        self.trace("client_disconnected", route_target=route_target, **route_summary)
                        return
        except urllib.error.HTTPError as exc:
            body = exc.read()
            self.trace("http_error", route_target=route_target, status=exc.code, **route_summary)
            self.send_response(exc.code)
            self.send_header("content-type", exc.headers.get("content-type", "application/json"))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                self.trace("client_disconnected", route_target=route_target, **route_summary)
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            # Upstream wedged with no HTTP status (socket/connect timeout). Surface a
            # clean 504 with an Anthropic-shaped body so the subagent ends with an
            # actionable timeout signal instead of a bare 502 (or an hour-long hang).
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            self.trace("upstream_timeout", route_target=route_target, error=str(reason), **route_summary)
            try:
                self.send_json(
                    504,
                    {"type": "error", "error": {"type": "upstream_timeout", "message": f"gateway timeout: {reason}"}},
                )
            except BrokenPipeError:
                self.trace("client_disconnected", route_target=route_target, **route_summary)
        except Exception as exc:
            if isinstance(exc, BrokenPipeError):
                self.trace("client_disconnected", route_target=route_target, **route_summary)
                return
            self.trace("proxy_error", error=str(exc), **route_summary)
            traceback.print_exc(file=sys.stderr)
            try:
                self.send_json(502, {"type": "error", "error": {"type": "proxy_error", "message": str(exc)}})
            except BrokenPipeError:
                self.trace("client_disconnected", route_target=route_target, **route_summary)

    def force_subagent_model_body(self, body: bytes | None, payload: JSON) -> bytes | None:
        if not body:
            return body
        if not self.forced_subagent_model:
            return body
        model = str(payload.get("model") or "")
        if model in self.alias_model_ids:
            return body
        updated = dict(payload)
        updated["model"] = self.forced_subagent_model
        return json_bytes(updated)

    def route_for_body(self, body: bytes | None) -> tuple[str, bool, bytes | None]:
        if not self.passthrough_main:
            return self.target, True, body
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path not in {"/v1/messages", "/v1/messages/count_tokens"}:
            return self.target, True, body

        payload: JSON = {}
        if body:
            try:
                parsed = json.loads(body.decode("utf-8") or "{}")
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception:
                return self.main_target, False, body

        model = str(payload.get("model") or "")
        if model in self.direct_alias_model_ids and self.direct_alias_target:
            return self.direct_alias_target, True, body
        if model in self.alias_model_ids:
            return self.target, True, body
        if system_has_subagent_tag(payload.get("system")):
            return self.target, True, body
        if system_has_claude_subagent_context(payload.get("system")):
            forced_body = self.force_subagent_model_body(body, payload)
            if self.forced_subagent_model in self.direct_alias_model_ids and self.direct_alias_target:
                return self.direct_alias_target, True, forced_body
            return self.target, True, forced_body
        return self.main_target, False, body

    def forward_headers(self, to_ccr: bool) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key in ("accept", "content-type", "anthropic-version", "anthropic-beta"):
            value = self.headers.get(key)
            if value:
                headers[key] = value
        for key, value in self.headers.items():
            lower = key.lower()
            if lower.startswith("x-claude-code-") or lower.startswith("anthropic-"):
                headers[lower] = value
        if to_ccr and self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        elif not to_ccr:
            for key in ("authorization", "x-api-key"):
                value = self.headers.get(key)
                if value:
                    headers[key] = value
        return headers


class Server(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        target: str,
        main_target: str,
        api_key: str,
        model_ids: list[str],
        alias_model_ids: list[str],
        direct_alias_target: str,
        direct_alias_model_ids: list[str],
        passthrough_main: bool,
    ) -> None:
        super().__init__(server_address, Handler)
        self.target = target
        self.main_target = main_target
        self.direct_alias_target = direct_alias_target
        self.api_key = api_key
        self.model_ids = model_ids
        self.alias_model_ids = alias_model_ids
        self.direct_alias_model_ids = direct_alias_model_ids
        self.passthrough_main = passthrough_main
        self.forced_subagent_model = alias_model_ids[0] if alias_model_ids else ""


def parse_models(raw: str) -> list[str]:
    seen: set[str] = set()
    models: list[str] = []
    for item in raw.split(","):
        model = item.strip()
        if not model or model in seen:
            continue
        seen.add(model)
        models.append(model)
    if not models:
        raise ProxyError(2, "--models must contain at least one model id")
    return models


def main() -> int:
    parser = argparse.ArgumentParser(description="Claude Code compatibility proxy for Claude Code Router")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--port-file", default="")
    parser.add_argument("--target", required=True)
    parser.add_argument("--main-target", default="https://api.anthropic.com")
    parser.add_argument("--direct-alias-target", default="")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--models", required=True)
    parser.add_argument("--alias-models", default="")
    parser.add_argument("--direct-alias-models", default="")
    parser.add_argument("--passthrough-main", action="store_true")
    args = parser.parse_args()

    server = Server(
        (args.host, args.port),
        args.target,
        args.main_target,
        args.api_key,
        parse_models(args.models),
        parse_models(args.alias_models) if args.alias_models else [],
        args.direct_alias_target,
        parse_models(args.direct_alias_models) if args.direct_alias_models else [],
        args.passthrough_main,
    )
    actual_port = int(server.server_address[1])
    if args.port_file:
        Path(args.port_file).write_text(str(actual_port), encoding="utf-8")
    print(f"claude-codex CCR proxy listening on http://{args.host}:{actual_port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
