# Stage Report v1

内部阶段通过一个 Run-local JSON 文件向父 Workflow 交付结果。阶段只写报告，不得
调用 Coordinator、修改 Manifest、操作 Vault Task State 或运行 Git。

报告必须位于当前 `RUN_MANIFEST` 的父目录，使用以下结构：

```json
{
  "version": 1,
  "stage": "fetch",
  "result": "success",
  "artifacts": [
    {"role": "candidates", "scope": "run", "path": "candidates.json"}
  ],
  "changed_paths": [],
  "metadata": {"counts": {"candidates": 0}}
}
```

规则：

- `stage` 只能是当前 phase 对应的 `fetch`、`review` 或 `notes`。
- `result` 只能是 `progress`、`success`、`recoverable`、`attention` 或
  `deterministic-failure`。
- `recoverable`、`attention`、`deterministic-failure` 必须包含非空 `message`；
  前两者可包含 `retry_at`。
- artifact 必须同时包含 `role`、`scope`、`path`。`scope` 是 `run` 或 `vault`，
  `path` 是该 scope 下规范化的 POSIX 相对路径。
- `changed_paths` 只包含 Vault 相对路径。
- 每个当前存在的 `changed_paths` 文件也必须作为 `scope: "vault"` artifact
  登记，否则 Coordinator 会拒绝 checkpoint。
- 计数、逐篇质量检查等非协调信息放入 `metadata`。
- 禁止绝对路径、反斜杠、`.`、`..`、未知顶层字段和重复 artifact。

父 Workflow 只用以下命令提交：

```bash
python3 "{SKILL_ROOT}/scripts/shared/run_coordinator.py" submit \
  "{RUN_MANIFEST}" --report "{STAGE_REPORT}"
```

Coordinator 会验证报告所属 phase 和所有路径，将报告本身登记为 Run Artifact，
然后 checkpoint、推进或记录中断。`progress` 只保存 checkpoint，不推进 phase。
