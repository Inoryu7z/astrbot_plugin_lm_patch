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
            # 初始化
            (
                f"{PAGE_API_PREFIX}/init/state",
                self.get_init_state,
                ["GET"],
                "LMPatch init state",
            ),
            (
                f"{PAGE_API_PREFIX}/init/persona/start",
                self.start_persona_init,
                ["POST"],
                "LMPatch start persona init",
            ),
            (
                f"{PAGE_API_PREFIX}/init/compact/start",
                self.start_compact_init,
                ["POST"],
                "LMPatch start compact init",
            ),
            (
                f"{PAGE_API_PREFIX}/init/cancel",
                self.cancel_init,
                ["POST"],
                "LMPatch cancel init",
            ),
        ]

        for route, handler, methods, desc in routes:
            register(route, handler, methods, desc)

        logger.info(f"[LMPatch] 已注册 {len(routes)} 个 Web API 路由")

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _wrap_response(self, result: Any) -> dict:
        """将内部 {success, ...} 响应格式转换为 AstrBot 标准 {status, data} 格式。

        AstrBot 插件页面 bridge SDK 会自动解包标准响应：
        - 成功: {"status": "ok", "data": X} → bridge 将 X 传递给前端
        - 失败: {"status": "error", "message": M} → bridge 抛出 Error(M)

        内部代码使用 {success: true/false, ...} 格式，此方法负责转换。
        stalled（无法收敛）视为"软成功"：操作已完成但提案已标记为 stalled，
        前端需要读取 stalled 字段，因此放在 data 中返回而非作为错误。
        """
        if not isinstance(result, dict) or "success" not in result:
            return {"status": "ok", "data": result}

        if result.get("success") or result.get("stalled"):
            data = {k: v for k, v in result.items() if k != "success"}
            return {"status": "ok", "data": data}

        message = result.get("error") or result.get("message") or "操作失败"
        return {"status": "error", "message": str(message)}

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
            "is_init": bool(p.get("is_init", 0)),
            "init_batch": p.get("init_batch", 0),
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
        return self._wrap_response({
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
        })

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

        return self._wrap_response({
            "success": True,
            "data": [self._fmt_proposal(p) for p in proposals],
        })

    async def get_proposal(self):
        """获取单个提案详情。"""
        from astrbot.api.web import request

        try:
            proposal_id = int(request.query.get("id", "0"))
        except (TypeError, ValueError):
            return self._wrap_response({"success": False, "error": "id 参数无效"})
        if not proposal_id:
            return self._wrap_response({"success": False, "error": "缺少 id 参数"})

        proposal = await self.store.get_proposal(proposal_id)
        if not proposal:
            return self._wrap_response({"success": False, "error": "提案不存在"})

        return self._wrap_response({
            "success": True,
            "data": self._fmt_proposal(proposal),
        })

    async def approve_proposal(self):
        """审批通过提案。"""
        from astrbot.api.web import request

        data = await request.json(default={}) or {}
        try:
            proposal_id = int(data.get("id", 0))
        except (TypeError, ValueError):
            proposal_id = 0
        if not proposal_id:
            return self._wrap_response({"success": False, "error": "缺少 id"})

        return self._wrap_response(
            await self.persona_patcher.approve_proposal(proposal_id)
        )

    async def reject_proposal(self):
        """拒绝提案（终态）。"""
        from astrbot.api.web import request

        data = await request.json(default={}) or {}
        try:
            proposal_id = int(data.get("id", 0))
        except (TypeError, ValueError):
            proposal_id = 0
        if not proposal_id:
            return self._wrap_response({"success": False, "error": "缺少 id"})
        reason = str(data.get("reason", ""))

        return self._wrap_response(
            await self.persona_patcher.reject_proposal(proposal_id, reason)
        )

    async def reroll_proposal(self):
        """打回提案，LLM 结合理由重新提议。"""
        from astrbot.api.web import request

        data = await request.json(default={}) or {}
        try:
            proposal_id = int(data.get("id", 0))
        except (TypeError, ValueError):
            proposal_id = 0
        if not proposal_id:
            return self._wrap_response({"success": False, "error": "缺少 id"})
        reason = str(data.get("reason", "")).strip()
        if not reason:
            return self._wrap_response({"success": False, "error": "打回需要提供理由"})

        return self._wrap_response(
            await self.persona_patcher.reroll_proposal(proposal_id, reason)
        )

    async def restart_proposal(self):
        """重启 stalled 提案。"""
        from astrbot.api.web import request

        data = await request.json(default={}) or {}
        try:
            proposal_id = int(data.get("id", 0))
        except (TypeError, ValueError):
            proposal_id = 0
        if not proposal_id:
            return self._wrap_response({"success": False, "error": "缺少 id"})

        return self._wrap_response(
            await self.persona_patcher.restart_stalled_proposal(proposal_id)
        )

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

        return self._wrap_response({
            "success": True,
            "data": [self._fmt_snapshot(s) for s in snapshots],
        })

    async def rollback_snapshot(self):
        """回滚到指定快照。"""
        from astrbot.api.web import request

        data = await request.json(default={}) or {}
        try:
            snapshot_id = int(data.get("id", 0))
        except (TypeError, ValueError):
            snapshot_id = 0
        if not snapshot_id:
            return self._wrap_response({"success": False, "error": "缺少 id"})

        return self._wrap_response(
            await self.persona_patcher.rollback_to_snapshot(snapshot_id)
        )

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

        return self._wrap_response({
            "success": True,
            "data": [self._fmt_compact_log(log) for log in logs],
        })

    async def trigger_patch(self):
        """手动触发一次人设补丁周期。"""
        count = await self.scheduler.trigger_patch_now()
        return self._wrap_response({
            "success": True,
            "created_proposals": count,
            "message": f"人设补丁已触发，创建 {count} 个提案",
        })

    async def trigger_compact(self):
        """手动触发一次记忆压缩周期。"""
        count = await self.scheduler.trigger_compact_now()
        return self._wrap_response({
            "success": True,
            "compacted_count": count,
            "message": f"记忆压缩已触发，执行 {count} 次压缩",
        })

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    async def get_init_state(self):
        """获取初始化状态。"""
        state = await self.store.get_init_state()
        return self._wrap_response({
            "success": True,
            "data": {
                "type": state.get("type"),
                "status": state.get("status", "idle"),
                "current_persona_id": state.get("current_persona_id"),
                "current_persona_idx": state.get("current_persona_idx", 0),
                "current_batch": state.get("current_batch", 0),
                "total_personas": state.get("total_personas", 0),
                "total_processed": state.get("total_processed", 0),
                "total_compacted": state.get("total_compacted", 0),
                "started_at": self._fmt_time(state.get("started_at")),
                "updated_at": self._fmt_time(state.get("updated_at")),
                "finished_at": self._fmt_time(state.get("finished_at")),
                "error": state.get("error") or "",
            },
        })

    async def start_persona_init(self):
        """启动人设迭代初始化。"""
        return self._wrap_response(
            await self.persona_patcher.start_persona_init()
        )

    async def start_compact_init(self):
        """启动记忆压缩初始化。"""
        return self._wrap_response(
            await self.memory_compactor.start_compact_init()
        )

    async def cancel_init(self):
        """取消正在进行的初始化。"""
        state = await self.store.get_init_state()
        if state.get("status") != "running":
            return self._wrap_response({"success": False, "error": "当前无初始化正在进行"})
        init_type = state.get("type")
        if init_type == "persona":
            return self._wrap_response(
                await self.persona_patcher.cancel_persona_init()
            )
        elif init_type == "compact":
            return self._wrap_response(
                await self.memory_compactor.cancel_compact_init()
            )
        return self._wrap_response({
            "success": False,
            "error": f"未知的初始化类型: {init_type}",
        })
