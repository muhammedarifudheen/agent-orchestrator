"""Write policy: no tool that changes the world runs unless this run was allowed to write, or a person approved it (D24).

Industry-agnostic like the rest of agent/: nothing here names a tool, a server or a domain. Whether a tool writes is read
from the metadata its server publishes (`writes`, `side_effect`, `idempotent`, `reversible`, `scope`, set by
`servers/_base.make_tool` and, for generated servers, by the spec). A tool that writes but did not declare `reversible` and
`scope`, or that did not say whether it writes at all, cannot be classified and is refused: that is not approvable,
because an approver cannot judge a blast radius nobody stated.

Default: refuse. A write runs only if the run was started with `allow_writes=True`, or a caller-supplied `approver` says
yes to THAT call, or the duty it belongs to declared `unattended: true` with a rationale. A refusal is a pending-approval
record (agent/approvals.py), never a crash and never a silent skip.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from agent.tool_registry import ToolRegistry

SCOPES = ("single", "batch", "external")
APPROVAL_REQUIRED = "WRITE_APPROVAL_REQUIRED"
METADATA_MISSING = "WRITE_METADATA_MISSING"


@dataclass(frozen=True)
class ToolSafety:
    """What the server said about one tool. `missing` is non-empty when a write tool cannot be described."""

    tool: str
    writes: bool | None  # None = the server did not say (treated as a write)
    side_effect: str | None = None
    idempotent: bool | None = None
    reversible: bool | None = None
    scope: str | None = None
    missing: tuple[str, ...] = ()

    @property
    def is_read(self) -> bool:
        return self.writes is False

    def summary(self) -> dict[str, Any]:
        """What an approver is shown about the blast radius of one call."""
        return {"writes": self.writes, "side_effect": self.side_effect, "idempotent": self.idempotent,
                "reversible": self.reversible, "scope": self.scope,
                "moves_money": self.side_effect == "money"}


def classify(registry: ToolRegistry, tool: str) -> ToolSafety:
    """Classify a registered tool from its published metadata, failing closed: conflicting or absent facts mean a write."""
    meta = registry.tool_meta(tool) if hasattr(registry, "tool_meta") else {}
    hint = registry.is_read_only(tool) if hasattr(registry, "is_read_only") else None
    declared = meta.get("writes") if isinstance(meta.get("writes"), bool) else None
    writes = declared
    if writes is None and isinstance(hint, bool):
        writes = not hint
    if writes is False and hint is False:  # metadata says read, the protocol annotation says write: believe the stricter
        writes = True
    missing: list[str] = []
    if writes is None:
        missing.append("writes")
    if writes is not False:
        if not isinstance(meta.get("reversible"), bool):
            missing.append("reversible")
        if meta.get("scope") not in SCOPES:
            missing.append("scope")
    return ToolSafety(tool, writes, meta.get("side_effect"), meta.get("idempotent") if isinstance(meta.get("idempotent"), bool) else None,
                      meta.get("reversible") if isinstance(meta.get("reversible"), bool) else None,
                      meta.get("scope") if meta.get("scope") in SCOPES else None, tuple(missing))


@dataclass
class WriteProposal:
    """One write the run wants to make, as shown to an approver."""

    tool: str
    arguments: dict[str, Any]
    safety: ToolSafety
    goal: str
    prior_steps: list[dict[str, Any]]
    duty: str | None = None

    def cost_summary(self) -> dict[str, Any]:
        return self.safety.summary()


@dataclass
class Decision:
    allowed: bool
    mode: str  # read | allow_writes | unattended | approver | pending | invalid
    code: str | None = None  # the failure code when refused
    reason: str = ""
    rationale: str | None = None  # the duty's unattended_rationale, when that is what allowed it
    extra: dict[str, Any] = field(default_factory=dict)


Approver = Callable[[WriteProposal], Any]  # returns bool, or an awaitable of bool


class WritePolicy:
    def __init__(self, allow_writes: bool = False, approver: Approver | None = None) -> None:
        self.allow_writes, self.approver = allow_writes, approver

    async def decide(self, proposal: WriteProposal, *, unattended_rationale: str | None = None) -> Decision:
        safety = proposal.safety
        if safety.is_read:
            return Decision(True, "read")
        if safety.missing:
            return Decision(False, "invalid", METADATA_MISSING,
                            f"{proposal.tool} writes but its server did not declare {' and '.join(safety.missing)}; "
                            "a write tool must state whether it writes, whether one call is reversible, and its scope "
                            f"({'/'.join(SCOPES)}). It was not run, and cannot be approved until it does.")
        if self.allow_writes:
            return Decision(True, "allow_writes", reason="the run was started with allow_writes=True")
        if unattended_rationale:
            return Decision(True, "unattended", reason="the duty declared unattended: true", rationale=unattended_rationale)
        if self.approver is not None:
            try:
                verdict = self.approver(proposal)
                if inspect.isawaitable(verdict):
                    verdict = await verdict
            except Exception as exc:  # an approver that fails is not an approval
                return Decision(False, "pending", APPROVAL_REQUIRED,
                                f"the approver failed ({type(exc).__name__}: {exc}); treated as not approved")
            if verdict is True:
                return Decision(True, "approver", reason="approved by the caller's approver")
            return Decision(False, "pending", APPROVAL_REQUIRED, "the approver did not approve this write")
        return Decision(False, "pending", APPROVAL_REQUIRED,
                        "no allow_writes, no approver and no unattended duty: a person must approve this write")
