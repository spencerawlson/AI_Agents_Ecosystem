"""Shopify Admin GraphQL adapter: lets agents manage the store via the
official API. Implements CommerceAdapter.

Setup (owner does once):
  1. Create an app for the store in the Shopify Dev Dashboard (or a
     legacy custom app in the store admin) with the scopes below.
  2. Install it on the store, then provide credentials via env vars
     (never committed):
       SHOPIFY_SHOP          your-store.myshopify.com
       SHOPIFY_ACCESS_TOKEN  shpat_...   (static admin API token), OR
       SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET
                             (client-credentials grant; the adapter
                             fetches and refreshes short-lived tokens)
       SHOPIFY_API_VERSION   optional, defaults to DEFAULT_API_VERSION
  3. Verify: python ecosystem/shopify_pipeline.py check

Scopes (least privilege for store management):
  read_products / write_products        products, variants, SEO, media
  read_inventory / write_inventory      stock levels
  read_locations                        where stock lives
  read_publications / write_publications  publish to the Online Store
  read_orders                           order monitoring
  read_content / write_content          SEO blog articles (created hidden)

NOT requested: write_orders, refunds, payments, store settings — those
stay human-only.

Products are created as DRAFT. Going live is a separate call
(publish_product) that the pipeline only makes after owner approval.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from core.integrations import CommerceAdapter

log = logging.getLogger("core.shopify")

DEFAULT_API_VERSION = "2026-07"
SCOPES = [
    "read_products", "write_products",
    "read_inventory", "write_inventory",
    "read_locations",
    "read_publications", "write_publications",
    "read_orders",
    "read_content", "write_content",
]

# transport(method, url, headers, body_bytes) -> (status, response_text)
Transport = Callable[[str, str, dict, bytes | None], tuple[int, str]]


class ShopifyError(RuntimeError):
    pass


class ShopifyCredentialsError(ShopifyError):
    pass


def _urllib_transport(method: str, url: str, headers: dict,
                      body: bytes | None) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


def _gid(kind: str, value: str | int) -> str:
    value = str(value)
    return value if value.startswith("gid://") else f"gid://shopify/{kind}/{value}"


# -- GraphQL documents ------------------------------------------------------

_SHOP_Q = "query { shop { name myshopifyDomain currencyCode plan { displayName } } }"

_PRODUCT_FIELDS = """
  id title handle status vendor productType tags onlineStoreUrl
  seo { title description }
  variants(first: 1) { nodes { id sku price inventoryQuantity } }
"""

_PRODUCTS_Q = """
query($cursor: String) {
  products(first: 100, after: $cursor) {
    nodes { %s }
    pageInfo { hasNextPage endCursor }
  }
}""" % _PRODUCT_FIELDS

_PRODUCT_CREATE_M = """
mutation($product: ProductCreateInput!, $media: [CreateMediaInput!]) {
  productCreate(product: $product, media: $media) {
    product { %s }
    userErrors { field message }
  }
}""" % _PRODUCT_FIELDS

_VARIANTS_UPDATE_M = """
mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants { id sku price }
    userErrors { field message }
  }
}"""

_PRODUCT_UPDATE_M = """
mutation($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product { %s }
    userErrors { field message }
  }
}""" % _PRODUCT_FIELDS

_PUBLICATIONS_Q = "query { publications(first: 25) { nodes { id name } } }"

_PUBLISH_M = """
mutation($id: ID!, $input: [PublicationInput!]!) {
  publishablePublish(id: $id, input: $input) {
    userErrors { field message }
  }
}"""

_ORDER_FIELDS = """
  id name createdAt displayFinancialStatus displayFulfillmentStatus
  totalPriceSet { shopMoney { amount currencyCode } }
  lineItems(first: 50) { nodes { title sku quantity product { id } } }
"""

_ORDERS_Q = """
query($first: Int!) {
  orders(first: $first, reverse: true, sortKey: CREATED_AT) {
    nodes { %s }
  }
}""" % _ORDER_FIELDS

_ORDER_Q = "query($id: ID!) { order(id: $id) { %s } }" % _ORDER_FIELDS

_ORDERS_SINCE_Q = """
query($cursor: String, $q: String!) {
  orders(first: 100, after: $cursor, query: $q, sortKey: CREATED_AT) {
    nodes { %s }
    pageInfo { hasNextPage endCursor }
  }
}""" % _ORDER_FIELDS

_PRODUCT_DETAIL_Q = """
query($id: ID!) {
  product(id: $id) {
    %s
    descriptionHtml
    media(first: 10) { nodes { alt mediaContentType } }
  }
}""" % _PRODUCT_FIELDS

_BLOGS_Q = "query { blogs(first: 5) { nodes { id title handle } } }"

_ARTICLE_CREATE_M = """
mutation($article: ArticleCreateInput!) {
  articleCreate(article: $article) {
    article { id title handle isPublished }
    userErrors { field message }
  }
}"""

_VARIANT_BY_SKU_Q = """
query($q: String!) {
  productVariants(first: 1, query: $q) {
    nodes { id sku inventoryItem { id } }
  }
}"""

_LOCATIONS_Q = "query { locations(first: 1) { nodes { id name } } }"

_SET_QTY_M = """
mutation($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    userErrors { field message }
  }
}"""


class ShopifyCommerceAdapter(CommerceAdapter):
    platform = "shopify"

    def __init__(self, shop: str, access_token: str | None = None,
                 client_id: str | None = None,
                 client_secret: str | None = None,
                 api_version: str = DEFAULT_API_VERSION,
                 transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 max_retries: int = 3) -> None:
        shop = shop.strip().removeprefix("https://").rstrip("/")
        if not shop:
            raise ShopifyCredentialsError("shop domain is required")
        if not access_token and not (client_id and client_secret):
            raise ShopifyCredentialsError(
                "provide access_token or client_id + client_secret")
        self.shop = shop
        self.api_version = api_version
        self._token = access_token
        self._token_expires_at: float | None = None
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport or _urllib_transport
        self._sleep = sleep
        self._max_retries = max_retries
        self._online_store_publication: str | None = None
        self._location_id: str | None = None

    @classmethod
    def from_env(cls, **kwargs) -> "ShopifyCommerceAdapter":
        shop = os.environ.get("SHOPIFY_SHOP", "")
        if not shop:
            raise ShopifyCredentialsError("SHOPIFY_SHOP is not set")
        return cls(
            shop,
            access_token=os.environ.get("SHOPIFY_ACCESS_TOKEN") or None,
            client_id=os.environ.get("SHOPIFY_CLIENT_ID") or None,
            client_secret=os.environ.get("SHOPIFY_CLIENT_SECRET") or None,
            api_version=os.environ.get("SHOPIFY_API_VERSION",
                                       DEFAULT_API_VERSION),
            **kwargs,
        )

    @staticmethod
    def configured() -> bool:
        return bool(os.environ.get("SHOPIFY_SHOP") and (
            os.environ.get("SHOPIFY_ACCESS_TOKEN") or (
                os.environ.get("SHOPIFY_CLIENT_ID")
                and os.environ.get("SHOPIFY_CLIENT_SECRET"))))

    # -- auth -------------------------------------------------------------

    def _can_refresh(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def _fetch_token(self) -> None:
        """Client-credentials grant -> short-lived admin access token."""
        body = json.dumps({
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "client_credentials",
        }).encode()
        status, text = self._transport(
            "POST", f"https://{self.shop}/admin/oauth/access_token",
            {"Content-Type": "application/json",
             "Accept": "application/json"}, body)
        if status != 200:
            raise ShopifyCredentialsError(
                f"token request failed ({status}): {text[:200]}")
        data = json.loads(text)
        self._token = data["access_token"]
        expires_in = data.get("expires_in")
        # Refresh a minute early so a request never races expiry.
        self._token_expires_at = (time.time() + float(expires_in) - 60
                                  if expires_in else None)

    def _access_token(self) -> str:
        expired = (self._token_expires_at is not None
                   and time.time() >= self._token_expires_at)
        if (not self._token or expired) and self._can_refresh():
            self._fetch_token()
        if not self._token:
            raise ShopifyCredentialsError("no access token")
        return self._token

    # -- transport --------------------------------------------------------

    @property
    def endpoint(self) -> str:
        return f"https://{self.shop}/admin/api/{self.api_version}/graphql.json"

    def graphql(self, query: str, variables: dict | None = None) -> dict:
        """Run a GraphQL operation; returns `data`. Retries on throttling."""
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        refreshed = False
        for attempt in range(self._max_retries + 1):
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Shopify-Access-Token": self._access_token(),
            }
            status, text = self._transport("POST", self.endpoint, headers, body)
            if status == 401 and self._can_refresh() and not refreshed:
                self._fetch_token()
                refreshed = True
                continue
            if status == 429 or status >= 500:
                if attempt < self._max_retries:
                    self._sleep(2 ** attempt)
                    continue
                raise ShopifyError(f"Shopify HTTP {status} after retries")
            if status != 200:
                raise ShopifyError(f"Shopify HTTP {status}: {text[:300]}")
            payload = json.loads(text)
            errors = payload.get("errors")
            if errors:
                throttled = any(
                    (e.get("extensions") or {}).get("code") == "THROTTLED"
                    for e in errors if isinstance(e, dict))
                if throttled and attempt < self._max_retries:
                    self._sleep(2 ** attempt)
                    continue
                raise ShopifyError(f"GraphQL errors: {errors}")
            return payload.get("data") or {}
        raise ShopifyError("Shopify request failed after retries")

    @staticmethod
    def _check_user_errors(result: dict, op: str) -> dict:
        errs = (result or {}).get("userErrors") or []
        if errs:
            msg = "; ".join(f"{e.get('field')}: {e.get('message')}" for e in errs)
            raise ShopifyError(f"{op} rejected: {msg}")
        return result

    # -- CommerceAdapter interface ---------------------------------------

    def shop_info(self) -> dict:
        return self.graphql(_SHOP_Q)["shop"]

    def list_products(self) -> list[dict]:
        out, cursor = [], None
        while True:
            page = self.graphql(_PRODUCTS_Q, {"cursor": cursor})["products"]
            out.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return out
            cursor = page["pageInfo"]["endCursor"]

    def create_product(self, product: dict) -> dict:
        """Create a DRAFT product with price, SKU, cost, SEO and images.

        product keys: title, description_html, price_usd, sku, cost_usd,
        vendor, product_type, tags, seo_title, seo_description,
        image_urls, track_inventory (default False, i.e. dropship).
        """
        product_input: dict[str, Any] = {
            "title": product["title"],
            "descriptionHtml": product.get("description_html", ""),
            "status": "DRAFT",
        }
        for src, dst in (("vendor", "vendor"), ("product_type", "productType")):
            if product.get(src):
                product_input[dst] = product[src]
        if product.get("tags"):
            product_input["tags"] = list(product["tags"])
        if product.get("seo_title") or product.get("seo_description"):
            product_input["seo"] = {
                "title": product.get("seo_title", ""),
                "description": product.get("seo_description", ""),
            }
        media = [
            {"originalSource": url, "mediaContentType": "IMAGE",
             "alt": product.get("image_alt") or product["title"]}
            for url in product.get("image_urls") or []
        ]
        res = self._check_user_errors(
            self.graphql(_PRODUCT_CREATE_M,
                         {"product": product_input, "media": media or None}
                         )["productCreate"], "productCreate")
        created = res["product"]

        variant_nodes = created["variants"]["nodes"]
        if variant_nodes and product.get("price_usd") is not None:
            inventory_item: dict[str, Any] = {
                "tracked": bool(product.get("track_inventory", False))}
            if product.get("sku"):
                inventory_item["sku"] = product["sku"]
            if product.get("cost_usd") is not None:
                inventory_item["cost"] = f"{product['cost_usd']:.2f}"
            variant = {
                "id": variant_nodes[0]["id"],
                "price": f"{product['price_usd']:.2f}",
                "inventoryItem": inventory_item,
            }
            upd = self._check_user_errors(
                self.graphql(_VARIANTS_UPDATE_M, {
                    "productId": created["id"], "variants": [variant],
                })["productVariantsBulkUpdate"], "productVariantsBulkUpdate")
            created["variants"]["nodes"] = upd["productVariants"]
        return created

    def list_orders(self, limit: int = 50) -> list[dict]:
        first = max(1, min(limit, 250))
        return self.graphql(_ORDERS_Q, {"first": first})["orders"]["nodes"]

    def get_order(self, order_id: str) -> dict | None:
        try:
            return self.graphql(_ORDER_Q, {"id": _gid("Order", order_id)})["order"]
        except ShopifyError:
            return None

    def update_inventory(self, sku: str, quantity: int) -> dict:
        if quantity < 0:
            raise ValueError("inventory cannot be negative")
        nodes = self.graphql(_VARIANT_BY_SKU_Q,
                             {"q": f"sku:{sku}"})["productVariants"]["nodes"]
        if not nodes:
            raise ShopifyError(f"no variant with sku {sku!r}")
        item_id = nodes[0]["inventoryItem"]["id"]
        self._check_user_errors(
            self.graphql(_SET_QTY_M, {"input": {
                "name": "available",
                "reason": "correction",
                "ignoreCompareQuantity": True,
                "quantities": [{"inventoryItemId": item_id,
                                "locationId": self._primary_location(),
                                "quantity": quantity}],
            }})["inventorySetQuantities"], "inventorySetQuantities")
        return {"sku": sku, "quantity": quantity}

    # -- Shopify extras ----------------------------------------------------

    def _primary_location(self) -> str:
        if self._location_id is None:
            nodes = self.graphql(_LOCATIONS_Q)["locations"]["nodes"]
            if not nodes:
                raise ShopifyError("store has no locations")
            self._location_id = nodes[0]["id"]
        return self._location_id

    def online_store_publication_id(self) -> str:
        override = os.environ.get("SHOPIFY_PUBLICATION_ID")
        if override:
            return _gid("Publication", override)
        if self._online_store_publication is None:
            nodes = self.graphql(_PUBLICATIONS_Q)["publications"]["nodes"]
            match = next((n for n in nodes
                          if (n.get("name") or "").lower() == "online store"),
                         None)
            if match is None:
                raise ShopifyError(
                    "Online Store sales channel not found; set "
                    "SHOPIFY_PUBLICATION_ID")
            self._online_store_publication = match["id"]
        return self._online_store_publication

    def publish_product(self, product_id: str) -> dict:
        """Make a product live: status ACTIVE + published to Online Store."""
        pid = _gid("Product", product_id)
        res = self._check_user_errors(
            self.graphql(_PRODUCT_UPDATE_M, {
                "product": {"id": pid, "status": "ACTIVE"},
            })["productUpdate"], "productUpdate")
        self._check_user_errors(
            self.graphql(_PUBLISH_M, {
                "id": pid,
                "input": [{"publicationId": self.online_store_publication_id()}],
            })["publishablePublish"], "publishablePublish")
        return res["product"]

    def get_product(self, product_id: str) -> dict | None:
        return self.graphql(_PRODUCT_DETAIL_Q,
                            {"id": _gid("Product", product_id)})["product"]

    def list_orders_since(self, since_iso: str) -> list[dict]:
        """All orders created at/after since_iso (paginated)."""
        out, cursor = [], None
        q = f"created_at:>='{since_iso}'"
        while True:
            page = self.graphql(_ORDERS_SINCE_Q, {"cursor": cursor, "q": q})["orders"]
            out.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return out
            cursor = page["pageInfo"]["endCursor"]

    def create_article(self, title: str, body_html: str, tags: list[str],
                       summary: str = "", author: str = "Store Team",
                       published: bool = False) -> dict:
        """Create a blog article (hidden by default) on the first blog."""
        blogs = self.graphql(_BLOGS_Q)["blogs"]["nodes"]
        if not blogs:
            raise ShopifyError("store has no blog; create one in Online Store > Blog posts")
        res = self._check_user_errors(
            self.graphql(_ARTICLE_CREATE_M, {"article": {
                "blogId": blogs[0]["id"], "title": title, "body": body_html,
                "summary": summary, "tags": tags, "isPublished": published,
                "author": {"name": author},
            }})["articleCreate"], "articleCreate")
        return res["article"]

    def update_seo(self, product_id: str, title: str, description: str) -> dict:
        res = self._check_user_errors(
            self.graphql(_PRODUCT_UPDATE_M, {"product": {
                "id": _gid("Product", product_id),
                "seo": {"title": title, "description": description},
            }})["productUpdate"], "productUpdate")
        return res["product"]
