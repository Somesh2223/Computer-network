"""Live mode for the explorer: run the real simulator on your own inputs.

  python app.py                 then open http://127.0.0.1:8000
  python app.py --port 8080

Serves frontend/explorer.html. In this mode the page's "Your scenario" panel
is switched on: describe your own fields and link, press Run, and the page
re-draws its episodes, replay and field table from a fresh simulator run of
ours, full ARQ and plain UDP on identical losses. Nothing leaves this machine.
"""

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "frontend"))

from scenario import fields_from_spec, run_scenario    # noqa: E402

PAGE = os.path.join(HERE, "frontend", "explorer.html")
MAX_DURATION_S = 60


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, separators=(",", ":")).encode(), "application/json")

    def do_GET(self):
        if self.path in ("/", "/index.html", "/explorer.html"):
            if not os.path.exists(PAGE):
                self._send(500, b"Run `python frontend/build.py` first.", "text/plain")
                return
            with open(PAGE, "rb") as fh:
                self._send(200, fh.read(), "text/html; charset=utf-8")
        elif self.path == "/api/ping":
            self._json(200, {"live": True})
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/run":
            self._send(404, b"not found", "text/plain")
            return
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            fields = fields_from_spec(req["fields"])
            loss, rtt = float(req["loss"]), float(req["rtt_ms"]) / 1000
            duration, seed = float(req["duration"]), int(req["seed"])
            if not 0 <= loss <= 0.5:
                raise ValueError("Loss rate must be between 0% and 50%.")
            if not 0.005 <= rtt <= 2:
                raise ValueError("Round trip must be between 5 ms and 2000 ms.")
            if not 5 <= duration <= MAX_DURATION_S:
                raise ValueError(f"Duration must be between 5 and {MAX_DURATION_S} seconds.")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc) if isinstance(exc, ValueError) else f"Missing or bad input: {exc}"})
            return
        started = time.perf_counter()
        data = run_scenario(fields, seed=seed, duration=duration, loss=loss, rtt=rtt, burst=bool(req.get("burst", True)))
        data["took_s"] = round(time.perf_counter() - started, 2)
        self._json(200, data)

    def log_message(self, fmt, *args):
        sys.stderr.write("  " + fmt % args + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    server = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Explorer in live mode at http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
