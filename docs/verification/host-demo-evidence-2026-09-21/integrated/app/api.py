"""todo 后端：本地任务清单 API（契约 notes/api-contract.json v2）。

运行：python3 app/api.py --port 8765 [--data data/tasks.json]，仅绑定 127.0.0.1。
同端口托管静态前端：GET / 返回 web/index.html，未提供时返回占位页（不报 500）。
handle(action, payload) 为纯函数入口，供测试与 HTTP 层共用：
  - list ：{"done": 可选 "true"/"false"}
  - add  ：{"title": str, "expected_revision": int}
  - patch：{"id": int, "done": bool, "expected_revision": int}
返回 {"status": int, "body": dict}；错误体 {"error": code, "message": str}。
E_PARAM→400，E_STATE→409，E_PROJECT→500。
"""
import argparse
import json
import os
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

try:
    from .store import StoreError, load_store, save_store
except ImportError:  # 直接以脚本运行（python3 app/api.py）时
    from store import StoreError, load_store, save_store

DEFAULT_DATA_PATH = "data/tasks.json"
INDEX_PATH = "web/index.html"

PLACEHOLDER_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>任务清单</title></head>
<body>
<h1>任务清单</h1>
<p>占位页：web/index.html 尚未提供（由前端任务产出）。API 已可用：
GET /api/tasks、POST /api/tasks、PATCH /api/tasks/{id}。</p>
</body>
</html>
"""


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _error(status, code, message):
    return {"status": status, "body": {"error": code, "message": message}}


def handle(action, payload, data_path=DEFAULT_DATA_PATH):
    try:
        return _dispatch(action, payload, data_path)
    except StoreError as exc:
        return _error(500, exc.code, exc.message)


def _dispatch(action, payload, data_path):
    if action == "list":
        return _list_tasks(payload, data_path)
    if action == "add":
        return _add_task(payload, data_path)
    if action == "patch":
        return _patch_task(payload, data_path)
    return _error(400, "E_PARAM", f"未知操作：{action}")


def _list_tasks(payload, data_path):
    done = payload.get("done")
    if done is not None:
        if done in ("true", "false"):
            done = done == "true"
        elif not isinstance(done, bool):
            return _error(400, "E_PARAM", "查询参数 done 仅接受 true/false")
    store = load_store(data_path)
    tasks = sorted(store["tasks"], key=lambda t: t.get("id", 0))  # 契约：按 id 升序
    if done is not None:
        tasks = [t for t in tasks if t.get("done") is done]
    return {"status": 200,
            "body": {"revision": store["revision"], "count": len(tasks), "tasks": tasks}}


def _add_task(payload, data_path):
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        return _error(400, "E_PARAM", "title 必填且 trim 后非空")
    expected = payload.get("expected_revision")
    if not _is_int(expected):
        return _error(400, "E_PARAM", "expected_revision 必填且为整数")
    store = load_store(data_path)
    if expected != store["revision"]:
        return _error(409, "E_STATE",
                      f"revision 已前进到 {store['revision']}（期望 {expected}），请重读后重试")
    new_id = max((t.get("id", 0) for t in store["tasks"]), default=0) + 1
    task = {"id": new_id, "title": title.strip(), "done": False,
            "created_at": _now_iso(), "completed_at": None}
    store["tasks"].append(task)
    store["revision"] += 1
    save_store(data_path, store)
    return {"status": 200, "body": {"revision": store["revision"], "task": task}}


def _patch_task(payload, data_path):
    task_id = payload.get("id")
    if not _is_int(task_id):
        return _error(400, "E_PARAM", "路径 id 必须是正整数")
    done = payload.get("done")
    if not isinstance(done, bool):
        return _error(400, "E_PARAM", "done 必填且为布尔")
    expected = payload.get("expected_revision")
    if not _is_int(expected):
        return _error(400, "E_PARAM", "expected_revision 必填且为整数")
    store = load_store(data_path)
    task = next((t for t in store["tasks"] if t.get("id") == task_id), None)
    if task is None:
        return _error(400, "E_PARAM", f"任务不存在：id={task_id}")
    if expected != store["revision"]:
        return _error(409, "E_STATE",
                      f"revision 已前进到 {store['revision']}（期望 {expected}），请重读后重试")
    if task["done"] == done:  # 同状态 no-op：不写文件、revision 不递增
        return {"status": 200, "body": {"revision": store["revision"], "task": task}}
    task["done"] = done
    task["completed_at"] = _now_iso() if done else None  # 回退复位为 null（契约默认）
    store["revision"] += 1
    save_store(data_path, store)
    return {"status": 200, "body": {"revision": store["revision"], "task": task}}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._respond_html(self._read_index())
            return
        if parsed.path == "/api/tasks":
            done = parse_qs(parsed.query).get("done", [None])[0]
            self._respond_api(handle("list", {"done": done}, self.server.data_path))
            return
        self._respond(404, {"error": "E_PARAM", "message": f"未知路径：{parsed.path}"})

    def do_POST(self):
        if urlparse(self.path).path != "/api/tasks":
            self._respond(404, {"error": "E_PARAM", "message": "未知路径"})
            return
        body = self._read_json_body()
        if body is None:
            self._respond(400, {"error": "E_PARAM", "message": "请求体必须是 JSON 对象"})
            return
        self._respond_api(handle("add", body, self.server.data_path))

    def do_PATCH(self):
        m = re.fullmatch(r"/api/tasks/([^/]+)", urlparse(self.path).path)
        if not m:
            self._respond(404, {"error": "E_PARAM", "message": "未知路径"})
            return
        if not m.group(1).isdigit():
            self._respond(400, {"error": "E_PARAM", "message": f"路径 id 非整数：{m.group(1)}"})
            return
        body = self._read_json_body()
        if body is None:
            self._respond(400, {"error": "E_PARAM", "message": "请求体必须是 JSON 对象"})
            return
        payload = dict(body)
        payload["id"] = int(m.group(1))
        self._respond_api(handle("patch", payload, self.server.data_path))

    def _read_index(self):
        if os.path.exists(INDEX_PATH):
            with open(INDEX_PATH, "rb") as f:
                return f.read()
        return PLACEHOLDER_HTML.encode("utf-8")

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _respond_api(self, result):
        self._respond(result["status"], result["body"])

    def _respond(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _respond_html(self, content):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, fmt, *args):  # 本地单用户，静默访问日志
        pass


def main():
    parser = argparse.ArgumentParser(description="todo 本地任务清单 API 服务")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", default=DEFAULT_DATA_PATH,
                        help="数据文件路径（默认 data/tasks.json，相对项目根）")
    args = parser.parse_args()
    os.makedirs(os.path.dirname(os.path.abspath(args.data)), exist_ok=True)  # 启动自举
    server = HTTPServer(("127.0.0.1", args.port), Handler)
    server.data_path = args.data
    print(f"todo api listening on http://127.0.0.1:{args.port} (data: {args.data})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
