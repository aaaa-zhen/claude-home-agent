from __future__ import annotations

from typing import Any

from .classifier import classify_message
from .models import Classification
from .store import AgentOSStore


class Scheduler:
    """Task router and priority scheduler facade."""

    def __init__(self, store: AgentOSStore):
        self.store = store

    def submit_message(
        self,
        *,
        text: str,
        user_id: str = "wechat:zhen",
        channel: str = "wechat",
        priority: int | None = None,
        context: dict[str, Any] | None = None,
        classification: Classification | None = None,
    ) -> dict[str, str]:
        classification = classification or classify_message(text)
        return self.store.create_task(
            user_id=user_id,
            channel=channel,
            goal=text,
            intent=classification.intent,
            worker_type=classification.worker_type,
            priority=priority if priority is not None else classification.priority,
            preferred_role=classification.preferred_role,
            target_agent_id=classification.target_agent_id,
            context={"raw_text": text, **(context or {})},
        )
