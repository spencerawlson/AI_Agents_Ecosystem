"""Ad platforms: build campaigns PAUSED, go live only on owner approval.

Every adapter creates the whole campaign (budget, targeting, creative)
in a paused state so nothing spends. activate() is only ever called by
the pipeline after the owner approves the campaign; pause() is always
allowed (stopping spend is never a risk). Budget increases go back
through the approval gate.

Meta (Facebook/Instagram) — Marketing API, sales objective, pixel
purchase optimisation. Env (never committed):
  META_ACCESS_TOKEN   system-user token with ads_management
  META_AD_ACCOUNT_ID  digits only (no act_ prefix)
  META_PAGE_ID        Facebook Page the ads run from
  META_PIXEL_ID       pixel/dataset connected to the Shopify store
  META_API_VERSION    optional

Google Ads — REST API, Search campaign with a responsive search ad and
phrase-match keywords. Env:
  GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_CLIENT_ID,
  GOOGLE_ADS_CLIENT_SECRET, GOOGLE_ADS_REFRESH_TOKEN,
  GOOGLE_ADS_CUSTOMER_ID (digits), GOOGLE_ADS_LOGIN_CUSTOMER_ID
  (manager account, optional), GOOGLE_ADS_API_VERSION (optional)
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from core.shopify_adapter import Transport, _urllib_transport

log = logging.getLogger("core.ads")

META_API_VERSION = "v24.0"
GOOGLE_ADS_API_VERSION = "v22"

# Google geo target constants for common markets.
GOOGLE_GEO = {"US": 2840, "CA": 2124, "GB": 2826, "AU": 2036, "NZ": 2554,
              "IE": 2372, "DE": 2276, "FR": 2250}

LIMITS = {
    "google_headline": 30, "google_description": 90,
    "meta_primary": 125, "meta_headline": 40, "meta_description": 30,
}


class AdsError(RuntimeError):
    pass


@dataclass
class AdCopy:
    headlines: list[str]          # short (<=30): Google RSA + Meta headline
    descriptions: list[str]       # <=90: Google RSA descriptions
    primary_texts: list[str]      # <=125: Meta primary text
    keywords: list[str]           # Google phrase-match keywords


@dataclass
class CampaignPlan:
    brief_id: str
    product_title: str
    product_url: str
    image_url: str
    daily_budget_usd: float
    duration_days: int
    countries: list[str]
    copy: AdCopy
    max_cpa_usd: float            # max ad cost per order (from pricing)
    name: str = ""

    def __post_init__(self) -> None:
        if self.daily_budget_usd <= 0:
            raise ValueError("daily budget must be positive")
        if self.duration_days <= 0:
            raise ValueError("duration must be positive")
        if not self.name:
            self.name = f"{self.product_title[:60]} | {self.brief_id}"

    @property
    def total_budget_usd(self) -> float:
        return round(self.daily_budget_usd * self.duration_days, 2)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CampaignPlan":
        d = dict(d)
        d["copy"] = AdCopy(**d["copy"])
        return cls(**d)


@dataclass
class Insights:
    spend_usd: float = 0.0
    impressions: int = 0
    clicks: int = 0
    purchases: float = 0.0
    revenue_usd: float = 0.0

    @property
    def cpa_usd(self) -> float | None:
        return self.spend_usd / self.purchases if self.purchases else None

    @property
    def roas(self) -> float | None:
        return self.revenue_usd / self.spend_usd if self.spend_usd else None


class AdsAdapter(ABC):
    platform: str = "base"

    @abstractmethod
    def create_paused(self, plan: CampaignPlan) -> dict: ...

    @abstractmethod
    def activate(self, refs: dict) -> None: ...

    @abstractmethod
    def pause(self, refs: dict) -> None: ...

    @abstractmethod
    def set_daily_budget(self, refs: dict, daily_budget_usd: float) -> None: ...

    @abstractmethod
    def insights(self, refs: dict) -> Insights: ...


class MockAdsAdapter(AdsAdapter):
    """In-memory ad platform for development and tests."""

    platform = "mock"

    def __init__(self) -> None:
        self.campaigns: dict[str, dict] = {}
        self.stats: dict[str, Insights] = {}

    def create_paused(self, plan: CampaignPlan) -> dict:
        cid = f"mock_{len(self.campaigns) + 1}"
        self.campaigns[cid] = {"status": "PAUSED",
                               "daily_budget_usd": plan.daily_budget_usd,
                               "name": plan.name}
        self.stats[cid] = Insights()
        return {"campaign_id": cid}

    def activate(self, refs: dict) -> None:
        self.campaigns[refs["campaign_id"]]["status"] = "ACTIVE"

    def pause(self, refs: dict) -> None:
        self.campaigns[refs["campaign_id"]]["status"] = "PAUSED"

    def set_daily_budget(self, refs: dict, daily_budget_usd: float) -> None:
        self.campaigns[refs["campaign_id"]]["daily_budget_usd"] = daily_budget_usd

    def insights(self, refs: dict) -> Insights:
        return self.stats[refs["campaign_id"]]


# -- Meta ---------------------------------------------------------------------

_META_PURCHASE_ACTIONS = ("purchase", "omni_purchase",
                          "offsite_conversion.fb_pixel_purchase")


class MetaAdsAdapter(AdsAdapter):
    platform = "meta"

    def __init__(self, access_token: str, ad_account_id: str, page_id: str,
                 pixel_id: str, api_version: str = META_API_VERSION,
                 transport: Transport | None = None) -> None:
        if not all((access_token, ad_account_id, page_id, pixel_id)):
            raise AdsError("Meta needs access token, ad account, page and pixel ids")
        self.token = access_token
        self.account = f"act_{ad_account_id.removeprefix('act_')}"
        self.page_id = page_id
        self.pixel_id = pixel_id
        self.base = f"https://graph.facebook.com/{api_version}"
        self._transport = transport or _urllib_transport

    @classmethod
    def from_env(cls, **kw) -> "MetaAdsAdapter":
        e = os.environ
        return cls(e.get("META_ACCESS_TOKEN", ""), e.get("META_AD_ACCOUNT_ID", ""),
                   e.get("META_PAGE_ID", ""), e.get("META_PIXEL_ID", ""),
                   api_version=e.get("META_API_VERSION", META_API_VERSION), **kw)

    @staticmethod
    def configured() -> bool:
        return all(os.environ.get(k) for k in (
            "META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID", "META_PAGE_ID", "META_PIXEL_ID"))

    def _req(self, method: str, path: str, fields: dict | None = None) -> dict:
        params = {"access_token": self.token}
        body = None
        url = f"{self.base}/{path}"
        if method == "GET":
            params.update(fields or {})
        else:
            form = {k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v))
                    for k, v in (fields or {}).items()}
            form["access_token"] = self.token
            body = urllib.parse.urlencode(form).encode()
            params = {}
        if params:
            url += "?" + urllib.parse.urlencode(params)
        status, text = self._transport(
            method, url, {"Content-Type": "application/x-www-form-urlencoded"}, body)
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            data = {}
        if status != 200 or "error" in data:
            msg = (data.get("error") or {}).get("message") or text[:300]
            raise AdsError(f"Meta {path}: HTTP {status}: {msg}")
        return data

    def verify(self) -> dict:
        """Live read-only check of the ad account."""
        data = self._req("GET", self.account,
                         {"fields": "name,account_status,currency"})
        # 1 = ACTIVE; anything else (disabled, unsettled, ...) can't run ads.
        return {"name": data.get("name"), "currency": data.get("currency"),
                "active": data.get("account_status") == 1,
                "account_status": data.get("account_status")}

    def create_paused(self, plan: CampaignPlan) -> dict:
        refs: dict = {}
        try:
            refs["campaign_id"] = self._req("POST", f"{self.account}/campaigns", {
                "name": plan.name,
                "objective": "OUTCOME_SALES",
                "status": "PAUSED",
                "special_ad_categories": [],
                "buying_type": "AUCTION",
                "is_adset_budget_sharing_enabled": False,
            })["id"]
            start = datetime.now(timezone.utc) + timedelta(hours=1)
            end = start + timedelta(days=plan.duration_days)
            refs["adset_id"] = self._req("POST", f"{self.account}/adsets", {
                "name": f"{plan.name} | adset",
                "campaign_id": refs["campaign_id"],
                "daily_budget": int(round(plan.daily_budget_usd * 100)),
                "billing_event": "IMPRESSIONS",
                "optimization_goal": "OFFSITE_CONVERSIONS",
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                "promoted_object": {"pixel_id": self.pixel_id,
                                    "custom_event_type": "PURCHASE"},
                "targeting": {"geo_locations": {"countries": plan.countries},
                              "age_min": 18,
                              "targeting_automation": {"advantage_audience": 1}},
                "start_time": start.strftime("%Y-%m-%dT%H:%M:%S+0000"),
                "end_time": end.strftime("%Y-%m-%dT%H:%M:%S+0000"),
                "status": "PAUSED",
            })["id"]
            c = plan.copy
            refs["creative_id"] = self._req("POST", f"{self.account}/adcreatives", {
                "name": f"{plan.name} | creative",
                "object_story_spec": {"page_id": self.page_id, "link_data": {
                    "link": plan.product_url,
                    "message": c.primary_texts[0],
                    "name": c.headlines[0][:LIMITS["meta_headline"]],
                    "description": c.descriptions[0][:LIMITS["meta_description"]],
                    "picture": plan.image_url,
                    "call_to_action": {"type": "SHOP_NOW",
                                       "value": {"link": plan.product_url}},
                }},
            })["id"]
            refs["ad_id"] = self._req("POST", f"{self.account}/ads", {
                "name": f"{plan.name} | ad",
                "adset_id": refs["adset_id"],
                "creative": {"creative_id": refs["creative_id"]},
                "status": "PAUSED",
            })["id"]
        except AdsError as exc:
            raise AdsError(f"{exc} (partially created, all PAUSED: {refs})") from exc
        return refs

    def _set_status(self, refs: dict, status: str, keys: tuple) -> None:
        for key in keys:
            if refs.get(key):
                self._req("POST", refs[key], {"status": status})

    def activate(self, refs: dict) -> None:
        # Children first so the campaign never goes live half-configured.
        self._set_status(refs, "ACTIVE", ("ad_id", "adset_id", "campaign_id"))

    def pause(self, refs: dict) -> None:
        self._set_status(refs, "PAUSED", ("campaign_id",))

    def set_daily_budget(self, refs: dict, daily_budget_usd: float) -> None:
        self._req("POST", refs["adset_id"],
                  {"daily_budget": int(round(daily_budget_usd * 100))})

    def insights(self, refs: dict) -> Insights:
        data = self._req("GET", f"{refs['campaign_id']}/insights", {
            "fields": "spend,impressions,clicks,actions,action_values",
            "date_preset": "maximum",
        }).get("data") or []
        if not data:
            return Insights()
        row = data[0]

        def action_total(key: str) -> float:
            vals = {a.get("action_type"): float(a.get("value", 0))
                    for a in row.get(key) or []}
            # Meta reports the same purchases under several action types;
            # take the single best-populated one, never the sum.
            return max((vals.get(t, 0.0) for t in _META_PURCHASE_ACTIONS), default=0.0)

        return Insights(spend_usd=float(row.get("spend", 0)),
                        impressions=int(row.get("impressions", 0)),
                        clicks=int(row.get("clicks", 0)),
                        purchases=action_total("actions"),
                        revenue_usd=action_total("action_values"))


# -- Google Ads ---------------------------------------------------------------

class GoogleAdsAdapter(AdsAdapter):
    platform = "google"

    def __init__(self, developer_token: str, client_id: str, client_secret: str,
                 refresh_token: str, customer_id: str,
                 login_customer_id: str | None = None,
                 api_version: str = GOOGLE_ADS_API_VERSION,
                 transport: Transport | None = None) -> None:
        if not all((developer_token, client_id, client_secret, refresh_token,
                    customer_id)):
            raise AdsError("Google Ads credentials incomplete")
        self.developer_token = developer_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.cid = customer_id.replace("-", "")
        self.login_cid = (login_customer_id or "").replace("-", "") or None
        self.api_root = f"https://googleads.googleapis.com/{api_version}"
        self.base = f"{self.api_root}/customers/{self.cid}"
        self._transport = transport or _urllib_transport
        self._token: str | None = None

    @classmethod
    def from_env(cls, **kw) -> "GoogleAdsAdapter":
        e = os.environ
        return cls(e.get("GOOGLE_ADS_DEVELOPER_TOKEN", ""),
                   e.get("GOOGLE_ADS_CLIENT_ID", ""),
                   e.get("GOOGLE_ADS_CLIENT_SECRET", ""),
                   e.get("GOOGLE_ADS_REFRESH_TOKEN", ""),
                   e.get("GOOGLE_ADS_CUSTOMER_ID", ""),
                   e.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID"),
                   api_version=e.get("GOOGLE_ADS_API_VERSION", GOOGLE_ADS_API_VERSION),
                   **kw)

    @staticmethod
    def configured() -> bool:
        return all(os.environ.get(k) for k in (
            "GOOGLE_ADS_DEVELOPER_TOKEN", "GOOGLE_ADS_CLIENT_ID",
            "GOOGLE_ADS_CLIENT_SECRET", "GOOGLE_ADS_REFRESH_TOKEN",
            "GOOGLE_ADS_CUSTOMER_ID"))

    def _access_token(self) -> str:
        if self._token is None:
            body = urllib.parse.urlencode({
                "grant_type": "refresh_token", "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
            }).encode()
            status, text = self._transport(
                "POST", "https://oauth2.googleapis.com/token",
                {"Content-Type": "application/x-www-form-urlencoded"}, body)
            if status != 200:
                raise AdsError(f"Google OAuth failed ({status}): {text[:200]}")
            self._token = json.loads(text)["access_token"]
        return self._token

    def _post(self, path: str, payload: dict) -> dict:
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self._access_token()}",
                   "developer-token": self.developer_token}
        if self.login_cid:
            headers["login-customer-id"] = self.login_cid
        status, text = self._transport("POST", f"{self.base}/{path}", headers,
                                       json.dumps(payload).encode())
        if status != 200:
            raise AdsError(f"Google Ads {path}: HTTP {status}: {text[:400]}")
        return json.loads(text) if text else {}

    def verify(self) -> dict:
        """Live read-only check: customers these credentials can reach."""
        headers = {"Authorization": f"Bearer {self._access_token()}",
                   "developer-token": self.developer_token}
        status, text = self._transport(
            "GET", f"{self.api_root}/customers:listAccessibleCustomers", headers, None)
        if status != 200:
            raise AdsError(f"Google Ads verify: HTTP {status}: {text[:300]}")
        names = json.loads(text).get("resourceNames", [])
        ids = [n.rsplit("/", 1)[-1] for n in names]
        # Via a manager account the client id may not be listed directly.
        return {"accessible": ids,
                "customer_listed": self.cid in ids or (self.login_cid in ids)}

    def _rn(self, kind: str, temp_id: int) -> str:
        return f"customers/{self.cid}/{kind}/{temp_id}"

    def create_paused(self, plan: CampaignPlan) -> dict:
        budget, campaign, group = (self._rn("campaignBudgets", -1),
                                   self._rn("campaigns", -2),
                                   self._rn("adGroups", -3))
        c = plan.copy
        ops: list[dict] = [
            {"campaignBudgetOperation": {"create": {
                "resourceName": budget, "name": f"{plan.name} | budget",
                "amountMicros": str(int(round(plan.daily_budget_usd * 1_000_000))),
                "deliveryMethod": "STANDARD", "explicitlyShared": False}}},
            {"campaignOperation": {"create": {
                "resourceName": campaign, "name": plan.name, "status": "PAUSED",
                "advertisingChannelType": "SEARCH", "campaignBudget": budget,
                "maximizeConversions": {},
                "networkSettings": {"targetGoogleSearch": True,
                                    "targetSearchNetwork": False,
                                    "targetContentNetwork": False},
                "containsEuPoliticalAdvertising":
                    "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"}}},
        ]
        for country in plan.countries:
            geo = GOOGLE_GEO.get(country.upper())
            if geo:
                ops.append({"campaignCriterionOperation": {"create": {
                    "campaign": campaign,
                    "location": {"geoTargetConstant": f"geoTargetConstants/{geo}"}}}})
        ops += [
            {"adGroupOperation": {"create": {
                "resourceName": group, "name": f"{plan.name} | ad group",
                "campaign": campaign, "status": "ENABLED",
                "type": "SEARCH_STANDARD"}}},
            {"adGroupAdOperation": {"create": {
                "adGroup": group, "status": "ENABLED",
                "ad": {"finalUrls": [plan.product_url], "responsiveSearchAd": {
                    "headlines": [{"text": h} for h in c.headlines[:15]],
                    "descriptions": [{"text": d} for d in c.descriptions[:4]]}}}}},
        ]
        for kw in c.keywords[:20]:
            ops.append({"adGroupCriterionOperation": {"create": {
                "adGroup": group, "status": "ENABLED",
                "keyword": {"text": kw, "matchType": "PHRASE"}}}})
        res = self._post("googleAds:mutate", {"mutateOperations": ops})
        refs: dict = {}
        for r in res.get("mutateOperationResponses", []):
            if "campaignResult" in r:
                refs["campaign"] = r["campaignResult"]["resourceName"]
            elif "campaignBudgetResult" in r:
                refs["budget"] = r["campaignBudgetResult"]["resourceName"]
        if "campaign" not in refs:
            raise AdsError(f"Google Ads returned no campaign: {res}")
        refs["campaign_id"] = refs["campaign"].rsplit("/", 1)[-1]
        return refs

    def _campaign_status(self, refs: dict, status: str) -> None:
        self._post("campaigns:mutate", {"operations": [{
            "update": {"resourceName": refs["campaign"], "status": status},
            "updateMask": "status"}]})

    def activate(self, refs: dict) -> None:
        self._campaign_status(refs, "ENABLED")

    def pause(self, refs: dict) -> None:
        self._campaign_status(refs, "PAUSED")

    def set_daily_budget(self, refs: dict, daily_budget_usd: float) -> None:
        self._post("campaignBudgets:mutate", {"operations": [{
            "update": {"resourceName": refs["budget"],
                       "amountMicros": str(int(round(daily_budget_usd * 1_000_000)))},
            "updateMask": "amount_micros"}]})

    def insights(self, refs: dict) -> Insights:
        query = ("SELECT metrics.cost_micros, metrics.impressions, metrics.clicks, "
                 "metrics.conversions, metrics.conversions_value FROM campaign "
                 f"WHERE campaign.resource_name = '{refs['campaign']}'")
        rows = self._post("googleAds:search", {"query": query}).get("results") or []
        if not rows:
            return Insights()
        m = rows[0].get("metrics", {})
        return Insights(spend_usd=int(m.get("costMicros", 0)) / 1_000_000,
                        impressions=int(m.get("impressions", 0)),
                        clicks=int(m.get("clicks", 0)),
                        purchases=float(m.get("conversions", 0)),
                        revenue_usd=float(m.get("conversionsValue", 0)))


def configured_ads_adapters() -> dict[str, AdsAdapter]:
    out: dict[str, AdsAdapter] = {}
    if MetaAdsAdapter.configured():
        out["meta"] = MetaAdsAdapter.from_env()
    if GoogleAdsAdapter.configured():
        out["google"] = GoogleAdsAdapter.from_env()
    return out


# -- Ad copy ------------------------------------------------------------------

_COPY_SYSTEM = (
    "You write direct-response ad copy for a small online store. Follow "
    "Meta and Google ad policies: no health/medical claims, no before/after "
    "promises, no fake urgency or discounts, no superlatives you can't "
    "prove, no personal attributes ('Are you overweight?'). Benefits, "
    "concrete features, clear call to action."
)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut if cut else text[:limit]


@dataclass
class AdCopyWriter:
    gateway: object = None
    spend_usd: float = 0.0
    tokens: int = 0
    warnings: list[str] = field(default_factory=list)

    def write(self, product_title: str, niche: str, keywords: list[str],
              facts: str = "") -> AdCopy:
        if self.gateway is not None:
            try:
                return self._llm(product_title, niche, keywords, facts)
            except Exception as exc:  # noqa: BLE001
                self.warnings.append(f"LLM ad copy failed, template used: {exc}")
        return self._normalise(self._template(product_title, niche, keywords), keywords)

    def _llm(self, title, niche, keywords, facts) -> AdCopy:
        res = self.gateway.complete(system=_COPY_SYSTEM, prompt=(
            "Return STRICT JSON: {\"headlines\": [10 strings <=30 chars], "
            "\"descriptions\": [4 strings <=90 chars], \"primary_texts\": "
            "[3 strings <=125 chars], \"keywords\": [10-15 buyer search "
            "phrases]}.\n"
            f"Product: {title}\nAudience/niche: {niche}\n"
            f"Seed keywords: {', '.join(keywords)}\nFacts: {facts or '(none)'}"))
        self.spend_usd += res.get("cost_usd", 0.0)
        self.tokens += res.get("input_tokens", 0) + res.get("output_tokens", 0)
        return self._normalise(res.get("json") or {}, keywords)

    @staticmethod
    def _template(title, niche, keywords) -> dict:
        return {
            "headlines": [title, f"Shop {title}", "Free Shipping", "Order Online Today",
                          f"Made For {niche.title()}"],
            "descriptions": [f"{title}. Ships with tracking. Order today.",
                             f"Designed for {niche}. Simple returns, secure checkout."],
            "primary_texts": [f"{title} — designed for {niche}. Tap Shop Now to see it."],
            "keywords": keywords,
        }

    @staticmethod
    def _normalise(data: dict, keywords: list[str]) -> AdCopy:
        def clean(items, limit, minimum, fallback):
            out = list(dict.fromkeys(
                _clip(i, limit) for i in (items or []) if str(i).strip()))
            for f in fallback:
                if len(out) >= minimum:
                    break
                f = _clip(f, limit)
                if f not in out:
                    out.append(f)
            return out

        heads = clean(data.get("headlines"), LIMITS["google_headline"], 3,
                      ["Shop Now", "Free Shipping", "Order Online Today"])
        descs = clean(data.get("descriptions"), LIMITS["google_description"], 2,
                      ["Secure checkout and tracked delivery.",
                       "Order online today. Simple returns."])
        prim = clean(data.get("primary_texts"), LIMITS["meta_primary"], 1,
                     [heads[0]])
        kws = list(dict.fromkeys(
            str(k).strip().lower() for k in (data.get("keywords") or keywords)
            if str(k).strip()))[:20]
        return AdCopy(headlines=heads[:15], descriptions=descs[:4],
                      primary_texts=prim[:5], keywords=kws)
