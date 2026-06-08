"""Local-only burst simulator for the Cenas in-app assistant.

This script is intentionally hostile to production URLs. Use it either against
a localhost dev instance of /assistant/ask or with the built-in synthetic server:

  python scripts/assistant_burst_sim.py --synthetic-mode bad --expect-502
  python scripts/assistant_burst_sim.py --synthetic-mode fixed

The "bad" synthetic mode emulates two sync workers pinned by slow request-path
work and returns local 502s when the local proxy wait is exceeded. The "fixed"
mode emulates a queue-first request path that returns immediately.
"""
from __future__ import annotations

import argparse
import json
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _assert_local_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").casefold()
    if host not in LOCAL_HOSTS:
        raise SystemExit(f"Refusing non-local assistant burst target: {url}")
    if "app.cenaskitchen.com" in url.casefold():
        raise SystemExit("Refusing production target app.cenaskitchen.com")


class SyntheticAssistant:
    def __init__(self, mode: str, slow_seconds: float, proxy_wait_seconds: float, worker_slots: int):
        self.mode = mode
        self.slow_seconds = slow_seconds
        self.proxy_wait_seconds = proxy_wait_seconds
        self.worker_slots = threading.BoundedSemaphore(worker_slots)
        self.background: list[threading.Thread] = []

    def handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "CenasAssistantBurstSynthetic/1.0"

            def log_message(self, _fmt: str, *_args: Any) -> None:
                return

            def _json(self, status: int, body: dict[str, Any]) -> None:
                payload = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self) -> None:
                if urllib.parse.urlparse(self.path).path != "/assistant/ask":
                    self._json(404, {"ok": False, "error": "not_found"})
                    return
                length = int(self.headers.get("Content-Length") or "0")
                if length:
                    self.rfile.read(min(length, 1024 * 256))
                if outer.mode == "fixed":
                    worker = threading.Thread(target=time.sleep, args=(outer.slow_seconds,), daemon=True)
                    worker.start()
                    outer.background.append(worker)
                    self._json(200, {"ok": True, "queued": True, "route_path": "review"})
                    return

                acquired = outer.worker_slots.acquire(timeout=outer.proxy_wait_seconds)
                if not acquired:
                    self._json(502, {"ok": False, "error": "local_worker_exhausted"})
                    return
                try:
                    time.sleep(outer.slow_seconds)
                    self._json(200, {"ok": True, "queued": False, "route_path": "general"})
                finally:
                    outer.worker_slots.release()

        return Handler


def _post_one(url: str, index: int, timeout: float) -> dict[str, Any]:
    body = json.dumps({"question": f"local burst question {index}"}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
            return {
                "index": index,
                "status": res.status,
                "ms": int((time.perf_counter() - started) * 1000),
                "body": data,
            }
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return {
            "index": index,
            "status": exc.code,
            "ms": int((time.perf_counter() - started) * 1000),
            "body": raw,
        }


def fire_burst(url: str, n: int, gap_ms: int, timeout: float) -> list[dict[str, Any]]:
    _assert_local_url(url)
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = []
        for index in range(n):
            futures.append(pool.submit(_post_one, url, index, timeout))
            time.sleep(gap_ms / 1000)
        for future in as_completed(futures):
            results.append(future.result())
    return sorted(results, key=lambda item: item["index"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Local-only assistant burst simulator.")
    parser.add_argument("--target", help="Local /assistant/ask URL. Must be localhost/127.0.0.1.")
    parser.add_argument("--synthetic-mode", choices=("bad", "fixed"), default="fixed")
    parser.add_argument("--n", type=int, default=4)
    parser.add_argument("--gap-ms", type=int, default=25)
    parser.add_argument("--slow-seconds", type=float, default=2.0)
    parser.add_argument("--proxy-wait-seconds", type=float, default=0.2)
    parser.add_argument("--worker-slots", type=int, default=2)
    parser.add_argument("--request-timeout", type=float, default=10.0)
    parser.add_argument("--expect-502", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="assistant-burst-") as tmp:
        db_path = Path(tmp) / "assistant_review.sqlite"
        # The path is printed only as a local temp location, never a prod DB.
        url = args.target
        httpd = None
        if not url:
            port = _free_port()
            synthetic = SyntheticAssistant(
                args.synthetic_mode,
                args.slow_seconds,
                args.proxy_wait_seconds,
                args.worker_slots,
            )
            httpd = ThreadingHTTPServer(("127.0.0.1", port), synthetic.handler())
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{port}/assistant/ask"
        _assert_local_url(url)
        try:
            results = fire_burst(url, args.n, args.gap_ms, args.request_timeout)
        finally:
            if httpd is not None:
                httpd.shutdown()
                httpd.server_close()

    statuses = [item["status"] for item in results]
    saw_502 = any(status == 502 for status in statuses)
    if args.json:
        print(json.dumps({"target": url, "statuses": statuses, "results": results}, indent=2, sort_keys=True))
    else:
        print(f"assistant local burst target: {url}")
        print(f"throwaway review DB path: {db_path}")
        print(f"statuses: {statuses}")
    if args.expect_502:
        return 0 if saw_502 else 1
    return 1 if saw_502 else 0


if __name__ == "__main__":
    raise SystemExit(main())
