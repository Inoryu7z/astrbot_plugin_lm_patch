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
