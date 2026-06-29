"""astrbot_plugin_lm_patch 插件主入口。

负责插件注册、生命周期管理、组件装配与 Web API 注册。
不暴露聊天指令，所有操作通过 WebUI 进行。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
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
        """插件停止：先停止调度器，再关闭数据库连接。"""
        logger.info("[LMPatch] 插件正在停止...")

        # 1. 停止后台调度（cancel 所有 task）
        try:
            await self.scheduler.stop()
        except Exception as e:
            logger.warning(f"[LMPatch] 停止调度器时出错: {e}")

        # 2. 关闭本地数据库
        try:
            await self.store.close()
        except Exception as e:
            logger.warning(f"[LMPatch] 关闭数据库时出错: {e}")

        self._initialized = False
        logger.info("[LMPatch] 插件已停止")
