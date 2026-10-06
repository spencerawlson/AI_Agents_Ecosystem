"""Automated supplier search.

Alibaba has no public buyer-side search API, so automated search runs
against CJ Dropshipping's official API (a China-based sourcing/dropship
agent with per-order fulfilment and tracking). Alibaba stays in the loop
as owner-driven sourcing: the report carries ready-made Alibaba search
links and the owner can add Alibaba quotes to the brief.

Setup: create a free CJ account -> My CJ -> Authorization -> API ->
generate an API key, then set CJ_API_KEY. Credentials never committed.

CJ API v2 calls used (all read-only — nothing is ordered):
  POST /authentication/getAccessToken   apiKey -> accessToken
  GET  /product/list                    keyword search
  GET  /product/query                   variants (vid) for one product
  POST /logistic/freightCalculate       per-order shipping CN -> customer
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
from typing import Callable

from core.shopify_adapter import Transport, _urllib_transport
from core.sourcing import BriefSupplierSource, ProductBrief, Supplier

log = logging.getLogger("core.supplier_search")

CJ_API = "https://developers.cjdropshipping.com/api2.0/v1"


class SupplierSearchError(RuntimeError):
    pass


def _parse_days(aging: object) -> int | None:
    """'7-15' -> 15 (worst case), '12' -> 12."""
    if aging is None:
        return None
    parts = [p for p in str(aging).replace(" ", "").split("-") if p.isdigit()]
    return max(int(p) for p in parts) if parts else None


def _first_price(value: object) -> float | None:
    """CJ prices can be '3.20' or a range '3.20 -- 4.10'; take the low end."""
    if value is None:
        return None
    text = str(value).replace("--", "-").split("-")[0].strip()
    try:
        return float(text)
    except ValueError:
        return None


class CJClient:
    """Authenticated CJ Dropshipping API v2 client (shared by search and
    order placement)."""

    platform = "cjdropshipping"

    def __init__(self, api_key: str, transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not api_key:
            raise SupplierSearchError("CJ api key is required")
        self.api_key = api_key
        self._transport = transport or _urllib_transport
        self._sleep = sleep
        self._token: str | None = None

    @classmethod
    def from_env(cls, **kw):
        return cls(os.environ.get("CJ_API_KEY", ""), **kw)

    def verify(self) -> dict:
        """Live read-only check: obtains an access token."""
        self._access_token()
        return {"ok": True}

    @staticmethod
    def configured() -> bool:
        return bool(os.environ.get("CJ_API_KEY"))

    def _call(self, method: str, path: str, params: dict | None = None,
              body: dict | None = None) -> object:
        url = CJ_API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Content-Type": "application/json"}
        if path != "/authentication/getAccessToken":
            headers["CJ-Access-Token"] = self._access_token()
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(3):
            status, text = self._transport(method, url, headers, data)
            if status == 429 and attempt < 2:
                self._sleep(1.0 + attempt)  # CJ allows ~1 request/second
                continue
            break
        if status != 200:
            raise SupplierSearchError(f"CJ HTTP {status}: {text[:200]}")
        payload = json.loads(text)
        if not payload.get("result", payload.get("code") == 200):
            raise SupplierSearchError(f"CJ error: {payload.get('message')}")
        return payload.get("data")

    def _access_token(self) -> str:
        if self._token is None:
            data = self._call("POST", "/authentication/getAccessToken",
                              body={"apiKey": self.api_key})
            self._token = (data or {}).get("accessToken")
            if not self._token:
                raise SupplierSearchError("CJ returned no access token")
        return self._token


class CJDropshippingSource(CJClient):
    """SupplierSource backed by the CJ Dropshipping API."""

    def __init__(self, api_key: str, transport: Transport | None = None,
                 dest_country: str = "US", per_keyword: int = 5,
                 max_suppliers: int = 6,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        super().__init__(api_key, transport=transport, sleep=sleep)
        self.dest_country = dest_country
        self.per_keyword = per_keyword
        self.max_suppliers = max_suppliers

    def _shipping(self, vid: str) -> tuple[float | None, int | None, str]:
        """Cheapest tracked option for one unit -> (usd, days, method)."""
        options = self._call("POST", "/logistic/freightCalculate", body={
            "startCountryCode": "CN",
            "endCountryCode": self.dest_country,
            "products": [{"quantity": 1, "vid": vid}],
        }) or []
        best = None
        for opt in options:
            price = _first_price(opt.get("logisticPrice"))
            if price is None:
                continue
            if best is None or price < best[0]:
                best = (price, _parse_days(opt.get("logisticAging")),
                        str(opt.get("logisticName", "")))
        return best or (None, None, "")

    def _supplier_for(self, item: dict) -> Supplier | None:
        pid = item.get("pid")
        name = item.get("productNameEn") or item.get("productName") or pid
        detail = self._call("GET", "/product/query", params={"pid": pid}) or {}
        variants = detail.get("variants") or []
        if not variants:
            return None
        v = min(variants, key=lambda x: _first_price(x.get("variantSellPrice")) or 1e9)
        unit = _first_price(v.get("variantSellPrice")) or _first_price(item.get("sellPrice"))
        if unit is None:
            return None
        ship, days, method = self._shipping(v["vid"])
        images = [img for img in [detail.get("productImage") or item.get("productImage")]
                  if isinstance(img, str) and img.startswith("http")]
        extra = detail.get("productImageSet") or []
        images += [i for i in extra if isinstance(i, str) and i.startswith("http")]
        notes = f"CJ pid {pid}, variant {v['vid']}"
        if method:
            notes += f", shipping via {method}"
        if ship is None:
            notes += "; SHIPPING NOT QUOTED — confirm before approving"
        return Supplier(
            name=f"CJ: {str(name)[:60]}",
            url=f"https://cjdropshipping.com/product/-p-{pid}.html",
            platform=self.platform,
            unit_cost_usd=unit,
            shipping_cost_usd=ship or 0.0,
            moq=1,
            lead_time_days=days,
            supplier_product_id=str(pid),
            supplier_variant_id=str(v["vid"]),
            shipping_method=method,
            # CJ handles per-order fulfilment + tracking; not an Alibaba
            # verified/Trade Assurance listing, so those stay False.
            image_urls=list(dict.fromkeys(images))[:8],
            notes=notes,
        )

    def find(self, brief: ProductBrief) -> list[Supplier]:
        seen: set[str] = set()
        out: list[Supplier] = []
        for kw in (brief.keywords or [brief.product_name])[:2]:
            res = self._call("GET", "/product/list", params={
                "productNameEn": kw, "pageNum": 1, "pageSize": self.per_keyword,
            }) or {}
            for item in res.get("list", []):
                pid = item.get("pid")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                try:
                    sup = self._supplier_for(item)
                except SupplierSearchError as exc:
                    log.warning("CJ product %s skipped: %s", pid, exc)
                    continue
                if sup is not None:
                    out.append(sup)
                if len(out) >= self.max_suppliers:
                    return out
        return out


class CombinedSupplierSource:
    """Owner-entered suppliers (e.g. Alibaba quotes) + automated search.

    A failing automated source never hides the owner's own candidates.
    """

    def __init__(self, *sources) -> None:
        self.sources = sources or (BriefSupplierSource(),)
        self.errors: list[str] = []

    def find(self, brief: ProductBrief) -> list[Supplier]:
        out: list[Supplier] = []
        names: set[str] = set()
        for src in self.sources:
            try:
                found = src.find(brief)
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"{type(src).__name__}: {exc}")
                log.warning("supplier source failed: %s", exc)
                continue
            for s in found:
                if s.name not in names:
                    names.add(s.name)
                    out.append(s)
        return out


def default_supplier_source() -> CombinedSupplierSource:
    sources: list = [BriefSupplierSource()]
    if CJDropshippingSource.configured():
        sources.append(CJDropshippingSource.from_env())
    return CombinedSupplierSource(*sources)
