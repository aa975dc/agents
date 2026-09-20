"""索引层：流式普查落库（scanner）与只读分页查询（reader），stdlib only。

generation 语义（03_CAPACITY_AND_INDEXING.md §6，见 scanner 模块文档）：
已发布快照（files 表）恒等于最近一次 complete 世代；进行中的扫描写独立 staging，
完成后单事务原子换装，绝不把不同 generation 拼成"完整当前结果"。
"""
