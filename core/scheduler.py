"""周期调度器：管理人设补丁与记忆压缩的后台定时任务。"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger

from .memory_compactor import MemoryCompactor
from .persona_patcher import PersonaPatcher

# 启动延迟（秒）：插件加载后等待一段时间再开始周期，确保 livingmemory 完成初始化
_STARTUP_DELAY = 60


class Scheduler:
    """后台周期调度器。

    - 人设补丁：按 persona_patch_interval_hours 间隔执行
    - 记忆压缩：按 memory_compact_check_interval_hours 间隔检查
    两个任务相互独立，各自循环。
    """

    def __init__(
        self,
        persona_patcher: PersonaPatcher,
        memory_compactor: MemoryCompactor,
        config: dict,
    ) -> None:
        self.persona_patcher = persona_patcher
        self.memory_compactor = memory_compactor
        self.config = config
        self._tasks: list[asyncio.Task] = []
        self._running = False

    @property
    def patch_interval_seconds(self) -> int:
        hours = int(self.config.get("persona_patch_interval_hours", 168))
        return max(1, hours) * 3600

    @property
    def compact_interval_seconds(self) -> int:
        hours = int(self.config.get("memory_compact_check_interval_hours", 24))
        return max(1, hours) * 3600

    @property
    def patch_enabled(self) -> bool:
        return bool(self.config.get("persona_patch_enable", True))

    @property
    def compact_enabled(self) -> bool:
        return bool(self.config.get("memory_compact_enable", True))

    async def start(self) -> None:
        """启动后台调度任务。"""
        if self._running:
            return
        self._running = True

        if self.patch_enabled:
            task = asyncio.create_task(self._patch_loop())
            task.set_name("lm_patch_persona")
            self._tasks.append(task)
            logger.info(
                f"[LMPatch] 人设补丁调度已启动，间隔 {self.patch_interval_seconds // 3600} 小时"
            )
        else:
            logger.info("[LMPatch] 人设补丁未启用")

        if self.compact_enabled:
            task = asyncio.create_task(self._compact_loop())
            task.set_name("lm_patch_compact")
            self._tasks.append(task)
            logger.info(
                f"[LMPatch] 记忆压缩调度已启动，间隔 {self.compact_interval_seconds // 3600} 小时"
            )
        else:
            logger.info("[LMPatch] 记忆压缩未启用")

    async def stop(self) -> None:
        """停止所有调度任务。"""
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._tasks.clear()
        logger.info("[LMPatch] 调度器已停止")

    async def _patch_loop(self) -> None:
        """人设补丁周期循环。"""
        await asyncio.sleep(_STARTUP_DELAY)
        while self._running:
            try:
                await self.persona_patcher.run_patch_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[LMPatch] 人设补丁周期出错: {e}")
            await asyncio.sleep(self.patch_interval_seconds)

    async def _compact_loop(self) -> None:
        """记忆压缩周期循环。"""
        await asyncio.sleep(_STARTUP_DELAY)
        while self._running:
            try:
                await self.memory_compactor.run_compact_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[LMPatch] 记忆压缩周期出错: {e}")
            await asyncio.sleep(self.compact_interval_seconds)

    async def trigger_patch_now(self) -> int:
        """手动触发一次人设补丁。返回创建的提案数。"""
        return await self.persona_patcher.run_patch_cycle()

    async def trigger_compact_now(self) -> int:
        """手动触发一次记忆压缩。返回执行的压缩次数。"""
        return await self.memory_compactor.run_compact_cycle()

    def is_running(self) -> bool:
        return self._running
