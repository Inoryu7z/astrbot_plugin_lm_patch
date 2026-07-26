### v1.1.8

**🔁 压缩代数限制：日记最多压一次，对话最多压两次**

v1.1.7 让日记与对话分池压缩后，新问题是"压缩摘要本身能否被再次压缩"。无限制递归压缩会让 LLM 反复精炼已精炼过的内容，信息丢失风险随代数递增；日记一次压缩已到极限，对话两次压缩后信息已高度浓缩。

本次新增 `compact_generation` 字段标记压缩代数，并按池类型限制最大压缩轮数。

**1. 🏷️ 压缩代数标记**

* 原始记忆：无 `compact_generation` 字段（视为 0）
* 一次压缩摘要：`compact_generation=1`
* 二次压缩摘要：`compact_generation=2`
* `_compact_memories` 写入新摘要时：`new_generation = max(本批记忆的 generation, 默认0) + 1`
* 允许 generation=0 和 generation=1 的记忆在同一批被压成 generation=2（避免某些记忆存在时间长、某些短导致的分批复杂度）

**2. 🔢 按池类型限制压缩代数上限**

| 池 | max_generation | 含义 |
|---|---|---|
| 日记池（daymind） | 0 | 只压原始记忆（generation=0），generation=1 的日记摘要不再被压 |
| 对话池（conversation） | 1 | 压原始记忆（generation=0）和一次摘要（generation=1），generation=2 的对话摘要不再被压 |

* 日记压一次足够：日记是虚构内容，一次压缩已做"激进合并保留情感"，再压只会丢失仅剩的情感碎片
* 对话压两次合理：对话是真实信息，给两轮压缩机会充分精炼，但两轮后信息已高度浓缩，再压 LLM 风险大于收益
* 超过代数上限的摘要不被 `list_low_importance_memories` 返回，不再进入压缩流程，由 LivingMemory 自身清理机制（阈值 0.3）自然接管

**3. 🛠️ 实现细节**

* `lm_client.list_low_importance_memories` 新增 `max_generation` 参数：
  - SQL 过滤：`CAST(COALESCE(NULLIF(compact_generation, ''), '0') AS INTEGER) <= ?`
  - `NULLIF(..., '')` 把空字符串转 NULL，`COALESCE(..., '0')` 把 NULL 转 '0'，正确处理无字段/空值/正常值三种情况
  - `None` 表示不过滤（向后兼容）
* `MemoryCompactor._compact_one_pool` 按池类型传 `max_generation`：日记池=0，对话池=1
* `MemoryCompactor._compact_init_pool` 同步适配（WebUI 初始化流程与正常周期一致）
* 二次压缩的对话摘要不限制重要性上限（LLM 正常评估）

**4. 📌 设计哲学**

* **不对称设计**：日记被压制（只压一次）、对话被保留（压两次），与 v1.1.6 检索过滤、v1.1.7 双池分离的设计哲学一脉相承
* **让 LivingMemory 自然清理接管**：超过代数上限的摘要不被再压，importance 仍会随时间衰减，低于 0.3 时被 LivingMemory 清理。被清理是"遗忘"，被错误压缩是"记错"——后者更危险
* **混合批次安全**：generation=0 和 generation=1 可在同一批被压成 generation=2，无需按代数分批。混合本身不会造成信息丢失，因为同池内来源类型一致

---

### v1.1.7

**🧠 记忆压缩双池分离：日记与对话独立压缩**

v1.1.6 让日记在检索环节被压制，但压缩环节仍是混合处理：日记与真实对话记忆合并成同一批摘要，提示词需用 mixed 策略让 LLM 区分对待，但 LLM 实际执行精准度有限。同时 v1.1.5 的 source 字段依赖 LLM 输出，若 LLM 不输出则压缩后日记摘要丢失标记，下次检索过滤补丁识别不到。

本次将压缩环节做根因改造：**日记与对话分池压缩，互不相通**。每个池独立判断 min_count 触发，使用专属提示词，强制标注来源标记。

**1. 🔀 双池分离压缩**

* `lm_client.list_low_importance_memories` 新增 `source_filter` 参数：
  - `"daymind"`：只查 `source="daymind"` 的日记记忆
  - `"conversation"`：只查非日记记忆（含 source 为 None/unknown/mixed 或其他值）
  - `None`：不过滤（向后兼容）
  - 对话池过滤用 SQLite `IS NOT`（null-safe 不等）：`NULL IS NOT 'daymind'` → true，正确包含无 source 字段的旧记忆
* `MemoryCompactor._compact_single_persona` 拆为两步：先压缩日记池，再压缩对话池，各自独立判断 `min_count`
* `MemoryCompactor._compact_memories` 新增 `batch_kind` 入参：
  - `"daymind"`：使用日记专属提示词，新摘要强制写入 `source="daymind"` + `type="diary"`，重要性 clamp 到 0.5 以下
  - `"conversation"`：使用对话专属提示词，新摘要不带 source/type 标记
* `MemoryCompactor._compact_init_pool` 新增辅助方法：WebUI 触发的初始化流程中，每个 persona 内部先压日记池再压对话池，独立循环

**2. 📝 提示词拆为两套（`prompts.py`）**

* 新增 `MEMORY_COMPACT_SYSTEM_PROMPT_DIARY`（日记专属）：
  - 强调激进合并、保留情感与心路、淡化具体事件
  - 压缩后重要性建议 0.3-0.4，**不应高于 0.5**
  - 淡化"用户重大决定"为弱化表述
* 新增 `MEMORY_COMPACT_SYSTEM_PROMPT_CONVERSATION`（对话专属）：
  - 强调信息保全优先、正常合并冗余、重要性可重新评估
  - 保留完整的"压缩=合并冗余而非删减"说明与示例
* 移除 mixed 策略章节（不再产生混合批次）
* 移除 source 输出字段（由代码根据 batch_kind 强制写入）
* `MEMORY_COMPACT_USER_TEMPLATE` 共用，不区分批次类型
* 旧别名 `MEMORY_COMPACT_SYSTEM_PROMPT` 保留指向 CONVERSATION 版本，防止外部引用断裂

**3. 🛡️ 强制标注与重要性 clamp**

* 日记池压缩后摘要 metadata 强制写入 `source="daymind"` + `type="diary"`，不依赖 LLM 输出
* 日记池新摘要重要性 > 0.5 时代码强制 clamp 到 0.4（提示词已要求，代码兜底）
* 对话池压缩后摘要不带 source/type 标记（默认 unknown）

**4. 📌 设计哲学**

* **池子互不相通**：日记永远只与日记合并，对话永远只与对话合并，避免虚构事件污染真实记忆摘要
* **强制标注**：压缩后日记摘要必然带 source/type 标记，确保下次压缩时仍被识别进入日记池，下次检索时被 v1.1.6 过滤补丁识别压制
* **代码兜底**：提示词要求 LLM 做的事（importance 不超过 0.5、source 标记），代码层都做了 clamp 与强制写入，不依赖 LLM 遵守度

---

### v1.1.6

**🛡️ 日记过滤补丁：检索结果中日记条目数量压制**

daymind 插件每日生成虚拟日记存入 LivingMemory，这些日记是虚构内容而非真实对话。若检索时与真实对话记忆同等返回，会稀释真实记忆占比、让模型基于虚构内容作答。本次新增"日记过滤补丁"，通过 monkey-patch 包装 `MemoryEngine.search_memories`，在检索结果返回前过滤掉多余的日记条目。

**核心行为**：单次检索结果中最多保留 `diary_filter_max_in_recall` 条日记（默认 1）。即使记忆库中只有日记且全部召回匹配率极高，也只返回 1 条日记，其余被压制。日记识别口径为 `metadata.source=="daymind"` 或 `metadata.type=="diary"` 任一命中，覆盖原始日记、压缩后的日记摘要（v1.1.5 起 lm_patch 压缩会保留 source 字段）以及未来可能出现的其他 daymind 衍生记忆。

**1. 🔧 实现机制**

* 新增 `core/diary_filter.py`：核心过滤逻辑、安装/卸载/重试机制
  - `install_diary_filter(engine, max_diaries)`：包装 `engine.search_memories`，返回结果中最多保留 `max_diaries` 条日记
  - `install_with_retries(get_engine_fn, max_diaries, ...)`：周期性尝试获取 `memory_engine` 并安装补丁，应对 livingmemory 异步初始化未就绪的场景
  - `uninstall_diary_filter(engine)`：恢复原始 `search_memories`，用于插件 terminate
  - 已安装补丁的 `max_diaries` 变化时自动重新包装；过滤逻辑异常时降级返回原始结果，不阻断检索
* `main.py` 集成生命周期：
  - `initialize()` 启动后台任务 `_install_diary_filter_loop`，等待 livingmemory 就绪后安装补丁
  - `terminate()` 取消安装任务 + 卸载已安装补丁，恢复原始 `search_memories`
  - 后台任务最多尝试 20 次 × 间隔 30 秒（共 10 分钟），覆盖 livingmemory 初始化时间

**2. ⚙️ 配置项（`_conf_schema.json` 新增 2 项）**

* `diary_filter_enable`（默认 `true`）：是否启用日记过滤补丁
* `diary_filter_max_in_recall`（默认 `1`）：单次检索允许保留的日记条数。设为 0 表示全部过滤，设为较大值相当于关闭过滤

**3. 📌 设计哲学**

* **压制而非清除**：日记仍参与检索（保留 1 条），让角色"活过来"的设计意图得以保留
* **零侵入 LivingMemory**：通过 monkey-patch 包装 `search_memories`，不修改 livingmemory 任何代码
* **降级安全**：过滤逻辑异常时降级返回原始结果，不阻断检索链路

---

### v1.1.5

**🐛 修复压缩摘要 source 字段未传递的问题**

v1.1.2 引入的源感知压缩策略存在遗留问题：压缩后生成的新摘要 metadata 中**没有 `source` 字段**，导致 daymind 日记被压缩后，下次再被压缩时 `_format_memories` 读取到的是 `来源:unknown`，LLM 会把它当真实对话处理，削弱了源感知策略的效果。

**修复方案**：提示词新增 `source` 输出字段 + 代码回退到整批级别判断。

* `prompts.py` `MEMORY_COMPACT_SYSTEM_PROMPT`：
  - 输出格式 JSON 新增 `"source": "daymind|unknown|mixed"` 字段
  - 字段说明新增 `source` 字段（daymind=全部来自虚构日记，unknown=全部来自真实对话，mixed=混合来源）
  - 关键要求新增"source 必须正确标记"，要求 LLM 根据合并的记忆来源填写
* `core/memory_compactor.py` `_compact_memories`：
  - 收集原始记忆的 `source` 字段到 `source_set`
  - 整批级别回退：若所有原始记忆 source 相同（如全是 daymind）则继承该值，混合或全无则 None
  - 每个摘要优先使用 LLM 输出的 `source`，若 LLM 未输出则回退到 `batch_source`
  - `metadata["source"]` 写入最终 source 值（None 时不写入，默认 unknown）

**回退策略说明**：
* LLM 输出有效 source（daymind/unknown/mixed）→ 使用 LLM 输出
* LLM 未输出 + 整批同质 → 继承整批 source
* LLM 未输出 + 整批混合/全无 → 不写入 source（默认 unknown，用真实对话策略保守处理）

---

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
