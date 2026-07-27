# astrbot_plugin_kook_name_beautify

AstrBot 的 KOOK AI 频道名称美化插件。管理员可以直接用自然语言描述想要的风格，插件读取当前服务器的分组、文字频道和语音频道，调用 AstrBot 当前 LLM 生成统一改名方案。

插件不会让 AI 直接批量修改频道。每次先返回完整预览，只有管理员再次发送带方案编号的确认命令后才会调用 KOOK API；执行后还可以撤销。

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
```

AI 会调用 `kook_beautify_channels` 工具，返回类似：

```text
KOOK 频道美化预览（方案 a1b2c3d4，共 4 项）

1. [分组] 社区交流  ->  『 COMMUNITY 』
2. [文字] 闲聊大厅  ->  💬・闲聊大厅
3. [文字] 截图分享  ->  📷・截图分享
4. [语音] 组队开黑  ->  🎧・组队开黑

确认执行：/kook美化确认 a1b2c3d4
```

必须由管理员手动发送确认命令。AI 工具本身没有直接执行入口。

## 命令

```text
/kook美化 <自然语言要求>
/kook美化确认 <方案编号>
/kook美化撤销 <方案编号>
/kook频道列表
/kook美化帮助
```

所有命令都同时限制为 KOOK 平台和 AstrBot 管理员。若配置了 `allowed_user_ids`，还必须在额外白名单中。

## 安全行为

- AI 只能引用 KOOK API 实际返回的频道 ID。
- 空名称、控制字符、超长名称、重复频道、重名和超量变更会在执行前拒绝。
- 确认时会重新读取频道；原名称有变化就取消整批执行，避免覆盖他人的新修改。
- 批量改名串行执行，并遵守 KOOK 的 `429` 与 `X-Rate-Limit-Reset`。
- 中途失败会尽量把本次已修改的频道恢复为原名称。
- 撤销前也会检查当前名称，发现后续人工修改时不会强制覆盖。
- Token 不会写入日志或方案。

待确认方案默认 10 分钟过期。方案和撤销记录保存在内存中，AstrBot 重启后需要重新生成方案。

## 当前范围

当前版本只修改现有频道和分组的名称，不创建、删除、移动频道，也不修改权限、密码或慢速模式。这是刻意限制：频道结构和权限变更的影响更大，后续应使用独立的结构方案和二次确认流程实现。

## KOOK API 依据

- [HTTP 接口规范与 Bot 鉴权](https://developer.kookapp.cn/doc/reference)
- [频道列表与编辑频道](https://developer.kookapp.cn/doc/http/channel)
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
