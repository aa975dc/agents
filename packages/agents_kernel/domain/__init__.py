"""领域语义层：事件类型与当前视图（从事件折叠出的物化状态表）。

views 同时承担两件事：写事务内的视图折叠（由 storage.events 调用）与
面向展示/迁移的只读分页查询。本包不 import storage（storage 单向依赖本包）。
"""
