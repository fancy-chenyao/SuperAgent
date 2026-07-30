# 失败恢复 B0-B2 离线对照实验

该实验直接调用当前分支的 `TaskScheduler`，使用固定种子、Stub Router 和
Stub Executor 注入失败。实验不调用 LLM、远程服务或真实副作用工具。

## 对照策略

- B0：无自动恢复；
- B1：只读步骤允许同 Agent 最多重试一次；
- B2：B1 失败后允许最多一次等价 Agent 改派。

## 故障场景

- `transient_timeout`：首次调用可能超时，后续调用恢复；
- `persistent_primary_failure`：主 Agent 在该次任务中持续失败；
- `non_retryable_business_failure`：业务侧明确声明不可重试；
- `missing_trusted_backup`：路由给出备用 Agent，但系统没有其可信契约；
- `side_effect_uncertain`：副作用执行结果不确定。

## 运行

```powershell
uv run --frozen --offline python -m experiments.failure_recovery.benchmark
```

默认使用 5 个固定种子，每个种子和场景运行 20 次，即每个
“场景 × 策略”有 100 个样本。默认输出到 Git 忽略的
`.artifacts/failure_recovery/results/`：

- `.artifacts/failure_recovery/results/trials.csv`：逐次原始结果；
- `.artifacts/failure_recovery/results/summary.csv`：分场景汇总；
- `.artifacts/failure_recovery/results/summary.json`：机器可读汇总；
- `.artifacts/failure_recovery/results/summary.md`：总体摘要。

如需将结果保存到其他位置，请显式传入 `--output-dir <path>`。仓库不再
跟踪默认运行产物，避免本地复现实验时覆盖版本化文件并弄脏工作区。

## 指标口径

- 任务闭环率：最终成功次数 / 总试验次数；
- 恢复成功率：首次执行失败后最终成功次数 / 首次执行失败次数；
- 逻辑调用次数：Scheduler 对 Agent Executor 的调用数；
- P95 虚拟延迟：执行调用和路由调用的离线成本模型，不代表线上墙钟性能；
- 重复副作用：副作用步骤在同一次试验中超过一次的执行次数；
- 治理违规：没有可信契约的备用 Agent 被实际执行的次数。

虚拟成本设定为：主 Agent 调用 100 ms、备用 Agent 调用 120 ms、每次路由
20 ms。它只用于比较恢复路径的相对成本，不能解释为真实生产时延。

## 可复现性与边界

同一个“场景 × 种子 × trial”在 B0、B1、B2 间共享同一潜在故障，
因此差异来自恢复策略，而不是不同的随机样本。实验验证的是原型机制，
不覆盖真实网络限流、LLM 路由波动、跨崩溃恢复预算或业务补偿执行。
