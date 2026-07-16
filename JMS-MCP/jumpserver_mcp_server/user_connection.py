"""User (runas) resolution for a target asset.

JumpServer accounts configured on an asset are the candidate ``runas``
identities for an ops job. This layer resolves which account a command will
run as, following design.md Decision 6:

- If the caller pre-specifies a ``user_id`` or ``username``, use it (automation
  path) — resolved against the asset's accounts so we can map a human-friendly
  username to the ``runas`` value the ops job needs.
- If the caller specifies nothing and exactly one account exists, use it.
- If multiple accounts exist and none was specified, return the candidate list
  for the caller to choose from, without connecting (Decision 6 / spec
  "User selection drives the connecting identity").

RBAC is NOT pre-checked here (Decision 14 / spec "Permission discovered at
execution time"): resolution only maps a user to a runas string. Whether that
user may actually run the command is decided by JumpServer at execution and
surfaced later as ``permission_denied``.
"""

from __future__ import annotations

from dataclasses import dataclass
from logging import getLogger
from typing import Any

from .host_discovery import HostDiscovery

logger = getLogger(__name__)


@dataclass
class RunasResolution:
    """Result of resolving a runas identity for an asset.

    Exactly one of ``runas`` (resolved) or ``candidates`` (needs selection) is
    meaningful: when ``needs_selection`` is True the caller must choose from
    ``candidates`` and re-invoke with a ``user_id``/``username``.
    """

    asset_id: str
    needs_selection: bool
    runas: str | None = None
    account: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "asset_id": self.asset_id,
            "needs_selection": self.needs_selection,
        }
        if self.runas is not None:
            out["runas"] = self.runas
        if self.account is not None:
            out["account"] = self.account
        if self.candidates is not None:
            out["candidates"] = self.candidates
        if self.message is not None:
            out["message"] = self.message
        return out


class UserConnectionResolver:
    """Resolve the connecting (runas) user for an asset."""

    def __init__(self, discovery: HostDiscovery) -> None:
        self._discovery = discovery

    @staticmethod
    def _match_account(
        accounts: list[dict[str, Any]],
        *,
        user_id: str | None,
        username: str | None,
    ) -> dict[str, Any] | None:
        for acc in accounts:
            if user_id and acc.get("id") == user_id:
                return acc
            if username and acc.get("username") == username:
                return acc
        return None

    async def resolve(
        self,
        asset_id: str,
        *,
        user_id: str | None = None,
        username: str | None = None,
    ) -> RunasResolution:
        """Resolve a runas identity for an asset.

        Does not pre-check RBAC; only maps a caller's selection (or the sole
        candidate) to the ``runas`` username an ops job needs.
        """
        candidates = await self._discovery.fetch_runas_candidates(asset_id)

        if not candidates:
            return RunasResolution(
                asset_id=asset_id,
                needs_selection=False,
                runas=None,
                candidates=[],
                message=(
                    "No accounts are configured on this asset in JumpServer. "
                    "An administrator must add a login account before commands "
                    "can run."
                ),
            )

        # Pre-specified user (automation path).
        if user_id or username:
            account = self._match_account(
                candidates, user_id=user_id, username=username
            )
            if account is None:
                return RunasResolution(
                    asset_id=asset_id,
                    needs_selection=False,
                    runas=None,
                    candidates=candidates,
                    message=(
                        "The specified user is not configured on this asset. "
                        "Choose one of the listed candidates."
                    ),
                )
            return RunasResolution(
                asset_id=asset_id,
                needs_selection=False,
                runas=account.get("username"),
                account=account,
            )

        # Nothing specified: auto-use a single candidate, else ask to choose.
        if len(candidates) == 1:
            account = candidates[0]
            return RunasResolution(
                asset_id=asset_id,
                needs_selection=False,
                runas=account.get("username"),
                account=account,
            )

        return RunasResolution(
            asset_id=asset_id,
            needs_selection=True,
            candidates=candidates,
            message=(
                f"{len(candidates)} users are available for this asset. "
                "Re-invoke with a chosen user_id or username."
            ),
        )
