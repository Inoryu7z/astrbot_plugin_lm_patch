"""Web API 注册与处理：提案审批、快照回滚、压缩日志、手动触发。"""

from __future__ import annotations

import json
import time
from typing import Any

from astrbot.api import logger

from .lm_client import LMClient
from .memory_compactor import MemoryCompactor
from .persona_patcher import PersonaPatcher
from .scheduler import Scheduler
from .store import Store

PLUGIN_NAME = "astrbot_plugin_lm_patch"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/page"


def _parse_json_field(value: Any) -> Any:
    """尝试把可能是 JSON 字串的字段解析为 Python 对象。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return []
    if value is None:
        return []
    return value


class WebUI:
    """Web API 注册与请求处理。"""

    def __init__(
        self,
        context: Any,
        persona_patcher: PersonaPatcher,
        memory_compactor: MemoryCompactor,
        store: Store,
        lm_client: LMClient,
        scheduler: Scheduler,
        config: dict,
    ) -> None:
        self.context = context
        self.persona_patcher = persona_patcher
        self.memory_compactor = memory_compactor
        self.store = store
        self.lm_client = lm_client
        self.scheduler = scheduler
        self.config = config

    def register(self) -> None:
        """注册所有 Web API 路由。"""
        register = self.context.register_web_api

        routes = [
            # 状态
            (
                f"{PAGE_API_PREFIX}/status",
                self.get_status,
                ["GET"],
                "LMPatch status",
            ),
            # 提案
            (
                f"{PAGE_API_PREFIX}/proposals",
                self.list_proposals,
                ["GET"],
                "LMPatch proposals",
            ),
            (
                f"{PAGE_API_PREFIX}/proposal",
                self.get_proposal,
                ["GET"],
                "LMPatch proposal detail",
            ),
            (
                f"{PAGE_API_PREFIX}/proposal/approve",
                self.approve_proposal,
                ["POST"],
                "LMPatch approve proposal",
            ),
            (
                f"{PAGE_API_PREFIX}/proposal/reject",
                self.reject_proposal,
                ["POST"],
                "LMPatch reject proposal",
            ),
            (
                f"{PAGE_API_PREFIX}/proposal/reroll",
                self.reroll_proposal,
                ["POST"],
                "LMPatch reroll proposal",
            ),
            (
                f"{PAGE_API_PREFIX}/proposal/restart",
                self.restart_proposal,
                ["POST"],
                "LMPatch restart stalled proposal",
            ),
            # 快照
            (
                f"{PAGE_API_PREFIX}/snapshots",
                self.list_snapshots,
                ["GET"],
                "LMPatch snapshots",
            ),
            (
                f"{PAGE_API_PREFIX}/snapshot/rollback",
                self.rollback_snapshot,
                ["POST"],
                "LMPatch rollback to snapshot",
            ),
            # 压缩日志
            (
                f"{PAGE_API_PREFIX}/compact-log",
                self.list_compact_log,
                ["GET"],
                "LMPatch compaction log",
            ),
            # 手动触发
            (
                f"{PAGE_API_PREFIX}/trigger/patch",
                self.trigger_patch,
                ["POST"],
                "LMPatch trigger patch cycle",
            ),
            (
                f"{PAGE_API_PREFIX}/trigger/compact",
                self.trigger_compact,
                ["POST"],
                "LMPatch trigger compact cycle",
            ),
        ]

        for route, handler, methods, desc in routes:
            register(route, handler, methods, desc)

        logger.info(f"[LMPatch] 已注册 {len(routes)} 个 Web API 路由")

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _fmt_time(self, ts: float | None) -> str:
        if not ts:
            return ""
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        except Exception:
            return str(ts)

    def _fmt_proposal(self, p: dict) -> dict:
        return {
            "id": p["id"],
            "persona_id": p["persona_id"],
            "persona_name": p.get("persona_name") or p["persona_id"],
            "original_persona": p["original_persona"],
            "proposed_persona": p["proposed_persona"],
            "change_description": p.get("change_description") or "",
            "changed_aspects": _parse_json_field(p.get("changed_aspects")),
            "status": p["status"],
            "rejection_reason": p.get("rejection_reason") or "",
            "reroll_count": p.get("reroll_count", 0),
            "trigger_memory_ids": _parse_json_field(p.get("trigger_memory_ids")),
            "created_at": self._fmt_time(p.get("created_at")),
            "updated_at": self._fmt_time(p.get("updated_at")),
        }

    def _fmt_snapshot(self, s: dict) -> dict:
        return {
            "id": s["id"],
            "persona_id": s["persona_id"],
            "persona_name": s.get("persona_name") or s["persona_id"],
            "snapshot_text": s["snapshot_text"],
            "created_at": self._fmt_time(s.get("created_at")),
            "trigger_memory_ids": _parse_json_field(s.get("trigger_memory_ids")),
            "change_description": s.get("change_description") or "",
            "proposal_id": s.get("proposal_id"),
        }

    def _fmt_compact_log(self, log: dict) -> dict:
        return {
            "id": log["id"],
            "persona_id": log["persona_id"],
            "deleted_ids": _parse_json_field(log.get("deleted_ids")),
            "created_ids": _parse_json_field(log.get("created_ids")),
            "deleted_count": log.get("deleted_count", 0),
            "created_count": log.get("created_count", 0),
            "created_at": self._fmt_time(log.get("created_at")),
        }

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def get_status(self):
        """获取插件状态。"""
        lm_available = await self.lm_client.is_available()
        return {
            "success": True,
            "data": {
                "lm_available": lm_available,
                "scheduler_running": self.scheduler.is_running(),
                "patch_enabled": bool(self.config.get("persona_patch_enable", True)),
                "compact_enabled": bool(
                    self.config.get("memory_compact_enable", True)
                ),
                "patch_interval_hours": int(
                    self.config.get("persona_patch_interval_hours", 168)
                ),
                "compact_interval_hours": int(
                    self.config.get("memory_compact_check_interval_hours", 24)
                ),
            },
        }

    async def list_proposals(self):
        """获取提案列表，支持 status 过滤。"""
        from astrbot.api.web import request

        status = request.query.get("status", "") or ""
        try:
            limit = int(request.query.get("limit", "50"))
        except (TypeError, ValueError):
            limit = 50

        if status:
            proposals = await self.store.list_all_proposals(
                status=status, limit=limit
            )
        else:
            proposals = await self.store.list_all_proposals(limit=limit)

        return {
            "success": True,
            "data": [self._fmt_proposal(p) for p in proposals],
            "total": len(proposals),
        }

    async def get_proposal(self):
        """获取单个提案详情。"""
        from astrbot.api.web import request

        try:
            proposal_id = int(request.query.get("id", "0"))
        except (TypeError, ValueError):
            return {"success": False, "error": "id 参数无效"}
        if not proposal_id:
            return {"success": False, "error": "缺少 id 参数"}

        proposal = await self.store.get_proposal(proposal_id)
        if not proposal:
            return {"success": False, "error": "提案不存在"}

        return {"success": True, "data": self._fmt_proposal(proposal)}

    async def approve_proposal(self):
        """审批通过提案。"""
        from astrbot.api.web import request

        data = await request.json(default={}) or {}
        try:
            proposal_id = int(data.get("id", 0))
        except (TypeError, ValueError):
            proposal_id = 0
        if not proposal_id:
            return {"success": False, "error": "缺少 id"}

        return await self.persona_patcher.approve_proposal(proposal_id)

    async def reject_proposal(self):
        """拒绝提案（终态）。"""
        from astrbot.api.web import request

        data = await request.json(default={}) or {}
        try:
            proposal_id = int(data.get("id", 0))
        except (TypeError, ValueError):
            proposal_id = 0
        if not proposal_id:
            return {"success": False, "error": "缺少 id"}
        reason = str(data.get("reason", ""))

        return await self.persona_patcher.reject_proposal(proposal_id, reason)

    async def reroll_proposal(self):
        """打回提案，LLM 结合理由重新提议。"""
        from astrbot.api.web import request

        data = await request.json(default={}) or {}
        try:
            proposal_id = int(data.get("id", 0))
        except (TypeError, ValueError):
            proposal_id = 0
        if not proposal_id:
            return {"success": False, "error": "缺少 id"}
        reason = str(data.get("reason", "")).strip()
        if not reason:
            return {"success": False, "error": "打回需要提供理由"}

        return await self.persona_patcher.reroll_proposal(proposal_id, reason)

    async def restart_proposal(self):
        """重启 stalled 提案。"""
        from astrbot.api.web import request

        data = await request.json(default={}) or {}
        try:
            proposal_id = int(data.get("id", 0))
        except (TypeError, ValueError):
            proposal_id = 0
        if not proposal_id:
            return {"success": False, "error": "缺少 id"}

        return await self.persona_patcher.restart_stalled_proposal(proposal_id)

    async def list_snapshots(self):
        """获取快照列表，支持 persona_id 过滤。"""
        from astrbot.api.web import request

        persona_id = request.query.get("persona_id", "") or ""
        try:
            limit = int(request.query.get("limit", "20"))
        except (TypeError, ValueError):
            limit = 20

        if persona_id:
            snapshots = await self.store.list_snapshots(
                persona_id=persona_id, limit=limit
            )
        else:
            snapshots = await self.store.list_snapshots(limit=limit)

        return {
            "success": True,
            "data": [self._fmt_snapshot(s) for s in snapshots],
            "total": len(snapshots),
        }

    async def rollback_snapshot(self):
        """回滚到指定快照。"""
        from astrbot.api.web import request

        data = await request.json(default={}) or {}
        try:
            snapshot_id = int(data.get("id", 0))
        except (TypeError, ValueError):
            snapshot_id = 0
        if not snapshot_id:
            return {"success": False, "error": "缺少 id"}

        return await self.persona_patcher.rollback_to_snapshot(snapshot_id)

    async def list_compact_log(self):
        """获取压缩日志，支持 persona_id 过滤。"""
        from astrbot.api.web import request

        persona_id = request.query.get("persona_id", "") or ""
        try:
            limit = int(request.query.get("limit", "20"))
        except (TypeError, ValueError):
            limit = 20

        if persona_id:
            logs = await self.store.list_compaction_log(
                persona_id=persona_id, limit=limit
            )
        else:
            logs = await self.store.list_compaction_log(limit=limit)

        return {
            "success": True,
            "data": [self._fmt_compact_log(log) for log in logs],
            "total": len(logs),
        }

    async def trigger_patch(self):
        """手动触发一次人设补丁周期。"""
        count = await self.scheduler.trigger_patch_now()
        return {
            "success": True,
            "created_proposals": count,
            "message": f"人设补丁已触发，创建 {count} 个提案",
        }

    async def trigger_compact(self):
        """手动触发一次记忆压缩周期。"""
        count = await self.scheduler.trigger_compact_now()
        return {
            "success": True,
            "compacted_count": count,
            "message": f"记忆压缩已触发，执行 {count} 次压缩",
        }
