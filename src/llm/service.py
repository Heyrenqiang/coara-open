"""Central LLM service — all application LLM calls should go through here."""

from __future__ import annotations

import asyncio
from typing import Any

from src.core.logger import logger
from src.llm.profile_resolver import ProfileResolver, build_default_profiles
from src.llm.profiles import Profile
from src.llm.provider import LLMProvider, LLMResponse
from src.llm.registry import provider_registry
from src.llm.request import LLMRequest, ResolvedLLMCall


class LLMService:
    """Routes LLM requests by profile and manages in-flight abort scope."""

    def __init__(self) -> None:
        self._resolver = ProfileResolver()
        self._configured = False
        self._active_providers: set[str] = set()
        self._abort_lock = asyncio.Lock()

    def configure(self, config_manager: Any) -> None:
        """Load llm_profiles from config (call after initialize_providers)."""
        config = config_manager.config
        defaults = build_default_profiles(
            default_provider=config.default_provider,
            default_model=config.default_model,
        )
        profiles = dict(config.llm_profiles)
        if not profiles:
            profiles = defaults
        else:
            for name, profile in defaults.items():
                if name not in profiles:
                    profiles[name] = profile
        default_profile = config.default_profile or Profile.DEFAULT
        self._resolver.configure(profiles, default_profile=default_profile)
        self._configured = True
        logger.debug(f"LLMService configured: {len(profiles)} profiles, default={default_profile}")

    @property
    def configured(self) -> bool:
        return self._configured

    def resolve(
        self,
        profile: str | None = None,
        *,
        provider_name: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ResolvedLLMCall:
        if not self._configured:
            self._ensure_minimal_defaults()
        return self._resolver.resolve(
            profile,
            provider_name=provider_name,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    def list_profiles(self) -> list[str]:
        if not self._configured:
            return []
        return self._resolver.list_profiles()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        from src.coara.turn_completion import (
            _await_interruptible,
            _complete_with_provider,
            _complete_with_provider_streaming,
        )

        resolved = self.resolve(
            request.profile,
            provider_name=request.provider,
            model=request.model,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
        )
        provider_key = resolved.provider_name
        self._active_providers.add(provider_key)
        try:
            if request.on_assistant_delta is not None:
                return await _complete_with_provider_streaming(
                    resolved.provider,
                    resolved.model,
                    request.tools,
                    request.system_prompt,
                    request.messages,
                    max_tokens=resolved.max_tokens,
                    temperature=resolved.temperature,
                    tool_choice=request.tool_choice,
                    signal=request.signal,
                    on_assistant_delta=request.on_assistant_delta,
                )
            operation = _complete_with_provider(
                resolved.provider,
                resolved.model,
                request.tools,
                request.system_prompt,
                request.messages,
                max_tokens=resolved.max_tokens,
                temperature=resolved.temperature,
                tool_choice=request.tool_choice,
            )
            if request.signal is None:
                return await operation
            return await _await_interruptible(operation, request.signal)
        finally:
            self._active_providers.discard(provider_key)

    def abort_active(self) -> None:
        """Abort HTTP clients for providers with in-flight LLMService requests."""
        for name in list(self._active_providers):
            try:
                provider_registry.get(name).abort()
            except Exception as exc:
                logger.warning(f"LLMService abort failed for provider '{name}': {exc}")

    async def close(self) -> None:
        """Close all provider HTTP clients and reset service state."""
        await provider_registry.close_all()
        self.reset_for_tests()

    def reset_for_tests(self) -> None:
        """Reset in-process profile state (tests). Does not close HTTP clients."""
        self._active_providers.clear()
        self._configured = False
        self._resolver = ProfileResolver()

    def configure_test_profiles(
        self,
        *,
        default_provider: str,
        default_model: str,
        extra_profiles: dict[str, Any] | None = None,
    ) -> None:
        """Wire default llm_profiles for unit tests (provider must already be registered)."""
        from src.core.types import LLMProfileConfig

        profiles = build_default_profiles(
            default_provider=default_provider,
            default_model=default_model,
        )
        if extra_profiles:
            for name, profile in extra_profiles.items():
                if isinstance(profile, LLMProfileConfig):
                    profiles[name] = profile
                elif isinstance(profile, dict):
                    profiles[name] = LLMProfileConfig(**profile)
        self._resolver.configure(profiles, default_profile=Profile.DEFAULT)
        self._configured = True

    def register_injected_provider(self, provider: LLMProvider, *, model: str | None = None) -> None:
        """Register a programmatically supplied provider and synthesize default profiles.

        Used when CoaraBase receives an explicit provider instance (tests or custom wiring).
        """
        from src.llm.provider import LLMProvider as _LLMProvider

        if not isinstance(provider, _LLMProvider):
            raise TypeError("provider must be an LLMProvider instance")
        name = provider.name
        if not provider_registry.has(name):
            provider_registry.register(name, provider)
        model_name = model or provider.default_model or "default"
        profiles = build_default_profiles(default_provider=name, default_model=model_name)
        self._resolver.configure(profiles, default_profile=Profile.DEFAULT)
        self._configured = True

    def _ensure_minimal_defaults(self) -> None:
        """Allow tests to resolve profiles before full configure (single fake provider)."""
        available = provider_registry.list_available()
        if not available:
            raise RuntimeError("LLMService not configured and no providers registered")
        name = available[0]
        provider = provider_registry.get(name)
        profiles = build_default_profiles(
            default_provider=name,
            default_model=provider.default_model,
        )
        self._resolver.configure(profiles, default_profile=Profile.DEFAULT)
        self._configured = True


llm_service = LLMService()
