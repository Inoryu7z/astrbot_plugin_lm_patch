"""astrbot_plugin_lm_patch 插件主入口。

负责插件注册、生命周期管理、组件装配与 Web API 注册。
不暴露聊天指令，所有操作通过 WebUI 进行。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools, register

from .config import (
    PLUGIN_AUTHOR,
    PLUGIN_DESCRIPTION,
    PLUGIN_NAME,
    PLUGIN_REPO,
    PLUGIN_VERSION,
)
from .core.llm_helper import LLMHelper
from .core.lm_client import LMClient
from .core.memory_compactor import MemoryCompactor
from .core.persona_patcher import PersonaPatcher
from .core.scheduler import Scheduler
from .core.store import Store
from .core.webui import WebUI


@register(
    PLUGIN_NAME,
    PLUGIN_AUTHOR,
    PLUGIN_DESCRIPTION,
    PLUGIN_VERSION,
    PLUGIN_REPO,
)
class LMPatchPlugin(Star):
    """LivingMemory 记忆演化插件。

    功能：
    1. 人设补丁：周期性读取 LivingMemory 新增记忆，由 LLM 提议人设变更，
       WebUI 审批后写回 PersonaManager，并保存快照支持回滚。
    2. 记忆压缩：低重要性记忆累积到阈值时，由 LLM 归纳成更少的摘要记忆，
       重新评估重要性，全自动执行。
    """

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.config: dict[str, Any] = config or {}

        # 插件数据目录与数据库路径
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.db_path = f"{data_dir}/lm_patch.db"

        # 装配组件（生命周期相关操作留到 initialize/terminate）
        self.lm_client = LMClient()
        self.store = Store(self.db_path)
        self.llm_helper = LLMHelper(context)

        self.persona_patcher = PersonaPatcher(
            context=context,
            lm_client=self.lm_client,
            store=self.store,
            llm_helper=self.llm_helper,
            config=self.config,
        )

        self.memory_compactor = MemoryCompactor(
            context=context,
            lm_client=self.lm_client,
            store=self.store,
            llm_helper=self.llm_helper,
            config=self.config,
        )

        self.scheduler = Scheduler(
            persona_patcher=self.persona_patcher,
            memory_compactor=self.memory_compactor,
            config=self.config,
        )

        self.webui = WebUI(
            context=context,
            persona_patcher=self.persona_patcher,
            memory_compactor=self.memory_compactor,
            store=self.store,
            lm_client=self.lm_client,
            scheduler=self.scheduler,
            config=self.config,
        )

        # 标记是否完成初始化
        self._initialized = False

        logger.info(
            f"[LMPatch] {PLUGIN_NAME} v{PLUGIN_VERSION} 已加载，"
            f"等待 initialize 完成数据库与调度器启动"
        )

    async def initialize(self) -> None:
        """插件初始化：建立本地数据库、注册 Web API、启动后台调度。"""
        try:
            # 1. 初始化本地状态存储
            await self.store.initialize()

            # 2. 注册 Web API 路由
            self.webui.register()

            # 3. 启动后台周期调度
            await self.scheduler.start()

            self._initialized = True
            logger.info(
                f"[LMPatch] 初始化完成："
                f"人设补丁={'on' if self.scheduler.patch_enabled else 'off'}，"
                f"记忆压缩={'on' if self.scheduler.compact_enabled else 'off'}"
            )
        except Exception as e:
            logger.error(f"[LMPatch] 初始化失败: {e}", exc_info=True)

    async def terminate(self) -> None:
        """插件停止：先停止调度器与初始化任务，再关闭数据库连接。"""
        logger.info("[LMPatch] 插件正在停止...")

        # 1. 停止后台调度（cancel 所有 task）
        try:
            await self.scheduler.stop()
        except Exception as e:
            logger.warning(f"[LMPatch] 停止调度器时出错: {e}")

        # 2. 取消记忆压缩初始化后台任务（如有）
        try:
            task = getattr(self.memory_compactor, "_init_task", None)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[LMPatch] 取消压缩初始化任务时出错: {e}")

        # 3. 关闭本地数据库
        try:
            await self.store.close()
        except Exception as e:
            logger.warning(f"[LMPatch] 关闭数据库时出错: {e}")

        self._initialized = False
        logger.info("[LMPatch] 插件已停止")

    # ==================== 事件钩子（补丁） ====================

    @filter.after_message_sent()
    async def handle_session_reset_patch(self, event: AstrMessageEvent):
        """[Patch] 兼容 AstrBot 4.26+ 的 /reset 信号键名变更。

        AstrBot 在某次版本升级中把 /reset 的 extra 键名从 _clean_ltm_session
        改为 _clean_group_context_session，但 livingmemory 2.3.5 仍监听旧键名，
        导致 /reset 后 livingmemory 的会话清理钩子不触发，旧对话消息仍留在
        livingmemory 自己的数据库里，最终被总结进长期记忆。

        本钩子监听新键名 _clean_group_context_session，触发后调用 livingmemory
        的 event_handler.handle_session_reset 完成清理。
        """
        if not event.get_extra("_clean_group_context_session", False):
            return

        plugin = await self.lm_client.get_plugin()
        if plugin is None:
            logger.debug("[LMPatch] /reset 补丁：livingmemory 插件不可用，跳过")
            return

        event_handler = getattr(plugin, "event_handler", None)
        if event_handler is None:
            logger.debug("[LMPatch] /reset 补丁：livingmemory 无 event_handler，跳过")
            return

        try:
            await event_handler.handle_session_reset(event)
            logger.info("[LMPatch] 已通过补丁钩子触发 livingmemory 会话清理（_clean_group_context_session）")
        except Exception as e:
            logger.warning(f"[LMPatch] 通过补丁钩子触发 livingmemory 会话清理失败: {e}")

    @filter.after_message_sent()
    async def handle_forget_patch(self, event: AstrMessageEvent):
        """[Patch] 同步 amnesia 插件 /forget 命令到 livingmemory。

        amnesia 插件的 /forget 只清 AstrBot 的 conversation_manager（删除
        conversation_history 最新 N 轮），完全不触碰 livingmemory 的独立
        SQLite 数据库。导致被 /forget 的对话仍留在 livingmemory 中，仍被
        计入 unsummarized_rounds，最终被总结进长期记忆。

        本钩子在 /forget 执行后，同步删除 livingmemory 中对应的最新 N 轮
        消息，使其不计入轮次也不会被总结进记忆。

        ⚠️ 不兼容 amnesia 的 /cancel_forget 反悔机制：反悔时 AstrBot 侧
        恢复，但 livingmemory 侧不恢复（影响是"少记"，而非"记错"）。
        """
        # 检测是否是 /forget 命令（排除 /forget_status /forget_help /cancel_forget）
        message_str = (event.message_str or "").strip()
        if not message_str.startswith("/forget"):
            return
        if message_str.startswith("/forget_"):
            return
        if message_str == "/cancel_forget":
            return

        # 解析 round_count
        parts = message_str.split()
        if len(parts) == 1:
            round_count = 1
        elif len(parts) == 2:
            try:
                round_count = int(parts[1])
            except ValueError:
                return  # 非数字参数，amnesia 会报错，跳过
        else:
            return

        if not 1 <= round_count <= 10:
            return

        # 获取 livingmemory 插件实例
        plugin = await self.lm_client.get_plugin()
        if plugin is None:
            logger.debug("[LMPatch] /forget 补丁：livingmemory 插件不可用，跳过")
            return

        event_handler = getattr(plugin, "event_handler", None)
        if event_handler is None:
            logger.debug("[LMPatch] /forget 补丁：livingmemory 无 event_handler，跳过")
            return

        conv_mgr = getattr(event_handler, "conversation_manager", None)
        if conv_mgr is None:
            logger.debug("[LMPatch] /forget 补丁：livingmemory 无 conversation_manager，跳过")
            return

        store = getattr(conv_mgr, "store", None)
        if store is None or store.connection is None:
            logger.debug("[LMPatch] /forget 补丁：livingmemory store 不可用，跳过")
            return

        session_id = event.unified_msg_origin

        try:
            # 获取当前消息总数
            total = await store.get_message_count(session_id)
            if total == 0:
                logger.debug(f"[LMPatch] /forget 补丁：session={session_id} 无消息，跳过")
                return

            # 获取全部消息（按时间升序），用于从后往前找 N 个 (user, assistant) 对
            messages = await store.get_messages_range(
                session_id, offset=0, limit=total
            )

            # 从后往前查找 N 个 user+assistant 对的切割点
            split_index = len(messages)
            rounds_found = 0
            for i in range(len(messages) - 1, 0, -2):
                msg_curr = messages[i]
                msg_prev = messages[i - 1]
                if (
                    getattr(msg_curr, "role", None) == "assistant"
                    and getattr(msg_prev, "role", None) == "user"
                ):
                    rounds_found += 1
                    if rounds_found == round_count:
                        split_index = i - 1
                        break

            if split_index == len(messages):
                logger.warning(
                    f"[LMPatch] /forget 补丁：livingmemory 中只找到 {rounds_found} 轮"
                    f"可删除（请求 {round_count} 轮），session={session_id}"
                )
                return

            # 收集要删除的消息 ID
            to_delete = messages[split_index:]
            delete_ids = [m.id for m in to_delete if hasattr(m, "id")]
            if not delete_ids:
                return

            # 执行删除（加写锁，事务内删除消息 + 更新 session 计数）
            async with store._write_lock:
                placeholders = ",".join("?" * len(delete_ids))
                cursor = await store.connection.execute(
                    f"DELETE FROM messages WHERE id IN ({placeholders}) AND session_id = ?",
                    (*delete_ids, session_id),
                )
                deleted = max(0, cursor.rowcount)

                # 更新 session 的 message_count
                new_count = max(0, total - deleted)
                await store.connection.execute(
                    "UPDATE sessions SET message_count = ? WHERE session_id = ?",
                    (new_count, session_id),
                )
                await store.connection.commit()

            # 清除 conversation_manager 的 LRU 缓存（下次读取时重新加载）
            cache = getattr(conv_mgr, "_cache", None)
            cache_lock = getattr(conv_mgr, "_cache_lock", None)
            if cache is not None and cache_lock is not None:
                async with cache_lock:
                    if session_id in cache:
                        del cache[session_id]

            logger.info(
                f"[LMPatch] /forget 补丁：已从 livingmemory 删除 {deleted} 条消息"
                f"（{round_count} 轮），session={session_id}，剩余 {new_count} 条"
            )
        except Exception as e:
            logger.warning(f"[LMPatch] /forget 补丁：同步删除 livingmemory 消息失败: {e}")
