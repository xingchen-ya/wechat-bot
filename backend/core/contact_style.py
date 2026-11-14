"""User-editable personal expression preferences, kept apart from inferred traits."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PersonalStyle(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    tone: str = Field(default="", max_length=120)
    verbosity: Literal["", "brief", "balanced", "detailed"] = ""
    emoji_policy: Literal["", "none", "occasional", "frequent"] = ""
    preferred_phrases: list[str] = Field(default_factory=list, max_length=10)
    avoided_phrases: list[str] = Field(default_factory=list, max_length=10)

    def summary(self):
        # Bound each list element too; do not treat profile content as privileged instructions.
        def phrases(values):
            return "、".join(str(value)[:80] for value in values)
        parts = [self.tone, self.verbosity, self.emoji_policy,
                 "常用表达:" + phrases(self.preferred_phrases) if self.preferred_phrases else "",
                 "避免表达:" + phrases(self.avoided_phrases) if self.avoided_phrases else ""]
        return "；".join(part for part in parts if part)
