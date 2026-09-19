# 生命周期输入示例

以下是输入结构示例，所有用户决定、演示与检查描述都必须替换为真实事实。示例文字不证明本轮发生过用户确认或原型走查；主会话负责生成输入，新手无需手写。

统一调用形式：`python3 "<插件根>/scripts/companion.py" --project "<项目绝对路径>" plan --stage STAGE --input PATH --revision N`。首次规划 N 为 0，此后从 planning-status 的 record.revision 读取。材料仍不完整时不加 --complete；要完成阶段时必须前序有效、必要字段完整、open_questions 为空。product 还需实际确认后加 --user-confirmed。

## 可先保存半句话

```json
{"summary":"想做一个记账工具，使用设备还不确定","details":{},"open_questions":["只在电脑使用还是需要手机？"]}
```

草案允许 details、scope 和 interfaces 只保留已知内容，不为了通过格式校验猜技术方案。尚未解决的问题保留在 open_questions；已有决定不要重复提问。完整阶段的 details 字段都是字符串，不是数组。

下面六份 JSON 独立保存。每阶段成功后读取新规划 revision 再执行下一阶段；产品字段在 product 与 technical 中必须一致。

## concept

```json
{
  "summary": "做一个在本机计算支出合计的小工具。",
  "details": {
    "audience": "只给自己使用",
    "problem": "手工相加容易算错",
    "scenario": "在本机输入一组整数支出金额",
    "outcome": "立即得到正确合计，无效金额给出明确原因"
  },
  "open_questions": [],
  "decisions": [
    {
      "question": "首版金额精度？",
      "answer": "用户确定首版只处理整数元。",
      "source": "user"
    }
  ],
  "artifacts": []
}
```

## requirements

```json
{
  "summary": "先做整数支出合计，保持本地使用。",
  "details": {
    "constraints": "首版运行于已有 Python 3.9+ 环境，数据只在本地处理，不新增账号或付费服务。",
    "priorities": "先完成正确合计与输入错误反馈；跨设备和云同步不纳入首版。"
  },
  "open_questions": [],
  "decisions": [
    {
      "question": "是否需要云同步？",
      "answer": "用户确定首版不需要。",
      "source": "user"
    },
    {
      "question": "是否添加数据库？",
      "answer": "建议首版不保存历史记录，避免增加部署需求。",
      "source": "recommendation"
    }
  ],
  "artifacts": []
}
```

## product

```json
{
  "summary": "首版提供合计与输入错误反馈。",
  "details": {
    "positioning": "个人使用的本地支出合计工具"
  },
  "open_questions": [],
  "decisions": [],
  "artifacts": [],
  "scope": {
    "title": "个人记账工具",
    "goal": "快速汇总一组日常支出",
    "audience": "只给自己使用",
    "scenario": "在本机输入支出金额并查看合计",
    "out_of_scope": [
      "账号和云同步"
    ],
    "assumptions": [],
    "features": [
      {
        "id": "sum-expenses",
        "title": "计算支出合计",
        "acceptance_criteria": [
          "20 元和 30 元合计为 50 元",
          "负数金额应被拒绝"
        ],
        "requires_user_acceptance": true
      }
    ]
  }
}
```

## flow

```json
{
  "summary": "输入支出列表，验证每项金额，显示合计或具体输入错误。",
  "details": {
    "main_path": "sum-expenses：打开本地模块操作示例，输入 [20, 30]，调用 total_expenses，查看结果 50。",
    "alternatives": "空列表得到 0；负数被拒绝；非整数输入被拒绝；修正输入后重新计算。",
    "data_changes": "读取本次输入列表并计算结果，不持久化数据，也不修改原列表。"
  },
  "open_questions": [],
  "decisions": [],
  "artifacts": [],
  "feature_ids": [
    "sum-expenses"
  ]
}
```

## prototype

先创建并实际演示 design/module-walkthrough.md 指向的模块操作样例，再据实填写 walkthrough。图形界面项目应改为真实可打开原型及操作记录。artifact 必须是项目内已存在的普通文件，不能放在 .dev-companion 内；只有计划而没有产物时保持草案。

```json
{
  "summary": "使用可核对的模块操作样例代替页面原型。",
  "details": {
    "screens": "无图形界面。design/module-walkthrough.md 包含 Python 交互调用样例、输入和输出；纯本地模块适用此形式。",
    "states": "正常合计、空列表、负数异常与类型异常。",
    "walkthrough": "实际演示后记录使用的文件版本、命令和输出：total_expenses([20, 30]) 为 50；负数抛出 ValueError。此记录区分代理演示与用户试用，用户尚未确认体验。"
  },
  "open_questions": [],
  "decisions": [],
  "artifacts": [
    "design/module-walkthrough.md"
  ],
  "feature_ids": [
    "sum-expenses"
  ]
}
```

## technical

test_integration.py 需实际导入或启动模块，检查真实调用结果、错误路径和不改变输入等接口约定；此文件不是插件自动提供的占位测试。HTTP 项目将 kind 改为 http，并给出真实请求/响应/错误与启动检查方式，不用 mock 结果声称真实联调通过。

```json
{
  "summary": "使用 Python 标准库实现并检查真实模块调用。",
  "details": {
    "architecture": "单个 ledger.py 模块；功能检查与模块集成检查分开记录。",
    "data_model": "输入为整数金额列表，返回整数合计；负数抛出 ValueError，非整数抛出 TypeError。",
    "release_target": "先分发本机 dist/ledger-v1 目录，按本地目标验证；正式上线另定目标。"
  },
  "open_questions": [],
  "decisions": [],
  "artifacts": [],
  "scope": {
    "title": "个人记账工具",
    "goal": "快速汇总一组日常支出",
    "audience": "只给自己使用",
    "scenario": "在本机输入支出金额并查看合计",
    "out_of_scope": [
      "账号和云同步"
    ],
    "assumptions": [],
    "features": [
      {
        "id": "sum-expenses",
        "title": "计算支出合计",
        "acceptance_criteria": [
          "20 元和 30 元合计为 50 元",
          "负数金额应被拒绝"
        ],
        "requires_user_acceptance": true,
        "allowed_paths": [
          "ledger.py",
          "test_ledger.py",
          "test_integration.py"
        ],
        "check_commands": [
          [
            "python3",
            "-m",
            "unittest",
            "test_ledger.py"
          ]
        ]
      }
    ]
  },
  "interfaces": [
    {
      "feature_id": "sum-expenses",
      "kind": "local",
      "contract": "ledger.total_expenses(amounts: list[int]) -> int；20、30 得到 50；负数 ValueError；非整数 TypeError；不改变输入列表。",
      "check_commands": [
        [
          "python3",
          "-m",
          "unittest",
          "test_integration.py"
        ]
      ]
    }
  ]
}
```

## 从技术方案进入开发

六阶段完成后，主会话把 technical.scope 原样提取为输入文件，交给 init；已有 state 用 scope 并传 state 的最新 revision。随后按当前范围已有授权 confirm，再 packet、实现、receipt、独立 feature 检查与 integration 检查。需要用户体验确认的功能得到真实反馈后才 accept。

规划 revision、state revision 和 release revision 独立，不把某一个数字复制到另一个命令。开发者回报格式见 [CLI 契约](cli-contract.md)。

## 本地发布输入

以下示例将已验收 ledger.py 复制到新的本地 dist/ledger-v1 目录，并验证分发副本及版本标识。不是正式网站部署；只有项目确实采用此方式且已获相应命令授权时使用。目标已存在会拒绝创建，应先核对现有目标，再准备新版本目标，不能盲目覆盖。

部署和验证使用标准库，验证从分发目录实际导入代码而不是继续导入源码。dist 属于构建输出目录，不纳入普通源码指纹，因此验证命令必须核对其中的实际内容。此示例没有自动回退命令，不能执行 release-run rollback。

```json
{
  "version": "0.1.0",
  "target": "本机 dist/ledger-v1 分发目录",
  "environment": "local",
  "summary": "分发已验收的整数支出合计模块。",
  "rollback_plan": "本次发布到新目录，旧分发目录保留；需要回退时先取得明确授权，再将使用入口切回已验证旧目录。本例不自动执行回退。",
  "verification_notes": "从分发目录实际加载 ledger.py，确认 VERSION 是 0.1.0，并验证合计与负数拒绝。只有这些检查通过才记录本地验证成功。",
  "deploy_commands": [
    [
      "python3",
      "-c",
      "from pathlib import Path; import shutil; p=Path('dist/ledger-v1'); p.mkdir(parents=True, exist_ok=False); shutil.copyfile('ledger.py', p/'ledger.py'); (p/'VERSION').write_text('0.1.0', encoding='utf-8')"
    ]
  ],
  "verify_commands": [
    [
      "python3",
      "-c",
      "from pathlib import Path; import importlib.util; p=Path('dist/ledger-v1'); assert (p/'VERSION').read_text(encoding='utf-8') == '0.1.0'; spec=importlib.util.spec_from_file_location('released_ledger', p/'ledger.py'); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); assert module.total_expenses([20, 30]) == 50; assert module.total_expenses([]) == 0; print('deployed module version and sum verified')"
    ],
    [
      "python3",
      "-c",
      "import importlib.util; spec=importlib.util.spec_from_file_location('released_ledger', 'dist/ledger-v1/ledger.py'); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\ntry:\n    module.total_expenses([-1])\nexcept ValueError:\n    print('deployed module rejects negative input')\nelse:\n    raise AssertionError('negative input was accepted')"
    ]
  ],
  "rollback_commands": []
}
```

先 release-status，首次 `release-prepare --input PATH --revision 0`；以返回的发布版本执行已授权的 deploy，成功后再次读取发布版本，再执行已授权的 verify。local 只得到 local_verified，不能称正式上线。

正式环境应使用项目已有实际部署命令，verification_notes 明确部署地址、实际版本与关键业务流程，verify_commands 真实检查该目标。完整命令与配置会进入发布记录；秘密仅通过环境或既有安全配置传入，命令不得输出秘密。

如果进程中断，先检查外部目标与相关进程。确认写入已停止后，陈述实际核查证据，使用 release-reconcile 记录 interrupted；它不证明成功。若同时留下写锁，只有核实锁对应 PID 已不存在，才使用 recover-lock。然后再准备新计划或按具体授权回退，不能直接改 JSON 或盲目重放。
