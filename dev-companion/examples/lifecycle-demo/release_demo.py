"""Deploy/withdraw only this demo's explicit, isolated local output."""
from pathlib import Path
import shutil
import sys

root = Path(__file__).resolve().parent
destination = root / ".dev-companion" / "local-release"
if (root / ".dev-companion").is_symlink() or destination.is_symlink():
    raise SystemExit("拒绝文件链接发布目录")
if sys.argv[1] == "deploy":
    destination.mkdir(exist_ok=False)
    for name in ("app.py", "index.html"):
        shutil.copy2(root / name, destination / name)
elif sys.argv[1] == "rollback":
    for name in ("app.py", "index.html"):
        (destination / name).unlink()
    destination.rmdir()
else:
    raise SystemExit("仅支持 deploy 或 rollback")
print("本地演示输出：", sys.argv[1], destination)
