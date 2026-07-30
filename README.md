# astrbot_plugin_kook_name_beautify

AstrBot 的 KOOK AI 频道结构美化插件。管理员可以直接用自然语言描述服务器布局和风格，插件读取当前分组、文字频道和语音频道，调用 AstrBot 当前 LLM 生成可一键应用的完整结构方案。

插件不会让 AI 未经确认直接修改频道。每次先返回完整预览，只有管理员再次发送带方案编号的确认命令后，才会批量创建分组、文字频道、语音频道并改名现有频道；执行后还可以撤销。

## 安装

在 AstrBot WebUI 添加插件源：

```text
https://raw.githubusercontent.com/xzw38/astrbot_plugin_kook_name_beautify/master/plugins.json
```

手动安装时，将本仓库放到：

```text
data/plugins/astrbot_plugin_kook_name_beautify
```

依赖通常由 AstrBot 自动安装。需要手动安装时执行：

```bash
pip install -r data/plugins/astrbot_plugin_kook_name_beautify/requirements.txt
```

## 前置条件

1. AstrBot 已接入 KOOK，并配置可用的文本 LLM Provider。
2. KOOK 机器人已加入目标服务器。
3. 机器人角色拥有查看频道和管理频道的权限。
4. 使用者已加入 AstrBot 的管理员列表。

插件优先复用 AstrBot KOOK 适配器中的 Token，并从当前消息识别服务器 ID。若自动识别失败，在插件设置中填写 `bot_token` 和 `guild_id`。`bot_token` 只填 Token 本体，不带 `Bot ` 前缀。

## 自然语言使用

在 KOOK 中直接对 AI 说：

```text
把这个服务器的频道统一成简约高级黑白风，分类用英文大标题，普通频道保留中文语义
把频道整理成二次元社团风，Emoji 要统一，不要改“管理后台”
给所有游戏频道用电竞风，语音频道用同一套分隔符
帮我设计一套完整的二次元社团结构，要有公告、闲聊、作品分享和三个语音房，一键应用
```

AI 会调用 `kook_beautify_channels` 工具，返回类似：

```text
KOOK 频道结构预览（方案 a1b2c3d4，共 6 项）

1. [新建分组] 『 COMMUNITY 』
2. [新建文字] 📢・社区公告，归属 community
3. [新建文字] 💬・闲聊大厅，归属 community
4. [新建语音] 🎧・组队开黑，归属 community，人数 25，音质 2
5. [改名文字] 截图分享  ->  📷・作品分享
6. [改名语音] 大厅  ->  🎙・语音大厅

确认执行：/kook美化确认 a1b2c3d4
```

管理员可以手动发送确认命令，也可以直接回复：

```text
确认执行方案 a1b2c3d4
```

此时 AI 会调用 `kook_apply_beautify_plan`。执行工具同时检查当前原始消息中的确认文字、相同方案编号、`confirm=true`、KOOK 平台和 AstrBot 管理员身份，不能根据历史消息或模型自己的判断代替用户确认。

生成方案时会过滤 KOOK 子频道上残留的已删除分组 ID。如果 AI 仍偶尔引用刚失效的频道 ID，插件还会重新读取最新频道列表并自动规划一次。因此手动删除旧分组后，可以直接重新说“按当前结构全部重新设计”，不需要恢复已删除分组。
当要求中明确包含“新建、创建、新增频道”等文字时，插件会强制检查 creates 非空；AI 返回空方案时会带着校验原因重新生成。debug_logging 开启时，日志中的 AI plan output 会显示模型实际返回的 JSON，方便继续排查模型兼容性。
如果管理员在原话中明确提供了数字父分组 ID，插件允许 AI 把它写入 parent_ref，即使 KOOK 频道列表暂时没有返回该分组。该 ID 会在确认执行时由 KOOK channel/create 接口最终校验；若确实失效，方案会安全失败且不会修改其他频道。

## 命令

```text
/kook美化 <自然语言要求>
/kook美化确认 <方案编号>
/kook美化撤销 <方案编号>
/kook_beautify_confirm <方案编号>
/kook_beautify_rollback <方案编号>
/kook频道列表
/kook美化帮助
```

所有命令都同时限制为 KOOK 平台和 AstrBot 管理员。若配置了 `allowed_user_ids`，还必须在额外白名单中。

## 完整结构永久替换

管理员可以说“生成一套赛博朋克新模板，之前的旧频道都不要，用新模板替换”。插件会在同一预览中列出新建、改名和永久删除项目，并自动保护当前机器人回复频道。确认时必须使用 /kook替换确认 <方案编号> 或回复“确认永久替换方案 <编号>”。

执行顺序固定为先创建新模板、再改名复用频道、最后删除旧子频道和旧分组。进入删除阶段后无法恢复旧消息和权限，因此普通美化确认不能执行该方案，且永久替换完成后不支持撤销。

## 永久删除频道

管理员可以直接说“删除频道 完整名称”或提供频道 ID。AI 会调用 kook_plan_channel_deletion 生成只包含一个目标的预览；必须再次发送 /kook删除确认 <方案编号> 或明确回复“确认永久删除方案 <编号>”才会调用删除接口。 只有 AstrBot 管理员可以生成或执行删除方案；配置 allowed_user_ids 后，还必须同时位于额外白名单中。

删除方案不能和创建、改名混合，普通 /kook美化确认 无法执行删除。非空分组不能删除。频道删除后，历史消息、频道权限及内容无法由插件撤销恢复。

## 安全行为

- 改名只能引用 KOOK API 实际返回的频道 ID；新建子频道只能归属现有分组或同方案新建分组。
- 空名称、控制字符、超长名称、重复临时编号、错误频道类型、非法语音参数、重名和超量操作会在执行前拒绝。
- 确认时会重新读取频道；原名称有变化就取消整批执行，避免覆盖他人的新修改。
- 执行时先创建分组，再创建文字/语音频道，最后改名现有频道，并遵守 KOOK 限流规则。
- 中途失败会恢复本次改名，并按反序删除本次已创建的频道。
- 撤销只删除本方案创建的频道，不删除任何原有频道；同时恢复原频道名称。
- 若本方案创建的频道已被人工改名/删除，或新建分组中混入了后来人工创建的子频道，撤销会拒绝执行，避免误删。
- Token 不会写入日志或方案。

待确认方案默认 10 分钟过期。预览生成时会保存现有频道名称，应用时会记录本次新建频道的真实 ID，用于之后撤销。方案和撤销记录保存在内存中，AstrBot 重启后需要重新生成方案；插件无法恢复管理员在插件之外手动删除的频道。

## 当前范围

当前版本支持创建分组、文字频道和语音频道、改名现有频道、独立永久删除单个频道，以及管理员确认后的完整结构永久替换。完整替换会把受保护的当前操作频道迁入新分组；除此之外不会移动现有频道，也不修改权限、密码或慢速模式。非删除方案可在内存记录仍存在期间撤销；任何已永久删除的频道、消息和权限均无法恢复。

完整替换和选择性替换都支持按类型保护范围，不依赖固定提示词。例如“不动文字，其他全部替换”或“替换所有语音，不替换文字”会保护文字频道及其父分组；“不碰语音，其他全部替换”会保护语音频道及其父分组；“分组保持原样，其他全部替换”会复用现有分组。受保护类型禁止改名、移动、删除和新建，其他类型仍按要求替换。“保留”“不改”“保持原样”“不替换”“除了语音其他替换”等同义表达也会使用相同的本地强制保护。AI 偶尔误列受保护项目时，插件会直接过滤这些项目，不会仅因此整份重新规划。

## 调试改名失败

`debug_logging` 默认开启。确认方案后，AstrBot 日志会依次出现：

```text
[KOOK Beautify] apply start ...
[KOOK Beautify] create start ...
[KOOK API] request method=POST path=/channel/create ...
[KOOK API] response path=/channel/create http=...
[KOOK API] payload path=/channel/create code=... message=...
```

日志不会输出 Bot Token 或 Authorization。排查时重点查看第一条 `apply failed` 及它前面的 `http`、`code`、`message`：

- 能读取但创建或更新返回权限错误：检查机器人在目标服务器的角色是否具有管理频道权限。
- 返回 `429`：插件会按 `X-Rate-Limit-Reset` 自动等待并重试。
- 返回频道不存在或执行冲突：重新生成方案，避免使用频道已变化的旧方案。
- 无法识别服务器：在插件配置中填写 `guild_id`。

## KOOK API 依据

- [HTTP 接口规范与 Bot 鉴权](https://developer.kookapp.cn/doc/reference)
- [频道列表、创建、编辑与删除频道](https://developer.kookapp.cn/doc/http/channel)
- [Rate Limit](https://developer.kookapp.cn/doc/rate-limit)

## 开发与市场文件同步

运行测试：

```bash
python -m unittest discover -s tests -v
```

`metadata.yaml` 是市场元数据来源。修改版本、名称、说明或仓库地址后执行：

```bash
python tools/sync_marketplace.py
```

只检查是否同步：

```bash
python tools/sync_marketplace.py --check
```

测试和 GitHub Actions 都会执行同步检查；`metadata.yaml`、`plugins.json` 和 `main.py` 的版本不一致时会直接失败。
