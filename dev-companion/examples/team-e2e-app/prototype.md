# 任务清单应用：需求与设计实例（团队端到端示例）

这是 `tests/team_e2e/test_team_e2e.py` 端到端演练的设计输入实例：五角色流程
（设计 → 两路并行实现 → 独立审查 → 集成 → 真实操作验证）以它为被构建对象。
本文件是给人看的需求与设计 brief；机器可读契约见同目录 `api_contract.json`
（由 `contracts.schemas.validate_api_contract` 校验，测试中经 HandoffRegistry
冻结登记）。示例代码按实现者分工放在 `app/`（后端可写）与 `web/`（前端可写），
两个实现任务的 ownership 声明不相交。

## 需求卡

- **feature_id**：todo-app
- **用户任务**：在本机记录待办事项：添加任务、把任务标记完成、查看全部任务
- **范围**：添加/完成/列出三个操作；数据存本机 JSON 文件；单页 Web 界面
- **不做范围**：删除与编辑、账号登录、多设备同步、公网部署、实时协作
- **成功例子**：添加"买牛奶"后列表出现"买牛奶"；点击完成后显示完成态
- **失败例子**：空标题被拒绝并显示原因；完成不存在的任务编号返回错误原因
- **决策 owner**：用户（范围确认后才进入实现）

## 设计 brief：screen_id = todo-list

**用户操作顺序**：打开页面 → 看到任务列表（或空态提示）→ 输入标题点"添加" →
新任务出现在列表 → 点某条"完成" → 该条变为完成态。

**五态矩阵**（loading / empty / error / partial / success）：

| 状态 | 数据形态 | 界面文案与表现 |
|---|---|---|
| loading | 页面刚打开，列表尚未从 `/api/tasks` 返回 | 空列表占位；错误区为空，无"加载失败"字样 |
| empty | `list_tasks` 返回 `tasks: []` | 显示"还没有任务，添加第一条试试。"，隐藏空态即列表 |
| error | 请求失败或后端返回 422（空标题、未知操作等） | 错误区显示后端 `error` 原文（role=alert），列表保持上一次数据 |
| partial | 某条"完成"操作失败，但列表已正常展示 | 列表完整可见，仅错误区显示该条失败原因，不整页报错 |
| success | 列表正常返回 | 每条任务显示标题；完成条目带删除线与 ✓ 前缀，未完成条目带"完成"按钮 |

**组件与数据映射**：`#add-form`（输入框+添加按钮）→ `add_task`；`#tasks` 列表 →
`list_tasks.tasks[]`（id/title/done）；`#error` 警示区 → 422 响应的 `error` 字段。

**响应式与可达性**：正文最大宽 560px 居中，320px 视口单列无横向滚动；`html`
标注 `lang=zh-CN`；错误区 `role=alert`；按钮为原生元素可键盘操作。

**验收标准**（测试与真实操作逐条对应）：

1. 子进程调用契约入口 `add_task {"title": "买牛奶"}` 后，`list_tasks` 等于含
   `{"title": "买牛奶", "done": false}` 的单元素列表。
2. 数据文件 `data/tasks.json` 出现该任务且 `done` 字段等于 `false`；完成后重读
   `done` 等于 `true`（写入读回断言）。
3. 空标题 `add_task {"title": "  "}` 被拒绝，HTTP 状态等于 422，响应包含
   `error` 字段原文"任务标题不能为空"。
4. 打开页面 `/` 时 HTML 包含"任务清单"与 `/app.js` 引用；`/app.js` 可获取。
5. 完成 `data/tasks.json` 中不存在的任务编号时出现错误原因"任务不存在"。

**架构与分工边界**：纯标准库 Python 3.9+，无第三方依赖。后端实现
（impl-backend）可写 `app/store.py`、`app/api.py`；前端实现（impl-frontend）
可写 `web/index.html`、`web/app.js`；两侧 ownership 不相交，集成由
integration 任务把两个隔离工作区的产物合并后跑版本级回归。演示服务器只绑定
127.0.0.1；`serve(port=0)` 供测试在临时端口启动。
