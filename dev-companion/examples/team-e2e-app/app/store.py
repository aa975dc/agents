"""任务存储：JSON 文件持久化（纯标准库，Py3.9+）。

team-e2e-app 的数据唯一入口：添加/完成/列出任务，落盘 data/tasks.json。
可写范围归 impl-backend；web/ 界面文件归 impl-frontend，本模块不写 web/。
"""
import json
from pathlib import Path

DEFAULT_PATH = Path("data") / "tasks.json"


class TaskStore:
    """添加/完成/列出任务；数据存调用方给定路径的 JSON 文件。"""

    def __init__(self, path=DEFAULT_PATH):
        self.path = Path(path)

    def _load(self):
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, tasks):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(tasks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def add(self, title):
        title = str(title).strip()
        if not title:
            raise ValueError("任务标题不能为空")
        tasks = self._load()
        task = {"id": max((item["id"] for item in tasks), default=0) + 1,
                "title": title, "done": False}
        tasks.append(task)
        self._save(tasks)
        return task

    def complete(self, task_id):
        tasks = self._load()
        for task in tasks:
            if task["id"] == task_id:
                task["done"] = True
                self._save(tasks)
                return task
        raise KeyError("任务不存在：%s" % task_id)

    def list(self):
        return self._load()
