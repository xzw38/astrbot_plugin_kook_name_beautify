# Changelog

## 0.1.3 - 2026-07-28

- 新增 `kook_apply_beautify_plan` LLM 工具，使明确确认可通过 AI 工具执行，不再完全依赖斜杠命令路由。
- 新增 `kook_rollback_beautify_plan` LLM 撤销工具。
- 执行和撤销工具同时校验 `confirm=true`、当前原始消息中的动作文字及相同方案编号。
- 新增 `/kook_beautify_confirm` 和 `/kook_beautify_rollback` 英文备用命令。

## 0.1.2 - 2026-07-28

- 增加可配置的 KOOK API、改名进度、冲突检查和回滚调试日志。
- 执行或撤销失败时保留 KOOK API 的原始错误码与错误消息。
- 调试日志不会输出 Bot Token 或 Authorization。

## 0.1.1 - 2026-07-28

- 修复 AstrBot 以插件包路径加载时无法导入 `beautify` 和 `kook_api` 的问题。

## 0.1.0 - 2026-07-28

- 首个版本。
- 支持 AstrBot LLM 工具和自然语言频道名称规划。
- 支持方案预览、管理员确认、执行冲突检查和撤销。
- 支持 KOOK API 分页、限流重试和批量失败恢复。
- 增加插件市场元数据同步脚本与自动测试。
