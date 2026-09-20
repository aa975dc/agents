"""本地调用契约入口与 127.0.0.1 演示服务器（纯标准库）。

api_contract.json 的 entry：handle(action, payload)。演示服务器只绑定本机，
服务 web/ 静态页面（impl-frontend 产物，此处只读不写）并把
POST /api/tasks 的 {action, payload} 转发给同一入口。
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.store import TaskStore

VERSION = "0.1.0-team-e2e"

_ACTIONS = ("add_task", "complete_task", "list_tasks")


def handle(action, payload=None, store=None):
    """契约入口：add_task{title} / complete_task{task_id} / list_tasks{}。"""
    payload = payload or {}
    store = store or TaskStore()
    if action == "add_task":
        return {"task": store.add(payload["title"])}
    if action == "complete_task":
        return {"task": store.complete(int(payload["task_id"]))}
    if action == "list_tasks":
        return {"tasks": store.list()}
    raise ValueError("未知操作：%s" % action)


class Handler(BaseHTTPRequestHandler):
    root = Path(__file__).resolve().parents[1] / "web"

    def respond(self, code, body, content_type="application/json; charset=utf-8"):
        content = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.path == "/":
            return self.respond(200, (self.root / "index.html").read_bytes(),
                                "text/html; charset=utf-8")
        if self.path == "/app.js":
            return self.respond(200, (self.root / "app.js").read_bytes(),
                                "text/javascript; charset=utf-8")
        if self.path == "/api/version":
            return self.respond(200, {"version": VERSION})
        self.respond(404, {"error": "页面不存在"})

    def do_POST(self):
        if self.path != "/api/tasks":
            return self.respond(404, {"error": "接口不存在"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 8192:
                raise ValueError("请求为空或过大")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict) or data.get("action") not in _ACTIONS:
                raise ValueError("需要 {action, payload} JSON 对象")
            self.respond(200, handle(data["action"], data.get("payload")))
        except (ValueError, KeyError, OverflowError, UnicodeError) as exc:
            self.respond(422, {"error": str(exc)})

    def log_message(self, *_args):
        pass


def serve(port=8765):
    """启动演示服务器；先打印实际绑定地址再服务（port=0 供测试取临时端口）。"""
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        print("任务清单演示：http://127.0.0.1:%s" % server.server_port, flush=True)
        server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    serve(parser.parse_args().port)
