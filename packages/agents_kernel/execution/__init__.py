"""执行层（stdlib only, Py3.9+）：

- budget：活动预算与有界消费（P3-03）。
- lease：任务租约——文件锁 + TTL + epoch 接管（P5-02）。
- idempotency：回报幂等去重——同键同内容去重、同键异内容拒绝（P5-02）。
- backoff：有限重试退避与预算领取门（P5-02）。
- isolation：工作区隔离——worktree/受控副本、变更检查、在册清理与派发门（P5-03）。
- backpressure：并发背压闸门——有界 permit、FIFO 排队、gated_run（P6-03）。
"""
