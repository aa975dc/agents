// 任务清单前端：fetch /api/tasks，按 prototype.md 五态矩阵渲染。
// 可写范围归 impl-frontend；app/ 后端文件归 impl-backend，此处不写。
const list = document.getElementById("tasks");
const empty = document.getElementById("empty");
const error = document.getElementById("error");

async function call(action, payload) {
  const response = await fetch("/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: action, payload: payload })
  });
  const data = await response.json();
  if (!response.ok) { throw new Error(data.error || "请求失败"); }
  return data;
}

async function refresh() {
  const data = await call("list_tasks");       // loading → success / error
  list.textContent = "";
  empty.hidden = data.tasks.length > 0;        // empty 态
  for (const task of data.tasks) {
    const item = document.createElement("li");
    if (task.done) { item.className = "done"; }
    const label = document.createElement("span");
    label.textContent = (task.done ? "\u2713 " : "") + task.title;
    item.appendChild(label);
    if (!task.done) {
      const button = document.createElement("button");
      button.textContent = "完成";
      button.onclick = async () => {           // partial 态：单条失败不清列表
        error.textContent = "";
        try { await call("complete_task", { task_id: task.id }); refresh(); }
        catch (exc) { error.textContent = exc.message; }
      };
      item.appendChild(button);
    }
    list.appendChild(item);
  }
}

document.getElementById("add-form").onsubmit = async (event) => {
  event.preventDefault();
  error.textContent = "";
  const input = document.getElementById("title");
  try { await call("add_task", { title: input.value }); input.value = ""; refresh(); }
  catch (exc) { error.textContent = exc.message; }
};

refresh().catch((exc) => { error.textContent = "加载失败：" + exc.message; });
