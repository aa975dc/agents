"""Local-only demonstration server; no persistent user data."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path

VERSION = "0.2.0-demo"


def total(values):
    if not isinstance(values, list) or not values or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values):
        raise ValueError("请输入非负、有限的数字列表")
    result = sum(values)
    if not math.isfinite(result):
        raise ValueError("合计超出支持范围")
    return result


class Handler(BaseHTTPRequestHandler):
    root = Path(__file__).resolve().parent

    def respond(self, code, body, content_type="application/json; charset=utf-8"):
        content = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.path == "/api/version":
            self.respond(200, {"version": VERSION})
        elif self.path == "/":
            self.respond(200, (self.root / "index.html").read_bytes(), "text/html; charset=utf-8")
        else:
            self.respond(404, {"error": "页面不存在"})

    def do_POST(self):
        if self.path != "/api/sum":
            return self.respond(404, {"error": "接口不存在"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 8192:
                raise ValueError("请求为空或过大")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("需要 JSON 对象")
            self.respond(200, {"total": total(data.get("values"))})
        except (ValueError, OverflowError, UnicodeError) as exc:
            self.respond(422, {"error": str(exc)})

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        print("本地演示：http://127.0.0.1:%s" % server.server_port, flush=True)
        server.serve_forever()
