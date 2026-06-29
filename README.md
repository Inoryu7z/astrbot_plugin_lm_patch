[![LMPatch Counter](https://count.getloli.com/get/@Inoryu7z.lm_patch?theme=miku)](https://github.com/Inoryu7z/astrbot_plugin_lm_patch)

# 🧬 记忆演化 · LivingMemory Patch

给 LivingMemory 装上"人设演化"与"记忆压缩"两条暗线，让记忆不止于存储，还能反哺人设、自我精简。

**记忆演化** 是一个补丁插件，本身不存储聊天记忆，也不直接监听消息事件。它通过 `weakref` 接入已加载的 LivingMemory 插件实例，周期性地读取其累积的记忆，做两件事：

1. **人设补丁** — 由 LLM 判断新增记忆是否揭示了需要更新人设的事实（毕业、搬家、关系变化等），如果是，则在 WebUI 中提交一个**并排对比**的人设变更提案，由人工审批后再写回 `PersonaManager`。每次写回前自动保存人设快照，支持任意历史版本回滚。
2. **记忆压缩** — 当低重要性记忆累积到一定条数时，由 LLM 归纳成更少的摘要记忆，重新评估重要性，**全自动执行**（删旧加新），无需审批。

> 设计哲学：**破坏性操作（改人设）必须有人类签字；非破坏性操作（合并已被遗忘的边角料）放手让机器做。**

---

## ✨ 它能做什么

### 🧬 人设补丁

本插件会按设定间隔读取 LivingMemory 新增记忆，让 LLM 判断是否需要更新人设。

人设补丁的工作方式：
- 每个 persona 维护一个 checkpoint，下次只读上次处理后的新记忆
- LLM 判断新增记忆是否揭示了需要更新人设的事实
- 如果需要变更，自动生成一个待审提案

人设变更的判断标准：
- 揭示了与人设当前描述**矛盾**的事实（例如人设写"在读大三"，但记忆显示已毕业）
- 揭示了**重大状态变迁**（毕业、搬家、换工作、关系变化等）
- 揭示了人设中**缺失但重要**的持久信息（例如反复出现的某个重要习惯）

不会因为以下原因提议变更：
- 纯粹的闲聊或临时事件
- 记忆中提到的临时情绪或短暂状态
- 没有持久意义的信息

### ✅ 并排审批

WebUI 中并排展示原始人设与提议人设，diff 一目了然。

你可以选择：
- **通过** — 写回新的人设，并自动保存当前人设为快照
- **打回** — 附上理由，LLM 会结合理由重新提议（最多 3 轮）
- **拒绝** — 终态拒绝，不写回任何变更

### 🔄 完整回滚

每次写回人设前保存快照，任意历史快照都可回滚。

回滚的安全性：
- 回滚前还会再保存一次"回滚前快照"，便于撤销回滚
- 任意历史版本都可以一键回到
- 即使审批通过后后悔了，也能随时撤销

### 🗜️ 自动压缩

低重要性记忆累积到指定条数时自动压缩，无需人工干预。

压缩的数据安全：
- 先 add 新摘要全部成功后再 delete 旧记忆
- 避免删除后新增失败导致数据丢失
- 摘要记忆会重新评估重要性，有些"被遗忘"的重要信息会被抢救回来

压缩后会注入来源标记：
- `memory_origin=lm_patch_compact`
- `memory_type=SUMMARY`
- 保留原始记忆 ID 列表，便于追溯

### 🚫 Stalled 重启

超过最大 reroll 次数的提案标记为 stalled，仍可手动重启。

- 打回次数超过上限会自动标记为无法收敛
- stalled 提案不会消失，可以随时重启
- 重启时 reroll 计数清零，用上次的打回理由重新提议

---

## 🌼 适配场景

如果你希望：
- Bot 的人设能随着真实经历自然演化，而不是永远停留在初始设定
- 重大人生变化（毕业、搬家、关系变化）能自动反映到人设里
- 人设变更有人工把关，不会乱改
- 记忆库不会无限膨胀，低价值记忆能被自动精简
- 重要信息在被遗忘前能被抢救到摘要里

那记忆演化会很适合你。

---

## 🧩 推荐搭配插件

本插件是 LivingMemory 的补丁，必须搭配使用：

| 插件 | 作用 |
|------|------|
| `astrbot_plugin_livingmemory` | 提供长期记忆存储引擎，本插件通过 weakref 接入其已暴露的接口，零侵入 |

> 本插件不修改 LivingMemory 任何代码，仅消费其已存在的（虽未文档化的）接口。如 LivingMemory 后续版本调整了这些接口，本插件可能需要相应适配。

---

## ⚙️ 主要配置项

### LLM 设置
- `llm_provider_id`：用于人设补丁与记忆压缩的 LLM 提供商（留空则使用默认提供商，建议指定专用 LLM 以免影响主对话）

### 人设补丁
- `persona_patch_enable`：是否启用人设补丁
- `persona_patch_interval_hours`：人设补丁调度间隔（小时，默认 168 即一周）
- `persona_patch_max_memories`：每次读取最大记忆条数
- `persona_patch_max_reroll`：打回重提议最大轮次
- `persona_patch_enable_rollback`：是否启用人设版本快照

### 记忆压缩
- `memory_compact_enable`：是否启用记忆压缩
- `memory_compact_importance_threshold`：压缩重要性阈值（建议高于 LivingMemory 的清理阈值）
- `memory_compact_min_count`：触发压缩的最小记忆条数
- `memory_compact_check_interval_hours`：压缩检查间隔（小时）

---

## 📝 使用说明

### 1. 安装

将本插件放入 AstrBot 的 `data/plugins/` 目录，确保同级目录下已有 `astrbot_plugin_livingmemory`。重启 AstrBot 即可自动加载。

```
data/plugins/
├── astrbot_plugin_livingmemory/   # 必须先安装
└── astrbot_plugin_lm_patch/       # 本插件
```

> 本插件启动时会延迟 60 秒再开始周期调度，等待 LivingMemory 完成初始化。

### 2. 等待首个周期

安装后默认两个功能都开启。插件启动后会等待 60 秒，然后开始第一次检查。如果不想等，可以在 WebUI 中点击「手动触发」按钮立即跑一次。

### 3. 审批人设变更提案

当 LLM 判断有需要更新的人设时，会在 WebUI 的「提案审批」页面生成一条待审提案。点击提案可以看到：

- **左侧**：当前人设原文
- **右侧**：LLM 提议的新人设
- 下方：变更说明、涉及的方面、触发记忆 ID 列表

### 4. 回滚人设

如果发现某次审批后人设不理想，可以在「人设快照」页面找到任意历史快照，点击「回滚」即可。回滚前会自动保存当前人设为新快照，便于撤销回滚。

### 5. 查看压缩日志

记忆压缩是全自动的，无需人工干预。你可以在「压缩日志」页面查看每次压缩的详情：删除了哪些记忆 ID、新增了哪些摘要 ID、删除/新增条数。

---

## 🛠️ 工作原理

```
┌─────────────────────────────────────────────────────────────┐
│                    astrbot_plugin_lm_patch                   │
│                                                              │
│  ┌──────────────┐      ┌──────────────┐                     │
│  │ PersonaPatcher│      │MemoryCompactor│                    │
│  │  (功能 1)     │      │  (功能 2)     │                    │
│  └──────┬───────┘      └──────┬───────┘                     │
│         │                     │                              │
│         │   weakref           │   weakref                    │
│         ▼                     ▼                              │
│  ┌────────────────────────────────────┐                      │
│  │             LMClient               │                      │
│  │  (直读 SQLite + 调用 MemoryEngine) │                      │
│  └────────────────┬───────────────────┘                      │
│                   │                                          │
│                   │ weakref                                  │
│                   ▼                                          │
│  ┌────────────────────────────────────┐                      │
│  │  astrbot_plugin_livingmemory       │                      │
│  │  plugin.initializer.memory_engine  │                      │
│  └────────────────────────────────────┘                      │
│                                                              │
│  ┌────────────────────────────────────┐                      │
│  │              Store                 │                      │
│  │  (本地 SQLite，4 张表)             │                      │
│  │  - patch_checkpoints               │                      │
│  │  - persona_snapshots               │                      │
│  │  - pending_proposals               │                      │
│  │  - compact_log                     │                      │
│  └────────────────────────────────────┘                      │
│                                                              │
│  ┌────────────────────────────────────┐                      │
│  │              WebUI                 │                      │
│  │  (12 个 Web API + Dashboard 页面)  │                      │
│  └────────────────────────────────────┘                      │
└─────────────────────────────────────────────────────────────┘
```

### 关键设计

- **三源真相**：人设只放稳定特质，记忆放流动事实，知识库放客观知识。本插件只动人设这一层。
- **checkpoint 推进策略**：无论 LLM 是否提议变更，都推进 checkpoint 到最新 id，避免重复消耗 token。
- **压缩数据安全**：先 add 新摘要全部成功后再 delete 旧记忆，避免删除后新增失败导致数据丢失。
- **打回不重读记忆**：reroll 只基于原始人设 + 上次提议 + 打回理由，节省 token。
- **回滚安全性**：回滚前自动保存当前人设为新快照，便于撤销回滚。

---

## 🌐 Web API

本插件注册了 12 个 Web API 路由，前缀为 `/astrbot_plugin_lm_patch/page/`。前端 Dashboard 通过这些 API 与后端交互，你也可以直接调用它们集成到自己的工具中。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/status` | 获取插件运行状态 |
| GET | `/proposals` | 获取提案列表（支持 `status` / `limit` 参数） |
| GET | `/proposal?id={id}` | 获取单个提案详情 |
| POST | `/proposal/approve` | 通过提案（body: `{"id": int}`） |
| POST | `/proposal/reject` | 拒绝提案（body: `{"id": int, "reason": str}`） |
| POST | `/proposal/reroll` | 打回提案（body: `{"id": int, "reason": str}`） |
| POST | `/proposal/restart` | 重启 stalled 提案（body: `{"id": int}`） |
| GET | `/snapshots` | 获取快照列表（支持 `persona_id` / `limit` 参数） |
| POST | `/snapshot/rollback` | 回滚到快照（body: `{"id": int}`） |
| GET | `/compact-log` | 获取压缩日志（支持 `persona_id` / `limit` 参数） |
| POST | `/trigger/patch` | 手动触发一次人设补丁周期 |
| POST | `/trigger/compact` | 手动触发一次记忆压缩周期 |

---

## ❓ FAQ

**Q：为什么不直接修改 LivingMemory？**
A：LivingMemory 是 lxfight 的作品，我们尊重其设计。本插件通过 weakref 接入其已暴露的接口，零侵入。

**Q：人设补丁会不会乱改人设？**
A：不会。LLM 只能**提议**，是否写回完全由人工审批。即使审批通过，也可以随时回滚到任意历史快照。

**Q：记忆压缩会不会丢数据？**
A：压缩 LLM 会尽量保全关键事实。如果某条记忆包含独特信息，应在摘要中保留。压缩后原始记忆会被删除，但你可以在压缩日志中查到删除了哪些 ID。

**Q：LLM 调用失败怎么办？**
A：本插件有完善的容错。LLM 调用失败时会跳过本次周期，下次再试。已经创建的提案不会丢失。

**Q：能不能不用 WebUI，直接用聊天指令？**
A：当前版本不提供聊天指令，所有操作通过 WebUI 进行。如果你有这个需求，欢迎提 issue。

---

## 🌟 鸣谢

本插件基于 [LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) 构建，感谢其作者 **lxfight** 设计并实现了如此优秀的长期记忆系统。没有 LivingMemory 暴露的 `get_active_plugin()` weakref 入口与 `MemoryEngine` 接口，本插件将无从下手。

---

## 📄 许可证

[MIT](./LICENSE)
