"""功能 2：记忆压缩主流程。

低重要性记忆累积到指定条数时，由 LLM 归纳成更少的摘要记忆。
全自动执行：删旧加新，重新评估重要性，无需审批。
"""

from __future__ import annotations

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

        persona_ids = await self.lm_client.get_all_persona_ids()
        if not persona_ids:
            logger.info("[LMPatch] 未发现任何 persona_id，跳过记忆压缩周期")
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

        # 构造记忆文本
        memories_text = self._format_memories(memories)

        # 调用 LLM 压缩
        user_prompt = MEMORY_COMPACT_USER_TEMPLATE.format(
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

        for idx, summary in enumerate(summaries):
            if not isinstance(summary, dict):
                continue
            content = summary.get("content", "").strip()
            if not content:
                continue

            importance = summary.get("importance", 0.5)
            try:
                importance = float(importance)
            except (TypeError, ValueError):
                importance = 0.5
            # 限制重要性范围
            importance = max(0.0, min(1.0, importance))

            source_count = summary.get("source_count", 0)
            try:
                source_count = int(source_count)
            except (TypeError, ValueError):
                source_count = 0

            metadata = {
                "importance": importance,
                "source_count": source_count,
                "source_memory_ids": original_ids,
                "summary_reason": summary.get("reason", ""),
            }

            new_id = await self.lm_client.add_memory(
                content=content,
                session_id=None,
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

    def _format_memories(self, memories: list[dict]) -> str:
        """格式化记忆列表为 LLM 可读文本。"""
        lines = []
        for m in memories:
            text = m.get("text", "")
            meta = m.get("metadata", {}) or {}
            importance = meta.get("importance", "?")
            created = m.get("created_at", "")
            lines.append(
                f"[#{m.get('id', '?')}][重要性:{importance}][{created}] {text}"
            )
        return "\n".join(lines)
