"""功能 2：记忆压缩主流程。

低重要性记忆累积到指定条数时，由 LLM 归纳成更少的摘要记忆。
全自动执行：删旧加新，重新评估重要性，无需审批。
"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger

from ..prompts import (
    MEMORY_COMPACT_SYSTEM_PROMPT,
    MEMORY_COMPACT_USER_TEMPLATE,
    extract_json,
)
from .llm_helper import LLMHelper
from .lm_client import LMClient
from .store import Store

# 记忆压缩初始化的批次大小（硬编码，用户确认）
INIT_COMPACT_BATCH = 10
# 初始化时单 persona 连续压缩失败的最大次数，超过则跳过该 persona 进入下一个
# 避免 LLM 持续失败时死循环读取相同批次记忆
_INIT_MAX_CONSECUTIVE_FAILURES = 3


class MemoryCompactor:
    """记忆压缩器。"""

    def __init__(
        self,
        context: Any,
        lm_client: LMClient,
        store: Store,
        llm_helper: LLMHelper,
        config: dict,
    ) -> None:
        self.context = context
        self.lm_client = lm_client
        self.store = store
        self.llm_helper = llm_helper
        self.config = config
        self._init_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # 配置属性
    # ------------------------------------------------------------------

    @property
    def importance_threshold(self) -> float:
        return float(self.config.get("memory_compact_importance_threshold", 0.5))

    @property
    def min_count(self) -> int:
        return int(self.config.get("memory_compact_min_count", 10))

    @property
    def llm_provider_id(self) -> str:
        return str(self.config.get("llm_provider_id", "") or "")

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def run_compact_cycle(self) -> int:
        """对所有 persona 跑一遍压缩检查。返回执行的压缩次数。"""
        if not await self.lm_client.is_available():
            logger.warning("[LMPatch] LivingMemory 不可用，跳过记忆压缩周期")
            return 0

        # 初始化进行中时跳过正常周期，避免与初始化流程冲突
        init_state = await self.store.get_init_state()
        if init_state.get("status") == "running":
            logger.info("[LMPatch] 初始化进行中，跳过记忆压缩周期")
            return 0

        # 只处理近 30 天有真实记忆更新的 persona，跳过已被用户抛弃的 persona。
        # get_active_persona_ids 已排除 memory_origin='lm_patch_compact' 的压缩摘要，
        # 避免压缩自身产生的记忆被误判为"近期活跃"导致跳过逻辑失效。
        persona_ids = await self.lm_client.get_active_persona_ids(days=30)
        if not persona_ids:
            logger.info("[LMPatch] 未发现近 30 天活跃的 persona_id，跳过记忆压缩周期")
            return 0

        compact_count = 0
        for persona_id in persona_ids:
            try:
                compacted = await self._compact_single_persona(persona_id)
                if compacted:
                    compact_count += 1
            except Exception as e:
                logger.warning(
                    f"[LMPatch] 压缩 persona '{persona_id}' 的记忆时出错: {e}"
                )
        logger.info(f"[LMPatch] 记忆压缩周期完成，共执行 {compact_count} 次压缩")
        return compact_count

    async def _compact_single_persona(self, persona_id: str) -> bool:
        """处理单个 persona 的记忆压缩。返回是否执行了压缩。"""
        # persona 在 AstrBot 中已被删除时，跳过（压缩孤儿记忆无意义）
        try:
            await self.context.persona_manager.get_persona(persona_id)
        except Exception:
            logger.debug(
                f"[LMPatch] persona '{persona_id}' 在 AstrBot 中不存在，跳过压缩"
            )
            return False

        # 读取低重要性记忆
        memories = await self.lm_client.list_low_importance_memories(
            persona_id=persona_id,
            importance_threshold=self.importance_threshold,
            limit=100,
        )

        if len(memories) < self.min_count:
            logger.debug(
                f"[LMPatch] persona '{persona_id}' 低重要性记忆仅 {len(memories)} 条，"
                f"不足 {self.min_count} 条，跳过压缩"
            )
            return False

        return await self._compact_memories(persona_id, memories)

    async def _compact_memories(
        self, persona_id: str, memories: list[dict]
    ) -> bool:
        """对给定的一批低重要性记忆执行压缩。

        流程：先 add 新摘要全部成功后再 delete 旧记忆（避免删除后新增失败导致数据丢失）。
        输出格式与 LivingMemory 的记忆格式对齐：第一人称、保留会话ID、
        metadata 包含 persona_summary/canonical_summary/topics/key_facts 等。
        返回是否成功执行了压缩。
        """
        from collections import Counter
        from datetime import datetime

        # 获取人格系统提示词，为 LLM 提供人格上下文
        persona_text = ""
        try:
            persona = await self.context.persona_manager.get_persona(persona_id)
            persona_text = persona.system_prompt or ""
        except Exception as e:
            logger.debug(
                f"[LMPatch] 获取 persona '{persona_id}' 系统提示词失败，使用空文本: {e}"
            )

        # 构造记忆文本
        memories_text = self._format_memories(memories)

        # 调用 LLM 压缩
        current_date = datetime.now().strftime("%Y-%m-%d %H:%M")
        user_prompt = MEMORY_COMPACT_USER_TEMPLATE.format(
            current_date=current_date,
            persona_text=persona_text,
            memory_count=len(memories),
            memories_text=memories_text,
        )
        raw = await self.llm_helper.chat(
            system_prompt=MEMORY_COMPACT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            provider_id=self.llm_provider_id,
        )
        if not raw:
            return False

        result = extract_json(raw)
        if result is None:
            logger.warning(
                f"[LMPatch] persona '{persona_id}' 压缩 LLM 输出无法解析为 JSON"
            )
            return False

        summaries = result.get("summaries", [])
        if not summaries or not isinstance(summaries, list):
            logger.warning(
                f"[LMPatch] persona '{persona_id}' 压缩 LLM 输出无有效 summaries"
            )
            return False

        # 执行：先添加新摘要，再删除旧记忆（顺序很重要，避免删除后新增失败导致数据丢失）
        original_ids = [m["id"] for m in memories]
        created_ids: list[int] = []

        # 收集所有来源记忆的会话ID，用于为新摘要选择 session_id
        source_session_list: list[str] = []
        for m in memories:
            sid = (m.get("metadata", {}) or {}).get("session_id")
            if sid:
                source_session_list.append(sid)

        for idx, summary in enumerate(summaries):
            if not isinstance(summary, dict):
                continue

            # summary 字段（第一人称人格风格摘要）
            summary_text = str(summary.get("summary", "")).strip()
            if not summary_text:
                continue

            # 构建 canonical_summary（事实导向、风格中性，用于检索）
            # 与 LivingMemory 的 _build_storage_format 保持一致
            key_facts_raw = summary.get("key_facts", [])
            if not isinstance(key_facts_raw, list):
                key_facts_raw = [str(key_facts_raw)] if key_facts_raw else []
            key_facts = [str(f) for f in key_facts_raw[:5] if f]

            canonical_parts = [summary_text]
            if key_facts:
                canonical_parts.append("；".join(key_facts))
            canonical_summary = " | ".join(canonical_parts)

            if not canonical_summary.strip():
                continue

            # 重要性
            importance = summary.get("importance", 0.5)
            try:
                importance = float(importance)
            except (TypeError, ValueError):
                importance = 0.5
            importance = max(0.0, min(1.0, importance))

            # source_count
            source_count = summary.get("source_count", 0)
            try:
                source_count = int(source_count)
            except (TypeError, ValueError):
                source_count = 0

            # LLM 输出的 source_session_ids
            llm_session_ids = summary.get("source_session_ids", [])
            if not isinstance(llm_session_ids, list):
                llm_session_ids = []

            # topics
            topics_raw = summary.get("topics", [])
            if not isinstance(topics_raw, list):
                topics_raw = [str(topics_raw)] if topics_raw else []
            topics = [str(t) for t in topics_raw[:5] if t]

            # participants
            participants = summary.get("participants", [])
            if not isinstance(participants, list):
                participants = [str(participants)] if participants else []
            participants = [str(p) for p in participants if p]

            # sentiment
            sentiment = str(summary.get("sentiment", "neutral")).lower()
            if sentiment not in ("positive", "neutral", "negative"):
                sentiment = "neutral"

            # 判断交互类型
            interaction_type = "group_chat" if participants else "private_chat"

            # 为新摘要选择 session_id：
            # 优先使用 LLM 输出的 source_session_ids 中出现最多的，
            # 回退到来源记忆中 session_id 出现最多的
            session_candidates = (
                [str(s) for s in llm_session_ids if s]
                if llm_session_ids
                else source_session_list
            )
            if session_candidates:
                new_session_id = Counter(session_candidates).most_common(1)[0][0]
            else:
                new_session_id = None

            # 构建 metadata，与 LivingMemory 的格式对齐
            metadata = {
                "importance": importance,
                "source_count": source_count,
                "source_memory_ids": original_ids,
                "summary_reason": summary.get("reason", ""),
                # LivingMemory 对齐字段
                "topics": topics,
                "key_facts": key_facts,
                "sentiment": sentiment,
                "interaction_type": interaction_type,
                "canonical_summary": canonical_summary,
                "persona_summary": summary_text,
                "summary_schema_version": "v2",
                "source_session_ids": [str(s) for s in llm_session_ids if s],
            }
            if participants:
                metadata["participants"] = participants

            new_id = await self.lm_client.add_memory(
                content=canonical_summary,
                session_id=new_session_id,
                persona_id=persona_id,
                importance=importance,
                metadata=metadata,
            )
            if new_id is not None:
                created_ids.append(new_id)
            else:
                logger.warning(
                    f"[LMPatch] persona '{persona_id}' 添加摘要 #{idx + 1} 失败"
                )

        if not created_ids:
            logger.warning(
                f"[LMPatch] persona '{persona_id}' 所有摘要添加失败，不删除原始记忆"
            )
            return False

        # 批量删除原始记忆
        deleted_count = await self.lm_client.batch_delete_memories(original_ids)
        logger.info(
            f"[LMPatch] persona '{persona_id}' 压缩完成："
            f"删除 {deleted_count}/{len(original_ids)} 条原始记忆，"
            f"新增 {len(created_ids)} 条摘要"
        )

        # 记录日志
        await self.store.log_compaction(
            persona_id=persona_id,
            deleted_ids=original_ids,
            created_ids=created_ids,
        )
        return True

    # ------------------------------------------------------------------
    # 记忆压缩初始化（WebUI 触发，后台分批清完积压）
    # ------------------------------------------------------------------

    async def start_compact_init(self) -> dict:
        """启动记忆压缩初始化：后台分批压缩所有 persona 的低重要性记忆。

        从每个 persona 重要性最低的记忆开始，每次取 INIT_COMPACT_BATCH(10) 条压缩，
        循环直到该 persona 不足 min_count 条，然后进入下一个 persona。
        全程后台运行，完成后更新 init_state 为 completed。
        只初始化最近 30 天内有活跃记忆新增的 persona，跳过已被用户抛弃的 persona。
        """
        if not await self.lm_client.is_available():
            return {"success": False, "error": "LivingMemory 不可用"}

        # 只初始化最近 30 天活跃的 persona，跳过已被用户抛弃的
        persona_ids = await self.lm_client.get_active_persona_ids(days=30)
        if not persona_ids:
            return {"success": False, "error": "未发现近 30 天活跃的 persona_id，无需初始化"}

        if not await self.store.start_init("compact", len(persona_ids)):
            return {"success": False, "error": "已有初始化正在进行中，请先取消或等待完成"}

        logger.info(
            f"[LMPatch] 启动记忆压缩初始化：共 {len(persona_ids)} 个 persona，"
            f"每批 {INIT_COMPACT_BATCH} 条"
        )
        # 后台运行，不阻塞 WebUI 响应
        self._init_task = asyncio.create_task(self._run_compact_init(persona_ids))
        return {"success": True, "message": "记忆压缩初始化已启动，后台运行中"}

    async def _run_compact_init(self, persona_ids: list[str]) -> None:
        """后台执行记忆压缩初始化。"""
        try:
            total_compacted = 0
            for idx, persona_id in enumerate(persona_ids):
                # 检查是否被取消
                state = await self.store.get_init_state()
                if state.get("status") != "running":
                    logger.info("[LMPatch] 记忆压缩初始化已取消，停止处理")
                    return

                # persona 在 AstrBot 中已被删除时，跳过该 persona
                # （LivingMemory 中可能残留已删除 persona 的记忆，压缩它无意义）
                try:
                    await self.context.persona_manager.get_persona(persona_id)
                except Exception:
                    logger.warning(
                        f"[LMPatch] persona '{persona_id}' 在 AstrBot 中不存在"
                        f"（可能已被删除），跳过该 persona 的压缩"
                    )
                    continue

                await self.store.update_init_state(
                    current_persona_id=persona_id,
                    current_persona_idx=idx,
                )

                # 循环压缩当前 persona 的低重要性记忆
                consecutive_failures = 0
                while True:
                    state = await self.store.get_init_state()
                    if state.get("status") != "running":
                        return

                    memories = await self.lm_client.list_low_importance_memories(
                        persona_id=persona_id,
                        importance_threshold=self.importance_threshold,
                        limit=INIT_COMPACT_BATCH,
                    )

                    if len(memories) < self.min_count:
                        logger.info(
                            f"[LMPatch] persona '{persona_id}' 低重要性记忆已全部压缩完，"
                            f"进入下一个 persona"
                        )
                        break

                    success = await self._compact_memories(persona_id, memories)
                    if success:
                        consecutive_failures = 0
                        total_compacted += len(memories)
                        await self.store.update_init_state(
                            total_compacted=total_compacted,
                        )
                    else:
                        consecutive_failures += 1
                        logger.warning(
                            f"[LMPatch] persona '{persona_id}' 一批压缩失败"
                            f"（连续第 {consecutive_failures} 次），跳过该批继续"
                        )
                        # 连续失败超过阈值时跳过当前 persona，避免死循环读取相同批次
                        if consecutive_failures >= _INIT_MAX_CONSECUTIVE_FAILURES:
                            logger.warning(
                                f"[LMPatch] persona '{persona_id}' 连续压缩失败 "
                                f"{consecutive_failures} 次，跳过该 persona 进入下一个"
                            )
                            break

            await self.store.complete_init(0, total_compacted)
            logger.info(f"[LMPatch] 记忆压缩初始化完成，共压缩 {total_compacted} 条记忆")
        except Exception as e:
            logger.error(f"[LMPatch] 记忆压缩初始化失败: {e}", exc_info=True)
            await self.store.cancel_init(str(e))

    async def cancel_compact_init(self) -> dict:
        """取消记忆压缩初始化。"""
        state = await self.store.get_init_state()
        if state.get("status") != "running" or state.get("type") != "compact":
            return {"success": False, "error": "当前无压缩初始化正在进行"}
        await self.store.cancel_init("用户主动取消")
        # 后台任务会在下一轮循环检测到 status != running 后退出
        logger.info("[LMPatch] 记忆压缩初始化已请求取消")
        return {"success": True, "message": "初始化取消请求已发送，后台任务将在当前批次完成后停止"}

    def _format_memories(self, memories: list[dict]) -> str:
        """格式化记忆列表为 LLM 可读文本，包含会话ID和人格风格摘要。

        优先使用 metadata.persona_summary（第一人称人格风格摘要），
        回退到 text（canonical_summary，中性检索版本）。
        """
        lines = []
        for m in memories:
            meta = m.get("metadata", {}) or {}
            importance = meta.get("importance", "?")
            created = m.get("created_at", "")
            session_id = meta.get("session_id", "未知")
            interaction_type = meta.get("interaction_type", "unknown")
            # 优先使用 persona_summary（第一人称人格风格），回退到 text
            persona_summary = meta.get("persona_summary", "")
            text = persona_summary if persona_summary else m.get("text", "")
            lines.append(
                f"[#{m.get('id', '?')}][会话:{session_id}]"
                f"[重要性:{importance}][类型:{interaction_type}][{created}] {text}"
            )
        return "\n".join(lines)
