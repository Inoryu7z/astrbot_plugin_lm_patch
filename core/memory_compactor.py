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

        return await self._compact_memories(persona_id, memories)

    async def _compact_memories(
        self, persona_id: str, memories: list[dict]
    ) -> bool:
        """对给定的一批低重要性记忆执行压缩。

        流程：先 add 新摘要全部成功后再 delete 旧记忆（避免删除后新增失败导致数据丢失）。
        返回是否成功执行了压缩。
        """
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

    # ------------------------------------------------------------------
    # 记忆压缩初始化（WebUI 触发，后台分批清完积压）
    # ------------------------------------------------------------------

    async def start_compact_init(self) -> dict:
        """启动记忆压缩初始化：后台分批压缩所有 persona 的低重要性记忆。

        从每个 persona 重要性最低的记忆开始，每次取 INIT_COMPACT_BATCH(10) 条压缩，
        循环直到该 persona 不足 min_count 条，然后进入下一个 persona。
        全程后台运行，完成后更新 init_state 为 completed。
        """
        if not await self.lm_client.is_available():
            return {"success": False, "error": "LivingMemory 不可用"}

        persona_ids = await self.lm_client.get_all_persona_ids()
        if not persona_ids:
            return {"success": False, "error": "未发现任何 persona_id，无需初始化"}

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

                await self.store.update_init_state(
                    current_persona_id=persona_id,
                    current_persona_idx=idx,
                )

                # 循环压缩当前 persona 的低重要性记忆
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
                        total_compacted += len(memories)
                        await self.store.update_init_state(
                            total_compacted=total_compacted,
                        )
                    else:
                        logger.warning(
                            f"[LMPatch] persona '{persona_id}' 一批压缩失败，跳过该批继续"
                        )

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
