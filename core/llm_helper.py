"""LLM 调用辅助：统一处理 provider 选择与文本对话调用。"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger


class LLMHelper:
    """封装 LLM provider 的选择与调用。

    provider 选择优先级：
    1. 配置中指定的 llm_provider_id
    2. AstrBot 当前默认 provider（fallback，并记录警告）
    """

    def __init__(self, context: Any) -> None:
        self.context = context

    async def get_provider(self, provider_id: str = "") -> Any | None:
        """获取 LLM provider 实例。"""
        if provider_id:
            prov = self.context.get_provider_by_id(provider_id)
            if prov is not None:
                return prov
            logger.warning(
                f"[LMPatch] 配置的 LLM provider '{provider_id}' 不存在，回退到默认 provider"
            )

        prov = self.context.get_using_provider()
        if prov is None:
            logger.warning("[LMPatch] 未找到可用的默认 LLM provider，跳过本次任务")
            return None

        if not provider_id:
            logger.warning(
                "[LMPatch] 未配置专用 LLM，正在使用默认 provider（建议在插件配置中指定专用 LLM）"
            )
        return prov

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        provider_id: str = "",
    ) -> str | None:
        """调用 LLM 发送一次对话，返回纯文本响应。

        失败时返回 None。调用方需自行处理 None。
        """
        prov = await self.get_provider(provider_id)
        if prov is None:
            return None
        try:
            resp = await prov.text_chat(
                prompt=user_prompt,
                system_prompt=system_prompt,
            )
            text = resp.completion_text
            if not text:
                logger.warning("[LMPatch] LLM 返回空响应")
                return None
            return text
        except Exception as e:
            logger.warning(f"[LMPatch] LLM 调用失败: {e}")
            return None
