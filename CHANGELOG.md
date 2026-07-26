### v1.1.6

**日记压制与压缩分池**

- 新增日记过滤补丁：检索结果中日记条目最多返回 1 条（可配置），通过 monkey-patch 包装 `search_memories`，不修改 livingmemory 代码
- 记忆压缩拆为日记池与对话池，互不相通，各自独立判断触发阈值
- 日记压缩使用专属提示词，强制标注 `source="daymind"` + `type="diary"`，重要性上限 0.5
- 压缩代数限制：日记最多压一次，对话最多压两次，超限由 LivingMemory 自然清理接管
- 新增配置项：`diary_filter_enable`、`diary_filter_max_in_recall`

---

### v1.1.5

**修复压缩摘要 source 字段未传递**

- 压缩提示词新增 `source` 输出字段
- 代码回退到整批级别判断：同质继承，混合不写入

---

### v1.1.4

**同步 amnesia /forget 到 livingmemory**

- 新增 `after_message_sent` 钩子，监听 `/forget` 命令后同步删除 livingmemory 中对应消息
- 不兼容 amnesia 的 `/cancel_forget` 反悔机制（影响是"少记"而非"记错"）

---

### v1.1.3

**修复 /reset 后 livingmemory 会话不清理**

- 新增钩子监听 `_clean_group_context_session` 信号，触发 livingmemory 的 `handle_session_reset`
- 兼容 AstrBot 4.26+ 的键名变更

---

### v1.1.2

**记忆压缩源感知 + 压缩阈值下调**

- 压缩提示词新增来源标记，日记与对话差异化处理
- 压缩重要性阈值 0.5 → 0.3，与 LivingMemory 清理阈值一致

---

### v1.1.1

**人设补丁源感知：过滤虚构日记污染**

- 人设补丁提示词新增来源标记
- 用户重大决定类记忆需多源佐证，角色自身演化可单源触发

---

### v1.1.0

**历史记忆初始化功能**

- WebUI 一键启动人设迭代初始化与记忆压缩初始化
- 分批处理历史记忆，支持取消与异常恢复
- 数据库自动迁移，老用户无感升级

---

### v1.0.0

**首次发布：人设演化与记忆压缩**

- 人设补丁：周期性读取新增记忆，LLM 提议人设变更，WebUI 审批，支持快照回滚
- 记忆压缩：低重要性记忆自动压缩成摘要，先增后删避免数据丢失
- WebUI Dashboard：提案审批、人设快照回滚、压缩日志、运行状态
