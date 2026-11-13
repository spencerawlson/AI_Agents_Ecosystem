"""Server-side plan entitlement checks for Axionory's new tiers.

Checked here, not only in the UI — a direct API request must be
refused the same as a blocked button. Legacy accounts (plan == "")
are never limited by anything in this module; workspaces, team seats,
and the rest are new-tier concepts only.

Every check returns ``(allowed, message)``: ``(True, "")`` when the
action may proceed, ``(False, "<why> — upgrade to ...")`` when the
plan blocks it. ``is_operator`` bypasses every check (the operator
account is never a paying tier account); callers pass
``config.is_operator(account["email"])``.

Two judgment calls with no existing definition in the code — both
documented here so Spencer can adjust them in one place:

1. Growth integrations "selected": Growth gets the three established
   social channels (X, Facebook, Instagram). TikTok is Pro-only while
   the TikTok app is still in review (private-only posting until
   audited). Move ``"tiktok"`` into ``GROWTH_INTEGRATIONS`` to open
   it to Growth.
2. Growth API "limited": the only API surface today is read-only
   JSON data (``/api/snapshot``, ``/api/report`` and the other
   signed-in data endpoints), so "limited" grants those reads, the
   same as Pro. When write endpoints or API keys land, "limited"
   must exclude them — that is what makes it limited.
"""

from __future__ import annotations

from ventoryn.plans import PLANS, limit


# -- what counts as what ---------------------------------------------------
#
# "Active agents" is the number of websites with the agents on
# autopilot (``site["autopilot"]``). It is the only per-account agent
# count that scales to the plan caps (a Starter may have 2, a Pro
# 15): the autopilot runs the same four job types on every site, so
# counting job types could never reach 15. A one-off job run by hand
# does not count — it is already charged from credits / the wallet.
#
# "Active campaigns" is a campaign still awaiting its outcome:
# status ``proposed`` or ``scheduled`` with at least one draft still
# ``pending`` or ``scheduled``. A scheduled campaign whose posts all
# went out (or were held / rejected) is finished and frees its slot;
# a rejected campaign never counted.

#: Connection kinds every plan may use: read-only analytics and
#: sales, not publishing.
UTILITY_INTEGRATIONS = frozenset({"plausible", "stripe_sales"})

#: Connection kinds that publish on the owner's behalf — gated by plan.
PREMIUM_INTEGRATIONS = frozenset({"x", "facebook", "instagram", "tiktok"})

#: Starter's one publishing channel (plans.py: premium_integrations "x").
STARTER_INTEGRATIONS = frozenset({"x"})

#: Growth's "selected" premium integrations (see the module docstring).
GROWTH_INTEGRATIONS = frozenset({"x", "facebook", "instagram"})

#: What each plan may publish to, in words (Subscribe page, refusals).
INTEGRATIONS_LABEL = {"x": "X", "selected": "X, Facebook and Instagram",
                      "all": "X, Facebook, Instagram and TikTok"}

#: Display names of the publishing connections.
KIND_NAMES = {"x": "X", "facebook": "Facebook", "instagram": "Instagram",
              "tiktok": "TikTok"}

#: The API access line per plan value (Subscribe page).
API_LABEL = {True: "Full API access", "limited": "Read-only API access"}


def _plan_label(account: dict) -> str:
    plan = account.get("plan") or ""
    return PLANS.get(plan, {}).get("label", plan or "legacy")


def _cap_or_none(account: dict, key: str):
    """The plan's cap for ``key`` (None when unlimited / unknown)."""
    return limit(account.get("plan") or "", key)


def can_create_workspace(store, account: dict, *,
                         is_operator: bool = False) -> tuple[bool, str]:
    """True when ``account`` can create one more workspace under its
    plan's limit. Always True for a legacy account."""
    if is_operator or _cap_or_none(account, "workspaces") is None:
        return True, ""
    plan = account.get("plan") or ""
    cap = _cap_or_none(account, "workspaces")
    current = len(store.list_workspaces(account["id"]))
    if current >= cap:
        label = _plan_label(account)
        return False, (
            f"Your {label} plan allows {cap} workspace"
            f"{'s' if cap != 1 else ''}; you have {current}. "
            "Upgrade to create another.")
    return True, ""


def count_active_agents(store, account_id: str) -> int:
    """Websites with the agents on autopilot."""
    return sum(1 for s in store.list_sites(account_id)
               if int(s.get("autopilot", 1) or 0))


def can_activate_agents(store, account: dict, *,
                        is_operator: bool = False) -> tuple[bool, str]:
    """True when ``account`` can turn the agents on for one more
    website under its plan's active-agent limit."""
    if is_operator or _cap_or_none(account, "active_agents") is None:
        return True, ""
    cap = _cap_or_none(account, "active_agents")
    current = count_active_agents(store, account["id"])
    if current >= cap:
        label = _plan_label(account)
        return False, (
            f"Your {label} plan allows {cap} active agent"
            f"{'s' if cap != 1 else ''}; you have {current}. "
            "Turn the agents off on another website, or upgrade, "
            "to activate them here.")
    return True, ""


def is_autopilot_site_within_cap(store, account: dict, site: dict, *,
                                 is_operator: bool = False) -> bool:
    """True when this autopilot-on website is one of the plan's
    active-agent slots. Oldest websites keep their slots first, so a
    downgrade keeps the earliest sites working and stops the newest
    ones — the autopilot loop uses this instead of re-checking the
    toggle. A site with autopilot off is never within the cap."""
    if not int(site.get("autopilot", 1) or 0):
        return False
    if is_operator or _cap_or_none(account, "active_agents") is None:
        return True
    cap = _cap_or_none(account, "active_agents")
    on = [s for s in store.list_sites(account["id"])
          if int(s.get("autopilot", 1) or 0)]
    return site["id"] in {s["id"] for s in on[:cap]}


def count_active_campaigns(store, account_id: str) -> int:
    """Campaigns still awaiting an outcome: proposed or scheduled
    with at least one draft still pending or scheduled."""
    n = 0
    for c in store.list_campaigns(account_id, limit=200):
        if c.get("status") not in ("proposed", "scheduled"):
            continue
        drafts = store.campaign_drafts(c["id"])
        if any(d.get("status") in ("pending", "scheduled")
               for d in drafts):
            n += 1
    return n


def can_create_campaign(store, account: dict, *,
                        is_operator: bool = False) -> tuple[bool, str]:
    """True when ``account`` can start one more campaign under its
    plan's active-campaign limit."""
    if is_operator or _cap_or_none(account, "active_campaigns") is None:
        return True, ""
    cap = _cap_or_none(account, "active_campaigns")
    current = count_active_campaigns(store, account["id"])
    if current >= cap:
        label = _plan_label(account)
        return False, (
            f"Your {label} plan allows {cap} active campaign"
            f"{'s' if cap != 1 else ''}; you have {current}. "
            "Wait for one to finish, or upgrade, to start another.")
    return True, ""


def can_add_team_seat(store, account: dict, *,
                      is_operator: bool = False) -> tuple[bool, str]:
    """True when ``account`` can add one more team seat under its
    plan's limit. No invite flow exists yet, so the count is always
    0 until it lands — the check is ready for it."""
    if is_operator or _cap_or_none(account, "team_seats") is None:
        return True, ""
    cap = _cap_or_none(account, "team_seats")
    current = len(store.list_team_members(account["id"]))
    if current >= cap:
        label = _plan_label(account)
        return False, (
            f"Your {label} plan allows {cap} team seat"
            f"{'s' if cap != 1 else ''}; you have {current}. "
            "Upgrade to invite another teammate.")
    return True, ""


def can_view_advanced_analytics(store, account: dict, *,
                                is_operator: bool = False) -> tuple[bool, str]:
    """True when ``account`` may see advanced analytics (the
    acquisition trend, the conversion donut and the /report page).
    The funnel itself is basic analytics and is never gated."""
    if is_operator:
        return True, ""
    plan = account.get("plan") or ""
    if plan not in PLANS:
        return True, ""
    if limit(plan, "advanced_analytics"):
        return True, ""
    return False, (
        "Advanced analytics — the acquisition trend, the conversion "
        "donut and the full report — need Growth or Pro. Your "
        f"{_plan_label(account)} plan includes the basic funnel. "
        "Upgrade to see the rest.")


def can_connect_integration(store, account: dict, kind: str, *,
                            is_operator: bool = False) -> tuple[bool, str]:
    """True when ``account`` may connect ``kind`` for a website.
    Utility connections (Plausible, Stripe sales) are open to every
    plan; publishing connections are gated by the plan's
    premium-integrations tier. Reddit is refused for everyone — its
    API no longer offers the free access this would need."""
    if is_operator:
        return True, ""
    plan = account.get("plan") or ""
    if plan not in PLANS:
        return True, ""
    if kind in UTILITY_INTEGRATIONS:
        return True, ""
    if kind == "reddit":
        return False, (
            "Reddit's API no longer offers the free access this "
            "would need. No account was connected and nothing was "
            "stored.")
    tier = limit(plan, "premium_integrations")
    if tier == "all":
        return True, ""
    if tier == "selected" and kind in GROWTH_INTEGRATIONS:
        return True, ""
    if tier == "x" and kind in STARTER_INTEGRATIONS:
        return True, ""
    label = _plan_label(account)
    needs = "Pro" if kind == "tiktok" else "Growth or Pro"
    return False, (
        f"Your {label} plan posts to {INTEGRATIONS_LABEL.get(tier, 'X')}. "
        f"{KIND_NAMES.get(kind, kind)} needs {needs}. Upgrade to connect it.")


def autopilot_paused_by_plan(store, account: dict, site: dict, *,
                             is_operator: bool = False) -> bool:
    """True when this website's autopilot is switched on but the plan's
    active-agent cap stops it (after a downgrade, the newest sites past
    the cap). The switch says so, rather than looking on while the
    autopilot loop quietly skips the site."""
    return (bool(int(site.get("autopilot", 1) or 0))
            and not is_autopilot_site_within_cap(store, account, site,
                                                 is_operator=is_operator))


def can_use_api(store, account: dict, *,
                is_operator: bool = False) -> tuple[bool, str]:
    """True when ``account`` may use the API (the signed-in JSON data
    endpoints). Starter has no API access; Growth is read-only (see
    the module docstring); Pro is full."""
    if is_operator:
        return True, ""
    plan = account.get("plan") or ""
    if plan not in PLANS:
        return True, ""
    api = limit(plan, "api_access")
    if api:
        return True, ""
    return False, (
        "API access is not included in your "
        f"{_plan_label(account)} plan. Upgrade to Growth or Pro to "
        "use the API.")
