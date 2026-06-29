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
        return {
            "success": True,
            "snapshot_id": snapshot_id,
            "message": "人设已更新",
        }

    async def reject_proposal(self, proposal_id: int, reason: str = "") -> dict:
        """拒绝提案（终态，不写回人设）。"""
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
        return {"success": True, "message": "提案已拒绝"}

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
