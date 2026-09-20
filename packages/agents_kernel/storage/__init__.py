"""SQLite 事实库（Z13 核心，07_STORAGE_MIGRATION_AND_COMPAT §1/§2）。

db：建库/schema 版本化迁移/单协调写者（文件锁 + epoch）。
events：事件追加（append-only、幂等键、单调 seq）与 CAS 引用更新，写路径单事务。
"""
