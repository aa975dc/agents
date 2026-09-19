# 命令行与状态约定

所有命令使用 `python3 "<插件根>/scripts/companion.py" --project "<项目>"` 前缀。新手不必手写 JSON；主会话根据用户确认的内容生成输入文件。

`scope.json` 与 `receipt.json` 是示意文件名。把这些输入存放在**项目以外的临时目录**，或项目 `.dev-companion/inputs/`，不要在派发任务后突然写入项目源码根目录，否则会被算入本次文件变化。输入文件不属于台账；只有 `.dev-companion/inputs/` 可由主会话写入，不能直接改动 `state.json`、快照索引、锁或证据。

| 操作 | 接口 | 作用 |
|---|---|---|
| 检查环境 | `doctor` | 读取运行环境与项目情况 |
| 建立草案 | `init --input scope.json` | 保存需求草案，不等于确认 |
| 确认范围 | `confirm --revision N` | 使用刚读取的状态版本确认范围 |
| 修改范围 | `scope --input scope.json --revision N` | 更新草案，需重新确认 |
| 查看进度 | `status --format markdown` | 从真实状态生成信息卡 |
| 查看原始状态 | `status --format json` | 读取 `revision` 等真实字段 |
| 生成静态看板 | `status --format html --out PATH` | 写展示文件，不改变完成度 |
| 派发任务 | `packet --feature ID` | 开始一次执行，得到任务包及执行编号 |
| 导入回报 | `receipt --input receipt.json` | 记录实现/阻塞回报；实现只到待检查 |
| 执行检查 | `check --feature ID` | 运行预先确认的 argv 检查命令 |
| 接受成果 | `accept --feature ID --note TEXT [--user-confirmed]` | 需要当前内容的成功检查；需要用户验收时还需真实反馈 |
| 记录受阻 | `block --feature ID --reason TEXT` | 记录具体原因，保留未完成项 |
| 保存文件 | `save --paths PATH... --summary TEXT` | 显式选择本地普通文件制作快照 |
| 查看快照 | `history` | 查看已有快照与摘要 |
| 恢复预览 | `preview-restore --archive ID` | 读取影响范围并生成确认令牌 |
| 执行恢复 | `restore --archive ID --token TOKEN` | 用户确认预览且所有写入已停止后执行 |

`revision`、`scope_version`、`run_id`、快照 ID 和恢复令牌都必须从本次真实输出读取，不能猜测、复制示例或用旧会话缓存替代。发生状态版本冲突时重新读取、解释冲突，不自动覆盖。

## 需求输入

```json
{
  "title": "个人记账工具",
  "goal": "快速汇总一组日常支出",
  "audience": "只给自己使用",
  "scenario": "在本机输入支出金额并查看合计",
  "out_of_scope": ["账号和云同步", "上线"],
  "assumptions": [],
  "features": [
    {
      "id": "sum-expenses",
      "title": "计算支出合计",
      "acceptance_criteria": ["20 元和 30 元合计为 50 元", "负数金额应被拒绝"],
      "allowed_paths": ["ledger.py", "test_ledger.py"],
      "check_commands": [["python3", "-m", "unittest", "test_ledger.py"]],
      "requires_user_acceptance": true
    }
  ]
}
```

`allowed_paths` 是项目相对**文件**路径，不是任意目录或通配符。检查命令是 argv 数组，不经 shell 展开；依然会执行项目代码，确认需求时要说明这些检查做什么、有无外部副作用。需要访问网络、付费服务或真实业务数据的检查，应单独明确授权范围。CLI 不是代码执行沙箱，也不是对已确认命令安全性的证明。

## 开发者回报

```json
{
  "feature_id": "sum-expenses",
  "run_id": "从本次 packet 输出复制",
  "scope_version": 1,
  "status": "implemented",
  "summary": "描述实际产物与未覆盖部分",
  "changed_files": ["ledger.py", "test_ledger.py"],
  "evidence_files": ["test_ledger.py"],
  "blocker": null
}
```

无法实现时用 `status: "blocked"` 和具体 `blocker`。证据文件必须真实存在且位于允许范围内。`changed_files` 必须恰好列出本次实际变化的文件，包含真实删除的路径；复核已有实现而没有修改时用空数组，不把“相关文件”当作“本次修改文件”。回报不能直接填写百分比、已验收状态或伪造检查结果。

## 完成度和边界

- 草案阶段：范围待确认，不能计算有效完成度。
- 确认后：已验收功能数 / 当前范围功能总数。各项等权，受阻项仍计入分母。
- 实现回报：待检查；真实检查通过：仍需核对验收条件及必要的用户体验反馈。
- `accept`：要求新鲜的成功检查；`requires_user_acceptance` 为 true 时，只有用户实际确认后才加 `--user-confirmed`。
- 没有可执行检查：首版不能验收，说明未覆盖内容并记录阻塞。人工口头通过不能替代这一技术要求。
- 指纹覆盖的项目文件改变会使旧证据失效，需要重新检查；排除的敏感配置、外部数据库和服务变化无法自动识别，需告知主会话重新核验。100% 仅表示本版约定范围已验收，与保存、上线分开说明。

进度数据位于项目 `.dev-companion/`，只通过 CLI 修改。静态看板建议输出到 `.dev-companion/board.html`；不能用上次打开的 HTML 推断当前进度。不要直接编辑状态 JSON 来解除检查、冲突或确认要求。

专用的 `.dev-companion/board.html` 可以安全替换刷新；其他已存在的输出文件不会覆盖。正常操作返回 0；参数、状态或权限错误返回 2；真实检查未通过返回 3 并输出具体结果。恢复中断时核心派发、检查和存档都暂停，只读状态保留保护存档信息。

## 本地快照

只保存 `--paths` 显式纳入的普通文件。后续保存会累计纳管范围；恢复以预览列出的集合为准，可能删除较晚才纳管而目标快照中没有的文件。未纳管内容不受恢复影响。

恢复前必须停止开发者、测试进程和其它写入者；CLI 会拒绝仍在执行的已登记任务，但不能替你终止编辑器或其它外部写入。展示预览中的新增、替换、删除和未覆盖范围，得到用户对该次具体预览的确认后，使用其令牌恢复。脚本先制作并验证保护快照；失败则不能声称恢复成功。

敏感文件、符号链接和目录不能直接纳入；该保护依赖已知规则，无法自动识别普通源码里的所有秘密。只选择已检查过的项目文件。快照不保存 Git 历史、数据库、线上服务或外部资源，本机损坏仍可能丢失本地快照。撤回一次恢复也要先对保护快照生成新预览，再确认恢复。
