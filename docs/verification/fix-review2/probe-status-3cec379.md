# 三项收口探针状态与证据映射（2026-09-21，候选 3cec37937a42005630f837a206fa259759aaf636）

按复核者纪律（03 指令）：探针异常/超时/不再走原代码只能标 HARNESS_ERROR，不能当拒绝证据；
收口证据 = 转换后的负向断言 + 正向对照。三支探针在本候选上均 HARNESS_ERROR，逐支状态与
证据映射如下。原始探针日志保留于本目录（不改写）。

## probe_concurrency（FIX02-followup 域）

- 状态：**HARNESS_ERROR**。崩溃点 `assert created.wait(5)`——探针在 O_CREAT|O_EXCL 返回后
  注入时序；v2 锁改为 os.link 原子发布拥有者记录，该注入点不存在。
- 产品证据（转换后断言，tests/recovery/test_lock_v2.py 10 项）：
  - 双真实进程临界区串行化：共享计数递增 max_concurrent==1、B.entered≥A.exited；
  - 合法竞争者顺序进展（无死锁无饥饿）；
  - 迟到/异 token 持有者被**产品错误码**拒绝（CompanionError，非崩溃）；
  - 持锁 os._exit → 陈旧锁接管（崩溃恢复）；
  - 空/半写锁文件：不当死锁证明，1s 宽限后接管 + stderr 警告可追。
- 证据分类纠正：resume-race-final-548f03a.json 的双 BrokenBarrierError = 旧探针屏障超时
  （HARNESS_ERROR），非"产品拒绝陈旧身份"——原文已保留，另见
  resume-probe-reclassification-2026-09-21.md。

## probe_boundaries（FIX04-followup 域）

- 状态：**HARNESS_ERROR**。崩溃点 `from agents_kernel.digest import digest` ModuleNotFoundError
  ——探针的 sys.path 假设面向解包候选布局，与活仓库布局不匹配（非产品行为）。
- 产品证据（tests/closure_store/test_fix04_gates.py 16 项，覆盖复核者 7 步场景全组）：
  - 等长同 mtime 内容漂移（VALUE=1→2）→ done exit 2，指出漂移文件与新旧哈希；
  - 越界 changed_files（only.py 任务报 outside.py）→ report exit 2；
  - integrate 前漂移 → 拒绝 completed（不落事件）；
  - feature_level 仍如实 unknown（不虚标已验证）；
  - 正向对照：合法新版本重报→旧批准自动失效→重 approve→done→integrate completed，
    manifest 记录 regressed_on 新 sha 集合。

## probe_bridge（FIX05-followup 域）

- 状态：**HARNESS_ERROR（场景结构性消失）**。崩溃点读 `checkpoints/g2-progress.json`
  FileNotFoundError——单体检查点已被 per-chunk marker（run_root/g2/<gen>/<id>.done）+O(1)
  meta 取代；"写出自己读不回的文件"在该设计下不再可能（init O(1)、read 有界、标记即真相）。
- 产品证据（tests/test_precheck_index.py 44 项）：
  - 双进程对不同 chunk mark → completed 恰 2 条（并发成功数==恢复项数）；同 id 幂等；
  - 12,000 块 init/read 全有界（read 回执 471B）；
  - chunks-page 每次加载 ≤page_size（loaded_entries 可证）；
  - A1/argv 不携带全路径（/repos/app/file-* 断言）；
  - 续接闭合 3+2=5（中断前结果+新结果聚合无重无漏）；anchor 变化三处拒绝。

## 结论口径

三组收口的拒绝/正确性证据以转换后测试套件为准（fixtures 全部真实进程/真实文件系统，
正负对照成对）；探针原脚本与日志按 HARNESS_ERROR 归档。此口径与复核者 03 指令一致。
