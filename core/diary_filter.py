"""日记过滤补丁：限制 livingmemory 检索结果中的日记条目数量。

daymind 插件每日生成虚拟日记并存入 livingmemory，这些日记虽然丰富了角色
生活感，但在记忆召回时若与真实对话记忆同等返回，会稀释真实记忆的占比、
让模型基于虚构内容作答。本模块通过 monkey-patch 包装 MemoryEngine.search_memories，
在检索结果返回前过滤掉多余的日记条目，仅保留指定数量（默认 1 条）。

识别口径：metadata 中 source=="daymind" 或 type=="diary" 任一命中即视为日记。
覆盖原始日记、压缩后的日记摘要（v1.1.5 起 lm_patch 压缩会保留 source 字段）、
以及未来可能出现的其他 daymind 衍生记忆。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from astrbot.api import logger

# 日记来源标记（daymind 写入 metadata 时使用，见 daymind diary_ops._build_diary_memory_metadata）
_DIARY_SOURCE = "daymind"
_DIARY_TYPE = "diary"

# 补丁标记属性名，用于避免重复包装与卸载时识别
_PATCH_MARKER = "_lmpatch_diary_filter_installed"
_ORIGINAL_REF = "_lmpatch_original_search_memories"


def _is_diary_entry(metadata: Any) -> bool:
    """判断一条记忆的 metadata 是否表示日记条目。

    Args:
        metadata: HybridResult.metadata，理论上是 dict

    Returns:
        True 表示是日记
    """
    if not isinstance(metadata, dict):
        return False
    return (
        metadata.get("source") == _DIARY_SOURCE
        or metadata.get("type") == _DIARY_TYPE
    )


def install_diary_filter(engine: Any, max_diaries: int = 1) -> bool:
    """在 memory_engine 实例上安装日记过滤补丁。

    包装 engine.search_memories，使其在返回结果中最多保留 max_diaries 条日记。
    若已安装且 max_diaries 未变化，跳过；若 max_diaries 变化，重新包装。

    Args:
        engine: LivingMemory 的 MemoryEngine 实例
        max_diaries: 返回结果中允许的最大日记条数（0 表示全部过滤）

    Returns:
        True 表示新安装或更新成功；False 表示安装失败（engine 为空或无 search_memories）
    """
    if engine is None:
        return False

    original_method = getattr(engine, "search_memories", None)
    if original_method is None:
        return False

    # 检查是否已安装
    if getattr(original_method, _PATCH_MARKER, False):
        # 已安装，检查 max_diaries 是否变化
        current_max = getattr(original_method, "_lmpatch_max_diaries", None)
        if current_max == max_diaries:
            return True
        # max_diaries 变化，先卸载再重装
        stored_original = getattr(original_method, _ORIGINAL_REF, None)
        if stored_original is not None:
            original_method = stored_original
        # fallthrough：用原始方法重新包装

    # 保留对原始方法的引用（original_method 此时一定是未包装的原始方法）
    _original_search = original_method

    async def _filtered_search_memories(
        query: str,
        k: int = 5,
        session_id: str | None = None,
        persona_id: str | None = None,
    ):
        """search_memories 包装器：过滤多余日记条目。"""
        try:
            results = await _original_search(
                query, k, session_id, persona_id
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # 原始方法异常直接抛出，不吞错
            raise

        if not results:
            return results

        try:
            diary_count = 0
            kept_diary_count = 0
            filtered: list = []
            for r in results:
                metadata = getattr(r, "metadata", None) or {}
                if _is_diary_entry(metadata):
                    diary_count += 1
                    if diary_count <= max_diaries:
                        filtered.append(r)
                        kept_diary_count += 1
                else:
                    filtered.append(r)

            if diary_count > max_diaries:
                logger.info(
                    f"[LMPatch] 日记过滤：原始 {len(results)} 条结果中含 "
                    f"{diary_count} 条日记，保留 {kept_diary_count} 条日记，"
                    f"最终返回 {len(filtered)} 条"
                )

            return filtered
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 过滤逻辑异常不应阻断检索，降级返回原始结果
            logger.warning(
                f"[LMPatch] 日记过滤异常，降级返回原始结果: {e}",
                exc_info=True,
            )
            return results

    # 标记已安装 + 保留原始方法引用 + 记录当前 max_diaries
    setattr(_filtered_search_memories, _PATCH_MARKER, True)
    setattr(_filtered_search_memories, _ORIGINAL_REF, _original_search)
    setattr(_filtered_search_memories, "_lmpatch_max_diaries", max_diaries)

    engine.search_memories = _filtered_search_memories
    return True


def uninstall_diary_filter(engine: Any) -> bool:
    """卸载日记过滤补丁，恢复原始 search_memories。

    Args:
        engine: LivingMemory 的 MemoryEngine 实例

    Returns:
        True 表示已卸载或未安装（无需操作）；False 表示卸载失败
    """
    if engine is None:
        return False

    current = getattr(engine, "search_memories", None)
    if current is None:
        return False

    if not getattr(current, _PATCH_MARKER, False):
        # 未安装，无需卸载
        return True

    original = getattr(current, _ORIGINAL_REF, None)
    if original is not None:
        engine.search_memories = original
        return True

    # 异常情况：标记为已安装但无原始引用，无法恢复
    logger.warning("[LMPatch] 日记过滤补丁卸载失败：找不到原始 search_memories 引用")
    return False


def is_filter_installed(engine: Any) -> bool:
    """检查日记过滤补丁是否已安装在 memory_engine 上。"""
    if engine is None:
        return False
    method = getattr(engine, "search_memories", None)
    if method is None:
        return False
    return bool(getattr(method, _PATCH_MARKER, False))


async def install_with_retries(
    get_engine_fn: Callable[[], Awaitable[Any]],
    max_diaries: int = 1,
    max_attempts: int = 20,
    interval: float = 30.0,
) -> bool:
    """带重试的安装：等待 livingmemory 初始化完成后安装补丁。

    livingmemory 的初始化是异步的（__init__ 中 schedule 后台任务），lm_patch
    的 initialize 可能在 livingmemory 就绪前完成。本函数周期性尝试获取
    memory_engine 并安装补丁，直到成功或达到最大尝试次数。

    Args:
        get_engine_fn: 返回 memory_engine 的异步函数（返回 None 表示未就绪）
        max_diaries: 日记最大保留数
        max_attempts: 最大尝试次数
        interval: 尝试间隔（秒）

    Returns:
        True 表示安装成功；False 表示所有尝试均失败
    """
    for attempt in range(1, max_attempts + 1):
        try:
            engine = await get_engine_fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"[LMPatch] 日记过滤补丁：第 {attempt} 次获取 memory_engine 失败: {e}")
            engine = None

        if engine is not None:
            if install_diary_filter(engine, max_diaries=max_diaries):
                logger.info(
                    f"[LMPatch] 日记过滤补丁已安装（max_diaries={max_diaries}，"
                    f"第 {attempt} 次尝试）"
                )
                return True

        if attempt < max_attempts:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    logger.warning(
        f"[LMPatch] 日记过滤补丁安装失败：{max_attempts} 次尝试后 livingmemory 仍未就绪"
    )
    return False
