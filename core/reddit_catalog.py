"""Reddit Ads catalog sync (Phase B): Shopify -> Reddit mapping + dry-run.

Pure functions over Shopify product nodes as returned by
``ShopifyCommerceAdapter.list_products()``. No live calls, no
credentials, no push: the push to Reddit is a separate approval and
its code lands with it — this phase only maps and reports what WOULD
sync, so the merchant can review the exact mapping first.

Reddit catalog payload field names follow the design doc's
cross-verified surface (Google-Merchant-style feed fields:
``id`` / ``title`` / ``description`` / ``price`` / ``image_link`` /
``link`` / ``availability`` / ``brand``). The OpenAPI spec at
https://ads-api.reddit.com/api/v3/openapi.json is the build-time
authority before any live write — if it names these fields
differently, the mapper changes, not the dry-run report shape.
"""

from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass

# Skip reasons, in the order the mapper checks them. A product is
# skipped at the first reason that applies, so the report names exactly
# one cause per product.
REASON_NOT_ACTIVE = "not_active"
REASON_NO_TITLE = "no_title"
REASON_NO_IMAGE = "no_image"
REASON_NO_PRICE = "no_price"
REASON_OUT_OF_STOCK = "out_of_stock"
REASON_NO_URL = "no_url"

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Product description body as plain text (no tags, no entities)."""
    return " ".join(html.unescape(_TAG_RE.sub(" ", text or "")).split())


def _numeric_id(gid: str) -> str:
    """Shopify GID -> stable retailer id ("gid://shopify/Product/123" -> "123")."""
    gid = str(gid or "")
    return gid.rsplit("/", 1)[-1] if "/" in gid else gid


@dataclass
class RedditCatalogProduct:
    """One product as it would appear in the Reddit catalog feed."""

    id: str
    title: str
    description: str
    price: str            # "<amount> <currency>", e.g. "19.99 USD"
    image_link: str
    link: str
    availability: str     # "in_stock" | "out_of_stock"
    brand: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _first_variant(product: dict) -> dict | None:
    nodes = ((product.get("variants") or {}).get("nodes") or [])
    return nodes[0] if nodes else None


def _image_url(product: dict) -> str | None:
    """The product's featured image, or None when there is none.

    ``featuredMedia`` resolves to a Media object; only a MediaImage
    carries an image URL — a video (or no media at all) means the
    product cannot go in a catalog, which needs an image.
    """
    media = product.get("featuredMedia") or {}
    image = media.get("image") or {}
    return image.get("url") or None


def map_shopify_product(
    product: dict, currency: str = "USD",
) -> tuple[RedditCatalogProduct | None, str | None]:
    """Map one Shopify product node to a Reddit catalog product.

    Returns ``(product, None)`` when the product would sync, or
    ``(None, reason)`` when it would be skipped, with exactly one
    reason from the REASON_* constants.
    """
    if (product.get("status") or "") != "ACTIVE":
        return None, REASON_NOT_ACTIVE
    title = (product.get("title") or "").strip()
    if not title:
        return None, REASON_NO_TITLE
    image_url = _image_url(product)
    if not image_url:
        return None, REASON_NO_IMAGE
    variant = _first_variant(product)
    price = (variant or {}).get("price")
    if variant is None or price in (None, ""):
        return None, REASON_NO_PRICE
    quantity = variant.get("inventoryQuantity")
    if quantity is not None and quantity <= 0:
        return None, REASON_OUT_OF_STOCK
    url = (product.get("onlineStoreUrl") or "").strip()
    if not url:
        return None, REASON_NO_URL
    description = _strip_html(product.get("descriptionHtml") or "")
    if not description:
        description = ((product.get("seo") or {}).get("description") or "").strip()
    return RedditCatalogProduct(
        id=_numeric_id(product.get("id", "")),
        title=title,
        description=description,
        price=f"{price} {currency}",
        image_link=image_url,
        link=url,
        # inventoryQuantity is None when inventory is not tracked
        # (e.g. dropship): the product is sold, so it counts as in
        # stock — the dry-run flags it as a warning, never silently.
        availability="in_stock",
        brand=(product.get("vendor") or "").strip(),
    ), None


def dry_run_catalog_sync(
    products: list[dict], currency: str = "USD",
) -> dict:
    """Dry-run the catalog sync: the exact mapping, nothing pushed.

    Returns ``{"total", "included_count", "skipped_count", "included",
    "skipped", "warnings"}``. ``included`` holds the Reddit catalog
    products as plain dicts; ``skipped`` holds one
    ``{"product_id", "title", "reason"}`` per skipped product;
    ``warnings`` flags products included on an assumption (untracked
    inventory counted as in stock).
    """
    included: list[dict] = []
    skipped: list[dict] = []
    warnings: list[dict] = []
    for product in products:
        mapped, reason = map_shopify_product(product, currency=currency)
        pid = _numeric_id(product.get("id", ""))
        if mapped is None:
            skipped.append({"product_id": pid,
                            "title": (product.get("title") or "").strip(),
                            "reason": reason})
            continue
        variant = _first_variant(product)
        if variant is not None and variant.get("inventoryQuantity") is None:
            warnings.append({
                "product_id": pid, "title": mapped.title,
                "warning": "inventory not tracked; assumed in stock"})
        included.append(mapped.to_dict())
    return {"total": len(products),
            "included_count": len(included),
            "skipped_count": len(skipped),
            "included": included,
            "skipped": skipped,
            "warnings": warnings}
