"""Tests for the layered security policy (tasks 9.4, 9.8)."""

from jumpserver_mcp_server.security_policy import (
    Decision,
    PolicyConfig,
    PolicyEngine,
)


def _blacklist_engine() -> PolicyEngine:
    return PolicyEngine(PolicyConfig(mode="blacklist"))


def _whitelist_engine(whitelist: list[str]) -> PolicyEngine:
    return PolicyEngine(PolicyConfig(mode="whitelist", whitelist=whitelist))


# --- Tier-1 hard block (9.8) -------------------------------------------------

def test_tier1_rm_rf_root_is_blocked():
    res = _blacklist_engine().evaluate("rm -rf /")
    assert res.decision is Decision.BLOCK
    assert res.tier == 1


def test_tier1_mkfs_is_blocked():
    res = _blacklist_engine().evaluate("mkfs.ext4 /dev/sda")
    assert res.decision is Decision.BLOCK
    assert res.tier == 1


def test_tier1_dd_to_raw_disk_is_blocked():
    res = _blacklist_engine().evaluate("dd if=/dev/zero of=/dev/sda bs=1M")
    assert res.decision is Decision.BLOCK
    assert res.tier == 1


def test_tier1_has_no_override_via_exemption():
    eng = _blacklist_engine()
    res = eng.evaluate("rm -rf /")
    # Even if the (Tier-1) pattern were in the exempt set, it stays blocked.
    res2 = eng.evaluate("rm -rf /", exempt_patterns={res.matched_pattern})
    assert res2.decision is Decision.BLOCK
    assert res2.tier == 1


# --- Tier-2 approval (9.4) ---------------------------------------------------

def test_tier2_risky_requires_approval():
    res = _blacklist_engine().evaluate("rm -f /var/log/app/old.log")
    assert res.decision is Decision.PENDING_APPROVAL
    assert res.tier == 2
    assert res.matched_pattern


def test_tier2_exemption_allows_same_regex():
    eng = _blacklist_engine()
    res = eng.evaluate("rm -f /var/log/old.log")
    assert res.decision is Decision.PENDING_APPROVAL
    res2 = eng.evaluate(
        "rm -f /var/log/other.log", exempt_patterns={res.matched_pattern}
    )
    assert res2.decision is Decision.ALLOW


def test_tier2_preapproved_pattern_skips_prompt():
    eng = _blacklist_engine()
    res = eng.evaluate("rm -f /var/log/old.log")
    res2 = eng.evaluate(
        "rm -f /var/log/old.log", preapproved_patterns=[res.matched_pattern]
    )
    assert res2.decision is Decision.ALLOW


# --- Allow path --------------------------------------------------------------

def test_safe_command_allowed_in_blacklist():
    res = _blacklist_engine().evaluate("ls -la /home")
    assert res.decision is Decision.ALLOW


# --- Whitelist mode (9.8) ----------------------------------------------------

def test_whitelist_denies_unlisted():
    res = _whitelist_engine([r"^ls\b", r"^whoami\b"]).evaluate("cat /etc/passwd")
    assert res.decision is Decision.BLOCK
    assert res.tier is None


def test_whitelist_allows_listed():
    res = _whitelist_engine([r"^ls\b"]).evaluate("ls -la")
    assert res.decision is Decision.ALLOW


def test_whitelist_still_hard_blocks_tier1():
    # Even a whitelisted prefix cannot escape the Tier-1 floor.
    res = _whitelist_engine([r"^rm\b"]).evaluate("rm -rf /")
    assert res.decision is Decision.BLOCK
    assert res.tier == 1


# --- Config / floor ----------------------------------------------------------

def test_tier1_floor_cannot_be_emptied():
    cfg = PolicyConfig(mode="blacklist", tier1=[])
    cfg.tier1 = cfg.tier1 or list(PolicyConfig().tier1)
    eng = PolicyEngine(cfg)
    assert eng.evaluate("rm -rf /").tier == 1


def test_default_mode_is_blacklist():
    assert PolicyConfig().mode == "blacklist"
