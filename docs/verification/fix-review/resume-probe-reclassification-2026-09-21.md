# resume-race-final-548f03a.json 证据分类纠正（HARNESS_ERROR）

日期：2026-09-21（FIX02-followup 收口轮）
对象：`docs/verification/fix-review/resume-race-final-548f03a.json`（原样保留，本文不改其任何字节）
同源副本：复核包 `evidence/15_sender_original_resume_probe_548f03a.json`（内容一致的复核者留档）

## 结论

该文件中两个 worker 的失败**不是产品拒绝证据**，应分类为 **HARNESS_ERROR（旧探针调度不适用）**。此纠正与独立复核报告（bf974a97 recheck）§5"特别纠正上传证据"一致。

## 事实

1. 两条失败记录的异常均为 `threading.BrokenBarrierError`，栈帧位于旧探针脚本
   `reproduce_resume_race.py` 的 `scheduled_read → read_barrier.wait(timeout=10)`——
   即探针自己注入到 `ResumeLedger.open` 互斥临界区内的读屏障，不是
   `agents_kernel` 抛出的任何产品错误（产品侧拒绝路径抛 `CompanionError`）。
2. 该屏障设计为 2 方在 `_read` 处会合。台账打开本就处于 run_root 级互斥临界区内：
   互斥一旦生效，第二个参与者必然被挡在临界区外，屏障（parties=2）**结构性无法
   会合**，只能以超时破裂收场。因此这是探针调度机制与（正确的）互斥行为不兼容，
   属脚手架失效。
3. 两位写者均未完成 `open`，seed 台账保持 epoch=1、activities 为空。故
   `final_epoch=1`、`both_acquired_same_epoch=false` 的含义是"本次实验未完成写入"，
   而 `successful_A_activity_lost=true` 的前提（存在成功的 A）并不成立——该字段
   不能被表述为"两个陈旧写者被安全拒绝"，也不能被表述为"活动丢失"。

## 历史表述错误

此前的收口材料曾把该文件表述为"两个陈旧写者被产品安全拒绝"。该表述撤销：
seed 未变只证明这次实验未发生写入，不构成任何方向的产品断言。

## 对 FIX02 判定的影响

不改变缺陷判定。互斥缺陷（空文件窗口）由复核者的 `probe_concurrency.py` 以
os.open 时序注入独立复现（`evidence/09_concurrency_system.json` 的
`empty_lock_creation_window.mutual_exclusion_broken=true`），与本文件无关。
本轮修复后，该探针场景由 `tests/recovery/test_lock_v2.py`
（`ProbeScenarioTest.test_paused_holder_blocks_contender_no_double_entry`）
以真实双进程转正覆盖；旧屏障探针不再使用。
