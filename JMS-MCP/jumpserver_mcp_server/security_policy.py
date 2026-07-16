"""Layered command security policy (design.md Decision 10/11).

Every command passes a layered evaluation:

- **Layer 0 — Tier-1 destructive floor (always on, mode-independent):** a regex
  set for catastrophic operations (``rm -rf /``, ``mkfs*``, ``dd ... of=/dev/sd*``).
  A match is a hard reject with no override — evaluated first in *both* modes,
  so even a command mistakenly whitelisted is still blocked here.
- **Main gate — ``policy_mode`` (default ``blacklist``):**
    - ``blacklist``: default-allow. After Layer 0, a Tier-2 risky regex match
      returns ``pending_approval``; everything else runs.
    - ``whitelist``: default-deny. After Layer 0, the command must match the
      allowlist regex set to run; a non-match is rejected outright.

The policy config is **admin-only** — loaded from a file/env at startup and
never modified by any MCP tool (there is intentionally no setter exposed).

Session-scoped exemptions and pre-supplied allow lists let an approved (or
trusted-automation) command skip the Tier-2 prompt for *exactly the triggered
regex*, for the current session only.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from logging import getLogger
from pathlib import Path
from typing import Any, Iterable

from .config import settings

logger = getLogger(__name__)


class Decision(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"            # Tier-1 hard block, or whitelist deny
    PENDING_APPROVAL = "pending_approval"  # Tier-2 risky


# --- Default patterns (a conservative floor; admin config can extend) ---------

# Tier-1: catastrophic, no override, ever.
_DEFAULT_TIER1: tuple[str, ...] = (
    r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+/(\s|$)",  # rm -rf /
    r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*\s+/(\s|$)",  # rm -fr /
    r"\bmkfs\b",                                   # any mkfs
    r"\bdd\b.*\bof=/dev/(sd|nvme|vd|hd|xvd)",   # dd to a raw disk
    r":\(\)\s*\{\s*:\|\s*:&\s*\}\s*;\s*:",      # fork bomb
    r">\s*/dev/(sd|nvme|vd|hd|xvd)[a-z]?\b",     # overwrite raw disk
    r"\bwipefs\b",
    r"\bshred\b\s+(-[a-zA-Z]*\s+)*/dev/",
    r"\bchmod\b\s+(-R\s+)?0?777\s+/(\s|$)",      # chmod 777 /
    r"\bchown\b\s+(-R\s+)?\S+\s+/(\s|$)",        # chown ... /
)

# Tier-2: risky / state-modifying; requires human approval (blacklist mode).
_DEFAULT_TIER2: tuple[str, ...] = (
    r"\brm\s+(-[a-zA-Z]*\s+)*-?[a-zA-Z]*f",     # rm -f ... (non-root)
    r"\brm\s+-r\b",                                # recursive rm (non-root)
    r"\bmv\b\s+\S+\s+/",                          # move into root-level dirs
    r">\s*/etc/",                                  # overwrite a config file
    r"\b(systemctl|service)\s+(stop|disable|mask)\b",
    r"\bkill(all)?\b\s+-9\b",
    r"\biptables\b",
    r"\btc\b\s+qdisc\b",
    r"\b(insmod|rmmod|modprobe)\b",
    r"\b(useradd|userdel|usermod|groupadd|groupdel|passwd)\b",
    r"\btruncate\b",
    r"echo\s+.*>\s*\S",                            # echo '' > file (clobber)
)


@dataclass
class PolicyConfig:
    """Admin-managed policy configuration (never editable via MCP tools)."""

    mode: str = "blacklist"
    tier1: list[str] = field(default_factory=lambda: list(_DEFAULT_TIER1))
    tier2: list[str] = field(default_factory=lambda: list(_DEFAULT_TIER2))
    whitelist: list[str] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: str | Path) -> "PolicyConfig":
        """Load admin policy JSON. Tier-1 defaults are always merged in as a floor."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        tier1 = list(_DEFAULT_TIER1) + list(data.get("tier1") or [])
        return cls(
            mode=str(data.get("mode") or data.get("policy_mode") or "blacklist"),
            tier1=tier1,
            tier2=list(data.get("tier2") or list(_DEFAULT_TIER2)),
            whitelist=list(data.get("whitelist") or []),
        )

    @classmethod
    def load(cls) -> "PolicyConfig":
        """Load from the admin policy file if present, else defaults + env mode."""
        cfg_path = getattr(settings, "policy_config_path", "") or ""
        if cfg_path and Path(cfg_path).is_file():
            cfg = cls.from_file(cfg_path)
        else:
            cfg = cls(mode=settings.policy_mode)
        # The Tier-1 floor can never be emptied.
        if not cfg.tier1:
            cfg.tier1 = list(_DEFAULT_TIER1)
        return cfg


@dataclass
class PolicyResult:
    decision: Decision
    tier: int | None = None              # 1, 2, or None
    matched_pattern: str | None = None   # the triggered regex (exemption key)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "tier": self.tier,
            "matched_pattern": self.matched_pattern,
            "message": self.message,
        }


class PolicyEngine:
    """Evaluate commands against the layered policy. Immutable after construction."""

    def __init__(self, config: PolicyConfig | None = None) -> None:
        self._config = config or PolicyConfig.load()
        self._tier1 = [re.compile(p) for p in self._config.tier1]
        self._tier2 = [re.compile(p) for p in self._config.tier2]
        self._whitelist = [re.compile(p) for p in self._config.whitelist]
        self._tier1_src = list(self._config.tier1)
        self._tier2_src = list(self._config.tier2)
        self._whitelist_src = list(self._config.whitelist)

    @property
    def mode(self) -> str:
        return self._config.mode

    @staticmethod
    def _first_match(
        command: str, patterns: Iterable[re.Pattern[str]], sources: list[str]
    ) -> str | None:
        for pat, src in zip(patterns, sources):
            if pat.search(command):
                return src
        return None

    def evaluate(
        self,
        command: str,
        *,
        exempt_patterns: set[str] | None = None,
        preapproved_patterns: Iterable[str] | None = None,
    ) -> PolicyResult:
        """Run the layered evaluation for ``command``.

        ``exempt_patterns`` — Tier-2 regexes already approved this session.
        ``preapproved_patterns`` — caller-supplied allow list (regex strings);
        a Tier-2 hit matching any of these skips the approval prompt. Tier-1 is
        never exempted by either mechanism.
        """
        exempt = exempt_patterns or set()

        # Layer 0 — Tier-1 floor (always, regardless of mode or exemptions).
        t1 = self._first_match(command, self._tier1, self._tier1_src)
        if t1 is not None:
            return PolicyResult(
                decision=Decision.BLOCK,
                tier=1,
                matched_pattern=t1,
                message=(
                    "Command hard-blocked: matches a Tier-1 destructive pattern. "
                    "There is no override path for Tier-1 commands."
                ),
            )

        if self._config.mode == "whitelist":
            return self._evaluate_whitelist(command, exempt, preapproved_patterns)
        return self._evaluate_blacklist(command, exempt, preapproved_patterns)

    def _tier2_outcome(
        self,
        matched: str,
        exempt: set[str],
        preapproved_patterns: Iterable[str] | None,
    ) -> PolicyResult:
        if matched in exempt:
            return PolicyResult(
                decision=Decision.ALLOW,
                tier=2,
                matched_pattern=matched,
                message="Allowed: regex already approved for this session.",
            )
        for pre in preapproved_patterns or ():
            try:
                if re.search(pre, matched) or pre == matched:
                    return PolicyResult(
                        decision=Decision.ALLOW,
                        tier=2,
                        matched_pattern=matched,
                        message="Allowed: matched a caller pre-approved pattern.",
                    )
            except re.error:
                continue
        return PolicyResult(
            decision=Decision.PENDING_APPROVAL,
            tier=2,
            matched_pattern=matched,
            message="Command requires human approval (Tier-2 risky pattern).",
        )

    def _evaluate_blacklist(
        self,
        command: str,
        exempt: set[str],
        preapproved_patterns: Iterable[str] | None,
    ) -> PolicyResult:
        t2 = self._first_match(command, self._tier2, self._tier2_src)
        if t2 is not None:
            return self._tier2_outcome(t2, exempt, preapproved_patterns)
        return PolicyResult(decision=Decision.ALLOW, message="Allowed (blacklist mode).")

    def _evaluate_whitelist(
        self,
        command: str,
        exempt: set[str],
        preapproved_patterns: Iterable[str] | None,
    ) -> PolicyResult:
        allowed = self._first_match(command, self._whitelist, self._whitelist_src)
        if allowed is None:
            return PolicyResult(
                decision=Decision.BLOCK,
                matched_pattern=None,
                message=(
                    "Command denied: whitelist mode is active and the command "
                    "does not match any allowed pattern."
                ),
            )
        # Whitelisted — but a Tier-2 risky match still needs approval if configured.
        t2 = self._first_match(command, self._tier2, self._tier2_src)
        if t2 is not None:
            return self._tier2_outcome(t2, exempt, preapproved_patterns)
        return PolicyResult(decision=Decision.ALLOW, message="Allowed (whitelisted).")
