"""交接契约层：设计/接口产物与联审记录的最小 schema（从 events 折叠语义独立出来）。

schemas 是唯一机器防线——references/design-handoff.md 与 references/api-contract.md
的结构约定在这里落成可执行校验；tests/agents/test_handoff_contracts.py 拿
tests/fixtures/design/ 真实文件跑这些校验器，样例、文档、校验器三方漂移即失败。
"""
