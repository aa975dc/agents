"""Real local checks. These HTTP requests do not constitute browser/user acceptance."""
import importlib.util
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

mode = sys.argv[1]
root = Path(__file__).resolve().parent
target = root / ".dev-companion" / "local-release" if mode == "deployed" else root
spec = importlib.util.spec_from_file_location("demo_app", target / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
assert app.total([20, 30]) == 50, "20 + 30 应为 50"
for invalid in ([-1, 20], ["bad"], [], [True], [float("inf")], None):
    try:
        app.total(invalid)
    except ValueError:
        pass
    else:
        raise AssertionError("非法金额必须被拒绝：%r" % (invalid,))
if mode in ("integration", "deployed"):
    app.Handler.root = target
    with ThreadingHTTPServer(("127.0.0.1", 0), app.Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = "http://127.0.0.1:%s" % server.server_port
        try:
            with urlopen(url + "/", timeout=5) as response:
                page = response.read().decode()
                assert 'id="sum-form"' in page and "计算合计" in page and "/api/sum" in page
            with urlopen(url + "/api/version", timeout=5) as response:
                assert json.load(response)["version"] == "0.2.0-demo"
            for values, expected in (([20, 30], 200), ([-1], 422), (["bad"], 422)):
                request = Request(url + "/api/sum", data=json.dumps({"values": values}).encode(), headers={"Content-Type": "application/json"})
                try:
                    with urlopen(request, timeout=5) as response:
                        assert response.status == expected
                        assert json.load(response)["total"] == 50
                except HTTPError as exc:
                    assert exc.code == expected
                    assert "error" in json.load(exc)
        finally:
            server.shutdown()
            worker.join(timeout=5)
print(json.dumps({"passed": True, "mode": mode, "version": app.VERSION, "human_trial": False,
                  "evidence": "真实 Python 逻辑及临时端口 HTTP 检查；未操作浏览器"}, ensure_ascii=False))
