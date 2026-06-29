"""LivingMemory 访问客户端：通过 weakref 接入 livingmemory 插件实例。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from astrbot.api import logger

try:
    import aiosqlite
except ImportError:
    aiosqlite = None

_LM_PLUGIN_NAME = "astrbot_plugin_livingmemory"
_LM_MODULE_PATH = f"{_LM_PLUGIN_NAME}.core.passive_group_capture"


class LMClient:
    """封装对 LivingMemory 插件的访问。

    通过 livingmemory 暴露的 weakref 入口 get_active_plugin() 获取插件实例，
    再经由 plugin.initializer.memory_engine 访问记忆引擎。

    不修改 livingmemory 任何代码，仅消费其已存在的（虽未文档化的）接口。
    """

    def __init__(self) -> None:
        self._cached_plugin: Any = None
        self._cache_time: float = 0.0
        self._cache_ttl: float = 30.0  # 缓存插件实例引用，30 秒内不重复获取

    # ------------------------------------------------------------------
    # 插件实例获取
    # ------------------------------------------------------------------

    def _try_import_get_active_plugin(self):
        """尝试导入 livingmemory 的 get_active_plugin 函数。"""
        try:
            import importlib
            mod = importlib.import_module(_LM_MODULE_PATH)
            return getattr(mod, "get_active_plugin", None)
        except ImportError:
            return None
        except Exception:
            logger.debug("[LMPatch] 导入 livingmemory 模块时出错", exc_info=True)
            return None

    async def get_plugin(self) -> Any:
        """获取 livingmemory 插件实例，带缓存。"""
        now = time.time()

        # 检查缓存
        if self._cached_plugin is not None and (now - self._cache_time) < self._cache_ttl:
            plugin = self._cached_plugin()
            if plugin is not None and not getattr(plugin, "_terminating", False):
                return plugin
            self._cached_plugin = None

        get_fn = self._try_import_get_active_plugin()
        if get_fn is None:
            return None

        plugin = get_fn()
        if plugin is None:
            return None

        if getattr(plugin, "_terminating", False):
            return None

        # 检查初始化完成
        initializer = getattr(plugin, "initializer", None)
        if initializer is None:
            return None
        if not getattr(initializer, "is_initialized", False):
            return None

        self._cached_plugin = plugin  # 这里存的是实例本身（weakref 解引用后的对象）
        self._cache_time = now
        return plugin

    async def get_memory_engine(self) -> Any:
        """获取 MemoryEngine 实例。"""
        plugin = await self.get_plugin()
        if plugin is None:
            return None
        initializer = getattr(plugin, "initializer", None)
        if initializer is None:
            return None
        return getattr(initializer, "memory_engine", None)

    async def is_available(self) -> bool:
        """检查 livingmemory 是否已加载并初始化完成。"""
        return await self.get_memory_engine() is not None

    # ------------------------------------------------------------------
    # 记忆查询（直读 SQLite，参考 livingmemory Page API 的做法）
    # ------------------------------------------------------------------

    async def _query_documents(
        self,
        where_clause: str,
        params: tuple,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """直接查询 livingmemory 的 documents 表。"""
        if aiosqlite is None:
            logger.warning("[LMPatch] aiosqlite 未安装，无法查询记忆")
            return []

        engine = await self.get_memory_engine()
        if engine is None:
            return []

        db_path = getattr(engine, "db_path", None)
        if not db_path:
            return []

        sql = (
            f"SELECT id, text, metadata, created_at "
            f"FROM documents WHERE {where_clause} "
            f"ORDER BY id ASC LIMIT ?"
        )
        try:
            async with aiosqlite.connect(db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(sql, (*params, limit))
                rows = await cursor.fetchall()
                result = []
                for row in rows:
                    meta = {}
                    raw_meta = row["metadata"]
                    if raw_meta:
                        try:
                            meta = json.loads(raw_meta)
                        except Exception:
                            meta = {}
                    result.append({
                        "id": row["id"],
                        "text": row["text"],
                        "metadata": meta,
                        "created_at": row["created_at"],
                    })
                return result
        except Exception as e:
            logger.warning(f"[LMPatch] 查询 livingmemory documents 表失败: {e}")
            return []

    async def list_memories_by_persona(
        self,
        persona_id: str,
        since_id: int = 0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """按 persona_id 列出记忆，支持增量读取（since_id 之后）。

        只读取 status 为 active 的记忆。
        """
        where = (
            "json_extract(metadata, '$.persona_id') = ? "
            "AND id > ? "
            "AND (json_extract(metadata, '$.status') = 'active' "
            "     OR json_extract(metadata, '$.status') IS NULL)"
        )
        return await self._query_documents(where, (persona_id, since_id), limit)

    async def list_low_importance_memories(
        self,
        persona_id: str,
        importance_threshold: float,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """列出指定 persona 下重要性低于阈值的记忆。"""
        where = (
            "json_extract(metadata, '$.persona_id') = ? "
            "AND json_extract(metadata, '$.importance') < ? "
            "AND (json_extract(metadata, '$.status') = 'active' "
            "     OR json_extract(metadata, '$.status') IS NULL)"
        )
        return await self._query_documents(
            where, (persona_id, importance_threshold), limit
        )

    async def get_max_memory_id(self, persona_id: str) -> int:
        """获取指定 persona 下当前最大的 memory id。

        用于首次初始化 checkpoint：人设补丁只关心"新增记忆是否触发人设演化"，
        历史记忆在人设形成时已体现，因此首次运行应将 checkpoint 推进到当前
        最大 id，避免一次性读取全部历史记忆（用户可能有数百条）。
        """
        if aiosqlite is None:
            return 0

        engine = await self.get_memory_engine()
        if engine is None:
            return 0

        db_path = getattr(engine, "db_path", None)
        if not db_path:
            return 0

        try:
            async with aiosqlite.connect(db_path) as db:
                cursor = await db.execute(
                    "SELECT MAX(id) FROM documents "
                    "WHERE json_extract(metadata, '$.persona_id') = ?",
                    (persona_id,),
                )
                row = await cursor.fetchone()
                if row is None or row[0] is None:
                    return 0
                return int(row[0])
        except Exception as e:
            logger.warning(f"[LMPatch] 获取 persona '{persona_id}' 最大 id 失败: {e}")
            return 0

    async def get_all_persona_ids(self) -> list[str]:
        """获取 livingmemory 中所有出现过的 persona_id（去重）。"""
        if aiosqlite is None:
            return []

        engine = await self.get_memory_engine()
        if engine is None:
            return []

        db_path = getattr(engine, "db_path", None)
        if not db_path:
            return []

        try:
            async with aiosqlite.connect(db_path) as db:
                cursor = await db.execute(
                    "SELECT DISTINCT json_extract(metadata, '$.persona_id') AS pid "
                    "FROM documents "
                    "WHERE json_extract(metadata, '$.persona_id') IS NOT NULL "
                    "AND json_extract(metadata, '$.persona_id') != ''"
                )
                rows = await cursor.fetchall()
                return [row[0] for row in rows if row[0]]
        except Exception as e:
            logger.warning(f"[LMPatch] 获取 persona_id 列表失败: {e}")
            return []

    # ------------------------------------------------------------------
    # 记忆写入（通过 MemoryEngine 实例方法）
    # ------------------------------------------------------------------

    async def add_memory(
        self,
        content: str,
        session_id: str | None = None,
        persona_id: str | None = None,
        importance: float = 0.5,
        metadata: dict | None = None,
    ) -> int | None:
        """添加一条新记忆，返回新记忆的 id。"""
        engine = await self.get_memory_engine()
        if engine is None:
            return None
        try:
            # 注入来源标记
            full_metadata = metadata or {}
            full_metadata["memory_origin"] = "lm_patch_compact"
            full_metadata["memory_type"] = "SUMMARY"

            new_id = await engine.add_memory(
                content=content,
                session_id=session_id,
                persona_id=persona_id,
                importance=importance,
                metadata=full_metadata,
            )
            return new_id
        except Exception as e:
            logger.warning(f"[LMPatch] 添加压缩记忆失败: {e}")
            return None

    async def delete_memory(self, memory_id: int) -> bool:
        """删除一条记忆。"""
        engine = await self.get_memory_engine()
        if engine is None:
            return False
        try:
            return await engine.delete_memory(memory_id)
        except Exception as e:
            logger.warning(f"[LMPatch] 删除记忆 {memory_id} 失败: {e}")
            return False

    async def batch_delete_memories(self, memory_ids: list[int]) -> int:
        """批量删除记忆，返回成功删除的条数。"""
        engine = await self.get_memory_engine()
        if engine is None:
            return 0
        try:
            return await engine.batch_delete_memories(memory_ids)
        except Exception as e:
            logger.warning(f"[LMPatch] 批量删除记忆失败: {e}")
            return 0
