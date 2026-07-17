### v1.1.4

**🧠 同步 amnesia /forget 到 livingmemory**

amnesia 插件（`astrbot_plugin_llm_amnesia`）的 `/forget` 命令只清 AstrBot 的 `conversation_manager`（删除 `conversation_history` 最新 N 轮），完全不触碰 livingmemory 的独立 SQLite 数据库。导致被 `/forget` 的对话仍留在 livingmemory 中，仍被计入 `unsummarized_rounds`，最终被总结进长期记忆——与之前 `/reset` 的问题同构。

**修复方案**：在 lm_patch 新增 `after_message_sent` 钩子 `handle_forget_patch`，监听 `/forget` 命令执行后，同步删除 livingmemory 中对应的最新 N 轮消息。

* 新增 `@filter.after_message_sent()` 钩子，检测 `/forget` 命令（正则匹配，排除 `/forget_status`、`/forget_help`、`/cancel_forget`）
* 解析 `round_count` 参数（默认 1，范围 1-10，与 amnesia 一致）
* 通过 `LMClient.get_plugin()` 获取 livingmemory 插件实例，访问 `event_handler.conversation_manager.store`
* 从后往前查找 N 个 `user + assistant` 消息对（与 amnesia 的轮次查找算法一致）
* 加 store 写锁，事务内删除消息 + 更新 `sessions.message_count`
* 清除 conversation_manager 的 LRU 缓存，确保下次读取重新加载
* `unsummarized_rounds` 会随消息删除自动减少（`unsummarized = total - last_summarized_index`），被 forget 的对话不再计入总结轮次

**⚠️ 已知限制**：不兼容 amnesia 的 `/cancel_forget` 反悔机制。反悔时 AstrBot 侧恢复，但 livingmemory 侧不恢复。影响是"少记"而非"记错"，可接受（反悔场景极少，且少记不会污染记忆）。

---

### v1.1.3

**🐛 修复 /reset 后 livingmemory 会话不清理的问题**

AstrBot 4.26+ 把 `/reset` 命令的 extra 信号键名从 `_clean_ltm_session` 重构为 `_clean_group_context_session`，但 livingmemory 2.3.5 仍监听旧键名 `_clean_ltm_session`，导致 `/reset` 后 livingmemory 的 `handle_session_reset` 钩子永远不触发，`conversation_manager.clear_session()` 从未执行，旧对话消息仍留在 livingmemory 自己的 SQLite 数据库中，最终被总结进长期记忆（用户反馈"a今天叫我去吃烧烤"在 /reset 后仍被记入）。

**修复方案**：在 lm_patch 新增 `after_message_sent` 钩子 `handle_session_reset_patch`，监听新键名 `_clean_group_context_session`，触发后调用 livingmemory 的 `event_handler.handle_session_reset(event)` 完成清理。

* 新增 `@filter.after_message_sent()` 钩子，监听 `_clean_group_context_session` 信号
* 通过 `LMClient.get_plugin()` 获取 livingmemory 插件实例，调用其 `event_handler.handle_session_reset(event)`
* 钩子快速返回：非 reset 信号时第一行即 return，性能开销可忽略
* 向后兼容：若未来 livingmemory 修复键名，两个钩子都会触发但 `clear_session` 是幂等的，双触发安全

---

### v1.1.2

**🧠 记忆压缩源感知 + 压缩阈值下调**

针对 daymind 日记（虚构内容）与真实对话记忆在压缩时被同等对待的问题，新增"记忆压缩源感知"机制，让 LLM 区分对待虚构日记与真实对话记忆。同时下调压缩阈值，让日记记忆在衰减后被精简压缩，长期下来真实对话记忆保留更完整。

**1. 🔍 压缩记忆来源标记**

* `MemoryCompactor._format_memories` 在每条记忆前增加 `[来源:{source}]` 标记
* LLM 可看到 `来源:daymind`（虚构日记）与 `来源:unknown`（真实对话）的差异
* 与 v1.1.1 人设补丁的源感知机制对齐，现在记忆压缩环节也能识别 daymind 日记

**2. 🧠 差异化压缩策略**

* `MEMORY_COMPACT_SYSTEM_PROMPT` 新增"记忆来源与压缩策略"章节
* **daymind 日记**：更激进地合并，保留情感与心路、淡化具体事件细节，压缩后重要性不提升（保持 0.3-0.4），淡化用户重大决定
* **真实对话记忆**：信息保全优先，正常合并冗余，重要性可重新评估
* **混合批次**：优先分别处理，若必须合并以真实对话为主体、daymind 作为情感背景

**3. 📉 压缩阈值下调**

* `memory_compact_importance_threshold` 默认值 0.5 → 0.3
* 与 LivingMemory 清理阈值（0.3）一致：记忆重要性衰减到 0.3 时被压缩成摘要保留，低于 0.3 则被清理
* daymind 日记初始重要性 0.4，衰减约 10 天后到达 0.3 被压缩，真实对话记忆起始权重高、衰减慢，不会被过早压缩

---

### v1.1.1

**🛡️ 人设补丁源感知：过滤虚构日记污染**

针对 daymind 插件用 LLM 生成虚构日记（含用户重大决定等敏感内容）写入 LivingMemory 后，本插件人设补丁可能据此多次无效提议人设变更的问题，新增"记忆来源感知"机制，让 LLM 区分对待虚构日记与真实对话记忆。

**1. 🔍 记忆来源标记**

* `PersonaPatcher._format_memories` 在每条记忆前增加 `[来源:{source}]` 标记
* LLM 可看到 `source=daymind`（虚构日记）与其他来源（真实对话捕获等）的差异

**2. 🧠 多源佐证规则**

* `PERSONA_PATCH_SYSTEM_PROMPT` 新增"记忆来源与可信度"与"用户与角色关系变化判断规则"两节
* **关键规则**：对于"用户与角色关系重大变化"类记忆（表白、分手、关系定性改变、用户做出承诺等），若**仅来自 daymind 日记**而无其他来源佐证，LLM **不得提议变更人设**
* 对于"角色自身状态变迁"（角色毕业、角色搬家等），即使仅来自 daymind 日记也可正常提议——保留日记系统让角色"活过来"的设计意图

**3. 📌 设计哲学**

* 不切断日记路径：日记仍参与人设演化，角色仍能从虚构生活学习
* 区分对待：用户重大决定需多源佐证，角色自身演化可单源触发
* 配合 v1.1.0 的审批机制（人工最后把关），风险可控

---

### v1.1.0

**🚀 历史记忆初始化功能**

针对长期使用 LivingMemory 积累数百条历史记忆的老用户，新增 WebUI 触发的初始化功能，分批处理历史记忆，避免首次加载全量读取的设计缺陷。

**1. 🧬 人设迭代初始化**

* WebUI 一键启动，按历史记忆顺序对每个 persona 分批处理
* 每批硬编码 20 条记忆，由 LLM 提议人设变更并生成待审提案
* 审批通过后自动推进下一批，直至所有 persona 的全部历史记忆处理完毕
* 完成后自动将每个 persona 的 checkpoint 设为当前最大 id，后续周期仅监控新增记忆
* LLM 判断无需变更的批次自动跳过，不阻塞流程

**2. 🗜️ 记忆压缩初始化**

* WebUI 一键启动，从重要性最低的记忆开始分批压缩
* 每批硬编码 10 条，后台自动运行，无需用户介入
* 前端每 5 秒轮询状态，完成或异常时弹出通知
* 每批独立执行"先 add 新摘要，再 delete 旧记忆"事务，单批失败不影响下一批

**3. 🔒 互斥与状态机**

* 两种初始化互斥，同一时间仅允许一个进行中
* `init_state` 单行表持久化状态：idle / running / completed / cancelled
* 支持随时取消，后台任务在当前批次完成后退出
* 异常自动落库为 cancelled 状态并记录 error 字段

**4. 🗄️ 数据库迁移**

* `pending_proposals` 表新增 `is_init` 与 `init_batch` 列
* 启动时自动检测旧表结构，缺列时通过 `ALTER TABLE ADD COLUMN` 在线补齐
* 老用户升级无感，无需手动迁移

**5. 🌐 WebUI 增强**

* 新增"初始化"导航页，含介绍说明与三个操作按钮
* 提案列表与详情页对初始化迭代提案显示"初始化·迭代 N"徽标
* 审批初始化提案后自动加载并选中新一批生成的提案
* 状态卡片含进度信息（当前 persona、迭代批次、已处理条数）与完成/错误摘要

---

### v1.0.0

**🧬 首次发布：人设演化与记忆压缩**

**1. 🧬 人设补丁**

* 通过 weakref 接入 LivingMemory 插件实例，零侵入，不修改 livingmemory 任何代码
* 周期性读取 LivingMemory 新增记忆，由 LLM 判断是否需要更新人设
* WebUI 提交并排对比的变更提案，左侧原文 / 右侧提议，diff 一目了然
* 审批通过后写回 PersonaManager，并自动保存人设快照
* 支持任意历史版本回滚，回滚前自动保存"回滚前快照"，便于撤销回滚
* 审批打回可附理由，LLM 结合理由重新提议，最多 3 轮
* 超过最大 reroll 次数的提案标记为 stalled，可手动重启
* 无论 LLM 是否提议变更都推进 checkpoint，避免重复消耗 token

**2. 🗜️ 记忆压缩**

* 低重要性记忆（默认阈值 0.5）累积到指定条数（默认 10 条）时自动压缩
* 由 LLM 归纳成更少的摘要记忆，重新评估重要性
* 全自动执行，无需审批
* 先 add 新摘要全部成功后再 delete 旧记忆，避免删除后新增失败导致数据丢失
* 摘要记忆注入 `memory_origin=lm_patch_compact` 与 `memory_type=SUMMARY` 元数据

**3. ⚙️ 调度与容错**

* 后台调度器：人设补丁与记忆压缩各自独立循环，启动时延迟 60 秒等待 LivingMemory 初始化
* LLM provider 支持：可配置专用 provider，留空则回退到默认 provider 并记录警告
* 完善的容错：LLM 调用失败跳过本次周期，已创建的提案不会丢失

**4. 🌐 WebUI Dashboard**

* 提案审批：并排对比 + 打回理由 + stalled 重启
* 人设快照：查看 + 回滚
* 压缩日志：查看每次压缩的删除/新增详情
* 运行状态：LivingMemory 可用性、调度器状态、当前调度间隔
* 12 个 Web API 路由，支持手动触发补丁与压缩周期
* Notion 风设计系统：对齐 LivingMemory WebUI 风格，明暗双主题切换、SVG 图标、系统化设计 token
