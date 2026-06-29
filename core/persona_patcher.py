"""功能 1：人设补丁主流程。

周期性读取 LivingMemory 新增记忆，由 LLM 判断是否需要更新人设。
- LLM 提议后创建待审提案
- WebUI 审批通过 → 保存快照 + 写回 PersonaManager
- WebUI 打回（附理由） → LLM 重提议（≤ max_reroll 轮，超过则 stalled）
- stalled 提案可手动重启
- 任意历史快照可回滚
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from ..prompts import (
    PERSONA_PATCH_SYSTEM_PROMPT,
    PERSONA_PATCH_USER_TEMPLATE,
    PERSONA_PATCH_REROLL_TEMPLATE,
    extract_json,
)
from .llm_helper import LLMHelper
from .lm_client import LMClient
from .store import Store

# 人设迭代初始化的批次大小（硬编码，用户确认）
INIT_BATCH_SIZE = 20


class PersonaPatcher:
    """人设补丁器。"""

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
    def max_memories(self) -> int:
        return int(self.config.get("persona_patch_max_memories", 50))

    @property
    def max_reroll(self) -> int:
        return int(self.config.get("persona_patch_max_reroll", 3))

    @property
    def enable_rollback(self) -> bool:
        return bool(self.config.get("persona_patch_enable_rollback", True))

    @property
    def llm_provider_id(self) -> str:
        return str(self.config.get("llm_provider_id", "") or "")

    # ------------------------------------------------------------------
    # 主流程：周期检查
    # ------------------------------------------------------------------

    async def run_patch_cycle(self) -> int:
        """对所有 persona 跑一遍补丁检查。返回创建的提案数。"""
        if not await self.lm_client.is_available():
            logger.warning("[LMPatch] LivingMemory 不可用，跳过人设补丁周期")
            return 0

        # 初始化进行中时跳过正常周期，避免与初始化流程冲突一口气跑完
        init_state = await self.store.get_init_state()
        if init_state.get("status") == "running":
            logger.info("[LMPatch] 初始化进行中，跳过人设补丁周期")
            return 0

        persona_ids = await self.lm_client.get_all_persona_ids()
        if not persona_ids:
            logger.info("[LMPatch] 未发现任何 persona_id，跳过人设补丁周期")
            return 0

        proposal_count = 0
        for persona_id in persona_ids:
            try:
                created = await self._patch_single_persona(persona_id)
                if created:
                    proposal_count += 1
            except Exception as e:
                logger.warning(
                    f"[LMPatch] 处理 persona '{persona_id}' 时出错: {e}"
                )
        logger.info(f"[LMPatch] 人设补丁周期完成，共创建 {proposal_count} 个提案")
        return proposal_count

    async def _patch_single_persona(self, persona_id: str) -> bool:
        """处理单个 persona 的补丁检查。返回是否创建了提案。"""
        # 获取当前 persona 对象
        try:
            persona = await self.context.persona_manager.get_persona(persona_id)
        except Exception as e:
            logger.warning(
                f"[LMPatch] persona '{persona_id}' 在 AstrBot 中不存在，跳过: {e}"
            )
            return False

        current_persona_text = persona.system_prompt or ""
        if not current_persona_text:
            logger.debug(f"[LMPatch] persona '{persona_id}' 人设文本为空，跳过")
            return False

        # 优先取真实人设名，回退到 persona_id
        persona_name = getattr(persona, "name", None) or persona_id

        # 增量读取新记忆
        since_id = await self.store.get_checkpoint(persona_id)
        memories = await self.lm_client.list_memories_by_persona(
            persona_id=persona_id,
            since_id=since_id,
            limit=self.max_memories,
        )

        if not memories:
            logger.debug(f"[LMPatch] persona '{persona_id}' 无新记忆，跳过")
            return False

        # 推进 checkpoint（无论 LLM 是否提议变更，都推进游标，避免重复消耗 token）
        last_id = max(m["id"] for m in memories)
        await self.store.update_checkpoint(persona_id, last_id)

        # 构造记忆文本
        memories_text = self._format_memories(memories)

        # 调用 LLM
        user_prompt = PERSONA_PATCH_USER_TEMPLATE.format(
            persona_text=current_persona_text,
            memory_count=len(memories),
            memories_text=memories_text,
        )
        raw = await self.llm_helper.chat(
            system_prompt=PERSONA_PATCH_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            provider_id=self.llm_provider_id,
        )
        if not raw:
            return False

        result = extract_json(raw)
        if result is None:
            logger.warning(
                f"[LMPatch] persona '{persona_id}' LLM 输出无法解析为 JSON"
            )
            return False

        if not result.get("need_change", False):
            logger.info(
                f"[LMPatch] persona '{persona_id}' LLM 判断无需变更人设: "
                f"{result.get('change_description', '无说明')}"
            )
            return False

        new_persona = result.get("new_persona", "").strip()
        if not new_persona:
            logger.warning(
                f"[LMPatch] persona '{persona_id}' LLM 提议变更但 new_persona 为空"
            )
            return False

        # 创建待审提案
        trigger_ids = [m["id"] for m in memories]
        changed_aspects = result.get("changed_aspects", []) or []
        change_desc = result.get("change_description", "")

        proposal_id = await self.store.add_proposal(
            persona_id=persona_id,
            persona_name=persona_name,
            original_persona=current_persona_text,
            proposed_persona=new_persona,
            change_description=change_desc,
            changed_aspects=changed_aspects if isinstance(changed_aspects, list) else [],
            trigger_memory_ids=trigger_ids,
        )
        logger.info(
            f"[LMPatch] persona '{persona_id}' 创建提案 #{proposal_id}: {change_desc}"
        )
        return True

    def _format_memories(self, memories: list[dict]) -> str:
        """格式化记忆列表为 LLM 可读文本，包含来源标记。"""
        lines = []
        for m in memories:
            text = m.get("text", "")
            meta = m.get("metadata", {}) or {}
            importance = meta.get("importance", "?")
            created = m.get("created_at", "")
            source = meta.get("source", "unknown")
            lines.append(
                f"[#{m.get('id', '?')}][来源:{source}][重要性:{importance}][{created}] {text}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 审批操作（由 WebUI 调用）
    # ------------------------------------------------------------------

    async def approve_proposal(self, proposal_id: int) -> dict:
        """审批通过：保存快照 + 写回 PersonaManager。"""
        proposal = await self.store.get_proposal(proposal_id)
        if not proposal:
            return {"success": False, "error": "提案不存在"}

        if proposal["status"] != "pending":
            return {
                "success": False,
                "error": f"提案状态为 {proposal['status']}，无法审批",
            }

        persona_id = proposal["persona_id"]
        new_persona_text = proposal["proposed_persona"]

        # 获取当前 persona（用于保持 begin_dialogs 等字段不变）
        try:
            current = await self.context.persona_manager.get_persona(persona_id)
        except Exception as e:
            return {
                "success": False,
                "error": f"persona '{persona_id}' 不存在: {e}",
            }

        # 保存快照（回滚用）
        snapshot_id = None
        if self.enable_rollback:
            snapshot_id = await self.store.save_snapshot(
                persona_id=persona_id,
                persona_name=proposal.get("persona_name"),
                snapshot_text=current.system_prompt or "",
                trigger_memory_ids=proposal.get("trigger_memory_ids"),
                change_description=proposal.get("change_description"),
                proposal_id=proposal_id,
            )

        # 写回 PersonaManager（只更新 system_prompt，保持其他字段不变）
        try:
            await self.context.persona_manager.update_persona(
                persona_id=persona_id,
                system_prompt=new_persona_text,
                begin_dialogs=current.begin_dialogs,
            )
        except Exception as e:
            return {"success": False, "error": f"更新 persona 失败: {e}"}

        # 更新提案状态
        await self.store.update_proposal_status(proposal_id, "approved")
        logger.info(
            f"[LMPatch] 提案 #{proposal_id} 已通过，persona '{persona_id}' 已更新"
            f"{'（快照 #' + str(snapshot_id) + '）' if snapshot_id else ''}"
        )

        # 如果是初始化迭代提案，审批通过后自动推进下一批
        init_next = None
        if proposal.get("is_init"):
            try:
                init_next = await self.continue_persona_init_after_approval(proposal_id)
            except Exception as e:
                logger.warning(f"[LMPatch] 初始化迭代推进失败: {e}", exc_info=True)
                init_next = {"success": False, "error": f"迭代推进失败: {e}"}

        return {
            "success": True,
            "snapshot_id": snapshot_id,
            "message": "人设已更新",
            "init_next": init_next,
        }

    # ------------------------------------------------------------------
    # 人设迭代初始化（WebUI 触发，分批处理历史记忆）
    # ------------------------------------------------------------------

    async def start_persona_init(self) -> dict:
        """启动人设迭代初始化：对每个 persona 按历史记忆顺序分批生成提案。

        流程：
        1. 检查互斥（init_state 必须 idle/completed/cancelled）
        2. 获取最近 30 天内有活跃记忆新增的 persona_ids（跳过已被用户抛弃的 persona）
        3. 每个 persona 按记忆 id 顺序，每批 INIT_BATCH_SIZE(20) 条
        4. 每批生成一个提案，WebUI 审批通过后自动推进下一批
        5. 所有 persona 处理完后，设置 checkpoint 为各自最大 id（正常周期只监控新增）
        """
        if not await self.lm_client.is_available():
            return {"success": False, "error": "LivingMemory 不可用"}

        # 只初始化最近 30 天活跃的 persona，跳过已被用户抛弃的
        persona_ids = await self.lm_client.get_active_persona_ids(days=30)
        if not persona_ids:
            return {"success": False, "error": "未发现近 30 天活跃的 persona_id，无需初始化"}

        if not await self.store.start_init("persona", len(persona_ids)):
            return {"success": False, "error": "已有初始化正在进行中，请先取消或等待完成"}

        logger.info(
            f"[LMPatch] 启动人设迭代初始化：共 {len(persona_ids)} 个 persona，"
            f"每批 {INIT_BATCH_SIZE} 条记忆"
        )
        return await self._process_next_init_batch(persona_ids)

    async def _process_next_init_batch(self, persona_ids: list[str]) -> dict:
        """处理下一批初始化记忆，生成提案。"""
        state = await self.store.get_init_state()
        persona_idx = state.get("current_persona_idx", 0)

        # 所有 persona 处理完
        if persona_idx >= len(persona_ids):
            # 为每个 persona 设置 checkpoint 为最大 id，使正常周期只监控新增
            for pid in persona_ids:
                max_id = await self.lm_client.get_max_memory_id(pid)
                if max_id > 0:
                    await self.store.update_checkpoint(pid, max_id)
            total = state.get("total_processed", 0)
            await self.store.complete_init(total)
            logger.info(f"[LMPatch] 人设迭代初始化完成，共处理 {total} 条记忆")
            return {
                "success": True,
                "completed": True,
                "total_processed": total,
                "message": "人设迭代初始化已完成",
            }

        persona_id = persona_ids[persona_idx]

        # 获取 persona 对象
        # persona 在 AstrBot 中已被删除时，跳过该 persona 继续下一个，
        # 不取消整个初始化流程（LivingMemory 中可能残留已删除 persona 的记忆）
        try:
            persona = await self.context.persona_manager.get_persona(persona_id)
        except Exception as e:
            logger.warning(
                f"[LMPatch] persona '{persona_id}' 在 AstrBot 中不存在"
                f"（可能已被删除），跳过该 persona 继续下一个: {e}"
            )
            await self.store.update_init_state(
                current_persona_idx=persona_idx + 1,
                processed_until_id=0,
                current_persona_id=None,
                current_batch=0,
            )
            return await self._process_next_init_batch(persona_ids)

        current_persona_text = persona.system_prompt or ""
        persona_name = getattr(persona, "name", None) or persona_id

        # 读取本批记忆
        processed_until_id = state.get("processed_until_id", 0)
        memories = await self.lm_client.list_memories_by_persona(
            persona_id=persona_id,
            since_id=processed_until_id,
            limit=INIT_BATCH_SIZE,
        )

        # 当前 persona 无更多记忆，进入下一个
        if not memories:
            logger.info(
                f"[LMPatch] persona '{persona_id}' 历史记忆已全部处理完，"
                f"进入下一个 persona"
            )
            await self.store.update_init_state(
                current_persona_idx=persona_idx + 1,
                processed_until_id=0,
                current_persona_id=None,
                current_batch=0,
            )
            return await self._process_next_init_batch(persona_ids)

        # 更新 init_state
        new_batch = state.get("current_batch", 0) + 1
        await self.store.update_init_state(
            current_persona_id=persona_id,
            current_batch=new_batch,
        )

        # 构造记忆文本并调用 LLM
        memories_text = self._format_memories(memories)
        user_prompt = PERSONA_PATCH_USER_TEMPLATE.format(
            persona_text=current_persona_text,
            memory_count=len(memories),
            memories_text=memories_text,
        )
        raw = await self.llm_helper.chat(
            system_prompt=PERSONA_PATCH_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            provider_id=self.llm_provider_id,
        )
        if not raw:
            await self.store.cancel_init("LLM 调用失败")
            return {"success": False, "error": "LLM 调用失败，初始化已取消"}

        result = extract_json(raw)
        if result is None:
            await self.store.cancel_init("LLM 输出无法解析为 JSON")
            return {"success": False, "error": "LLM 输出无法解析，初始化已取消"}

        trigger_ids = [m["id"] for m in memories]

        if not result.get("need_change", False):
            # LLM 认为本批无需变更，推进游标准备下一批
            last_id = max(trigger_ids)
            await self.store.update_init_state(
                processed_until_id=last_id,
                total_processed=state.get("total_processed", 0) + len(memories),
            )
            logger.info(
                f"[LMPatch] persona '{persona_id}' 迭代 {new_batch}："
                f"LLM 判断无需变更，跳过本批"
            )
            return await self._process_next_init_batch(persona_ids)

        new_persona = result.get("new_persona", "").strip()
        if not new_persona:
            await self.store.cancel_init("LLM 输出 new_persona 为空")
            return {"success": False, "error": "LLM 输出 new_persona 为空，初始化已取消"}

        # 创建待审提案（标记为 init）
        changed_aspects = result.get("changed_aspects", []) or []
        change_desc = result.get("change_description", "")

        proposal_id = await self.store.add_proposal(
            persona_id=persona_id,
            persona_name=persona_name,
            original_persona=current_persona_text,
            proposed_persona=new_persona,
            change_description=change_desc,
            changed_aspects=changed_aspects if isinstance(changed_aspects, list) else [],
            trigger_memory_ids=trigger_ids,
            is_init=True,
            init_batch=new_batch,
        )
        logger.info(
            f"[LMPatch] persona '{persona_id}' 初始化迭代 {new_batch}："
            f"创建提案 #{proposal_id}，等待审批"
        )
        return {
            "success": True,
            "proposal_id": proposal_id,
            "persona_name": persona_name,
            "batch": new_batch,
            "message": f"迭代 {new_batch} 已生成，等待审批",
        }

    async def continue_persona_init_after_approval(self, proposal_id: int) -> dict:
        """init 提案审批通过后，推进游标并生成下一批提案。"""
        proposal = await self.store.get_proposal(proposal_id)
        if not proposal:
            return {"success": False, "error": "提案不存在"}

        trigger_ids = proposal.get("trigger_memory_ids", [])
        if not trigger_ids:
            return {"success": False, "error": "提案无 trigger_memory_ids"}

        last_id = max(trigger_ids)
        state = await self.store.get_init_state()
        await self.store.update_init_state(
            processed_until_id=last_id,
            total_processed=state.get("total_processed", 0) + len(trigger_ids),
        )

        # 重新获取 persona_ids（可能中途有新增）
        persona_ids = await self.lm_client.get_all_persona_ids()
        if not persona_ids:
            await self.store.complete_init(state.get("total_processed", 0))
            return {"success": True, "completed": True}

        return await self._process_next_init_batch(persona_ids)

    async def cancel_persona_init(self) -> dict:
        """取消人设迭代初始化。"""
        state = await self.store.get_init_state()
        if state.get("status") != "running":
            return {"success": False, "error": "当前无初始化正在进行"}
        await self.store.cancel_init("用户主动取消")
        logger.info("[LMPatch] 人设迭代初始化已取消")
        return {"success": True, "message": "初始化已取消"}

    async def reject_proposal(self, proposal_id: int, reason: str = "") -> dict:
        """拒绝提案（终态，不写回人设）。

        如果是初始化迭代提案，拒绝后自动推进下一批（与审批通过一致），
        避免"拒绝提案 → 初始化卡住"的问题。
        """
        proposal = await self.store.get_proposal(proposal_id)
        if not proposal:
            return {"success": False, "error": "提案不存在"}

        if proposal["status"] != "pending":
            return {
                "success": False,
                "error": f"提案状态为 {proposal['status']}，无法拒绝",
            }

        await self.store.update_proposal_status(proposal_id, "rejected", reason)
        logger.info(f"[LMPatch] 提案 #{proposal_id} 已拒绝: {reason}")

        # 如果是初始化迭代提案，拒绝后也推进下一批（用户不认可演进方向，
        # 但初始化流程应继续处理后续记忆，而非卡住）
        init_next = None
        if proposal.get("is_init"):
            try:
                init_next = await self.continue_persona_init_after_approval(proposal_id)
            except Exception as e:
                logger.warning(f"[LMPatch] 初始化迭代推进失败: {e}", exc_info=True)
                init_next = {"success": False, "error": f"迭代推进失败: {e}"}

        return {
            "success": True,
            "message": "提案已拒绝",
            "init_next": init_next,
        }

    async def reroll_proposal(
        self, proposal_id: int, rejection_reason: str
    ) -> dict:
        """打回提案，LLM 结合理由重新提议。

        若 reroll 次数超过上限，标记为 stalled。
        """
        proposal = await self.store.get_proposal(proposal_id)
        if not proposal:
            return {"success": False, "error": "提案不存在"}

        if proposal["status"] != "pending":
            return {
                "success": False,
                "error": f"提案状态为 {proposal['status']}，无法打回",
            }

        # 递增 reroll 次数
        new_count = await self.store.increment_reroll(proposal_id)
        if new_count > self.max_reroll:
            await self.store.update_proposal_status(
                proposal_id, "stalled", rejection_reason
            )
            logger.warning(
                f"[LMPatch] 提案 #{proposal_id} 打回次数超过上限 "
                f"{self.max_reroll}，标记为 stalled"
            )
            return {
                "success": False,
                "stalled": True,
                "error": f"打回次数超过上限（{self.max_reroll}），提案已标记为无法收敛",
            }

        # 构造 reroll prompt（不需要重读记忆，基于原始人设+上次提议+打回理由）
        user_prompt = PERSONA_PATCH_REROLL_TEMPLATE.format(
            persona_text=proposal["original_persona"],
            previous_proposal=proposal["proposed_persona"],
            previous_description=proposal.get("change_description") or "",
            rejection_reason=rejection_reason,
        )

        raw = await self.llm_helper.chat(
            system_prompt=PERSONA_PATCH_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            provider_id=self.llm_provider_id,
        )
        if not raw:
            return {"success": False, "error": "LLM 调用失败"}

        result = extract_json(raw)
        if result is None:
            return {"success": False, "error": "LLM 输出无法解析为 JSON"}

        if not result.get("need_change", False):
            # LLM 改主意了，认为不需要变更
            await self.store.update_proposal_status(
                proposal_id, "rejected", "LLM 重议后认为无需变更"
            )
            return {
                "success": True,
                "withdrawn": True,
                "message": "LLM 重议后认为无需变更，提案已撤回",
            }

        new_persona = result.get("new_persona", "").strip()
        if not new_persona:
            return {"success": False, "error": "LLM 输出 new_persona 为空"}

        # 更新提案内容
        changed_aspects = result.get("changed_aspects", []) or []
        await self.store.update_proposal_content(
            proposal_id=proposal_id,
            proposed_persona=new_persona,
            change_description=result.get("change_description", ""),
            changed_aspects=changed_aspects
            if isinstance(changed_aspects, list)
            else [],
        )
        logger.info(
            f"[LMPatch] 提案 #{proposal_id} 已重提议（第 {new_count} 次）"
        )
        return {
            "success": True,
            "reroll_count": new_count,
            "message": "已重新提议",
        }

    async def restart_stalled_proposal(self, proposal_id: int) -> dict:
        """重启 stalled 提案：reroll_count 清零，用上次的打回理由重新提议。"""
        proposal = await self.store.get_proposal(proposal_id)
        if not proposal:
            return {"success": False, "error": "提案不存在"}

        if proposal["status"] != "stalled":
            return {
                "success": False,
                "error": f"提案状态为 {proposal['status']}，无需重启",
            }

        # 重置 reroll_count 和状态
        await self.store.reset_reroll(proposal_id)

        # 用上次的打回理由做一次 reroll
        rejection_reason = (
            proposal.get("rejection_reason")
            or "无具体理由，请重新审视上次提议"
        )

        user_prompt = PERSONA_PATCH_REROLL_TEMPLATE.format(
            persona_text=proposal["original_persona"],
            previous_proposal=proposal["proposed_persona"],
            previous_description=proposal.get("change_description") or "",
            rejection_reason=rejection_reason,
        )

        raw = await self.llm_helper.chat(
            system_prompt=PERSONA_PATCH_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            provider_id=self.llm_provider_id,
        )
        if not raw:
            return {"success": False, "error": "LLM 调用失败"}

        result = extract_json(raw)
        if result is None:
            return {"success": False, "error": "LLM 输出无法解析为 JSON"}

        if not result.get("need_change", False):
            await self.store.update_proposal_status(
                proposal_id, "rejected", "重启后 LLM 认为无需变更"
            )
            return {
                "success": True,
                "withdrawn": True,
                "message": "重启后 LLM 认为无需变更，提案已撤回",
            }

        new_persona = result.get("new_persona", "").strip()
        if not new_persona:
            return {"success": False, "error": "LLM 输出 new_persona 为空"}

        changed_aspects = result.get("changed_aspects", []) or []
        await self.store.update_proposal_content(
            proposal_id=proposal_id,
            proposed_persona=new_persona,
            change_description=result.get("change_description", ""),
            changed_aspects=changed_aspects
            if isinstance(changed_aspects, list)
            else [],
        )
        logger.info(f"[LMPatch] 提案 #{proposal_id} 已重启并重新提议")
        return {"success": True, "message": "已重启并重新提议"}

    async def rollback_to_snapshot(self, snapshot_id: int) -> dict:
        """回滚到指定快照版本的人设。"""
        snapshot = await self.store.get_snapshot(snapshot_id)
        if not snapshot:
            return {"success": False, "error": "快照不存在"}

        persona_id = snapshot["persona_id"]
        snapshot_text = snapshot["snapshot_text"]

        # 获取当前 persona（保存回滚前快照）
        try:
            current = await self.context.persona_manager.get_persona(persona_id)
        except Exception as e:
            return {
                "success": False,
                "error": f"persona '{persona_id}' 不存在: {e}",
            }

        # 保存当前人设为新快照（回滚前快照，便于撤销回滚）
        pre_rollback_snapshot = await self.store.save_snapshot(
            persona_id=persona_id,
            persona_name=snapshot.get("persona_name"),
            snapshot_text=current.system_prompt or "",
            change_description=f"回滚到快照 #{snapshot_id} 前的保存",
        )

        # 写回 PersonaManager
        try:
            await self.context.persona_manager.update_persona(
                persona_id=persona_id,
                system_prompt=snapshot_text,
                begin_dialogs=current.begin_dialogs,
            )
        except Exception as e:
            return {"success": False, "error": f"更新 persona 失败: {e}"}

        logger.info(
            f"[LMPatch] persona '{persona_id}' 已回滚到快照 #{snapshot_id}"
            f"（回滚前快照 #{pre_rollback_snapshot}）"
        )
        return {
            "success": True,
            "pre_rollback_snapshot_id": pre_rollback_snapshot,
            "message": f"已回滚到快照 #{snapshot_id}",
        }
