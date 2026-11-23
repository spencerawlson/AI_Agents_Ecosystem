"""Reddit Ads catalog sync (Phase B): Shopify -> Reddit mapping + dry-run.

No live calls: the mapper is a pure function over Shopify product nodes
and the dry-run only reports what WOULD sync. The push to Reddit is a
separate approval — there is no push code in this phase.

Reddit catalog payload field names follow the design doc's
cross-verified surface (Google-Merchant-style feed fields); the OpenAPI
spec is the build-time authority before any live write.
"""

import json

import pytest

from core.reddit_catalog import (
    RedditCatalogProduct,
    dry_run_catalog_sync,
    map_shopify_product,
)
from core.shopify_adapter import ShopifyCommerceAdapter


def _product(**kw):
    base = {
        "id": "gid://shopify/Product/123",
        "title": "Aluminium Laptop Stand",
        "handle": "laptop-stand",
        "status": "ACTIVE",
        "vendor": "Acme",
        "productType": "Accessories",
        "tags": ["laptop"],
        "onlineStoreUrl": "https://shop.example/products/laptop-stand",
        "seo": {"title": "Stand", "description": "SEO blurb"},
        "descriptionHtml": "<p>Holds laptops 10-17 in. Folds flat.</p>",
        "featuredMedia": {"__typename": "MediaImage",
                          "image": {"url": "https://img.example/1.jpg"}},
        "variants": {"nodes": [{"id": "gid://shopify/ProductVariant/9",
                                "sku": "STAND-1", "price": "19.99",
                                "inventoryQuantity": 5}]},
    }
    base.update(kw)
    return base


# -- mapping: the happy path ----------------------------------------------------

def test_maps_full_product_exactly():
    product, reason = map_shopify_product(_product())
    assert reason is None
    assert isinstance(product, RedditCatalogProduct)
    assert product.to_dict() == {
        "id": "123",
        "title": "Aluminium Laptop Stand",
        "description": "Holds laptops 10-17 in. Folds flat.",
        "price": "19.99 USD",
        "image_link": "https://img.example/1.jpg",
        "link": "https://shop.example/products/laptop-stand",
        "availability": "in_stock",
        "brand": "Acme",
    }


def test_description_falls_back_to_seo_when_no_body():
    product, _ = map_shopify_product(_product(descriptionHtml=""))
    assert product.description == "SEO blurb"


def test_description_empty_when_neither_body_nor_seo():
    product, _ = map_shopify_product(
        _product(descriptionHtml="", seo={"title": "", "description": ""}))
    assert product.description == ""


def test_description_html_is_stripped_to_plain_text():
    product, _ = map_shopify_product(_product(
        descriptionHtml="<p>First line.</p><ul><li>One</li><li>Two</li></ul>"))
    assert product.description == "First line. One Two"
    assert "<" not in product.description


def test_price_uses_given_currency():
    product, _ = map_shopify_product(_product(), currency="CAD")
    assert product.price == "19.99 CAD"


# -- mapping: skip reasons ------------------------------------------------------

@pytest.mark.parametrize("overrides,reason", [
    ({"status": "DRAFT"}, "not_active"),
    ({"status": "ARCHIVED"}, "not_active"),
    ({"title": ""}, "no_title"),
    ({"featuredMedia": None}, "no_image"),
    ({"featuredMedia": {"__typename": "ExternalVideo"}}, "no_image"),
    ({"featuredMedia": {"__typename": "MediaImage", "image": None}}, "no_image"),
    ({"onlineStoreUrl": None}, "no_url"),
    ({"onlineStoreUrl": ""}, "no_url"),
    ({"variants": {"nodes": []}}, "no_price"),
    ({"variants": {"nodes": [{"id": "gid://shopify/ProductVariant/9",
                              "sku": "S", "price": None,
                              "inventoryQuantity": 5}]}}, "no_price"),
    ({"variants": {"nodes": [{"id": "gid://shopify/ProductVariant/9",
                              "sku": "S", "price": "19.99",
                              "inventoryQuantity": 0}]}}, "out_of_stock"),
])
def test_skip_reasons(overrides, reason):
    product, got = map_shopify_product(_product(**overrides))
    assert product is None
    assert got == reason


def test_untracked_inventory_is_in_stock_with_a_warning():
    p = _product()
    p["variants"]["nodes"][0]["inventoryQuantity"] = None
    product, reason = map_shopify_product(p)
    assert reason is None
    assert product.availability == "in_stock"
    report = dry_run_catalog_sync([p])
    assert len(report["warnings"]) == 1
    assert report["warnings"][0]["product_id"] == "123"
    assert "not tracked" in report["warnings"][0]["warning"]


# -- dry-run report ---------------------------------------------------------------

def test_dry_run_report_is_exact():
    products = [
        _product(),  # in
        _product(id="gid://shopify/Product/124", title="No Image",
                 featuredMedia=None),  # skipped: no_image
        _product(id="gid://shopify/Product/125", title="Sold Out",
                 variants={"nodes": [{"id": "gid://shopify/ProductVariant/10",
                                      "sku": "S", "price": "9.99",
                                      "inventoryQuantity": 0}]}),  # out_of_stock
        _product(id="gid://shopify/Product/126", title="Draft One",
                 status="DRAFT"),  # not_active
    ]
    report = dry_run_catalog_sync(products)
    assert report["total"] == 4
    assert report["included_count"] == 1
    assert report["skipped_count"] == 3
    assert [p["id"] for p in report["included"]] == ["123"]
    assert report["skipped"] == [
        {"product_id": "124", "title": "No Image", "reason": "no_image"},
        {"product_id": "125", "title": "Sold Out", "reason": "out_of_stock"},
        {"product_id": "126", "title": "Draft One", "reason": "not_active"},
    ]
    assert report["warnings"] == []


def test_dry_run_empty_catalog():
    report = dry_run_catalog_sync([])
    assert report == {"total": 0, "included_count": 0, "skipped_count": 0,
                      "included": [], "skipped": [], "warnings": []}


# -- the Shopify query actually fetches what the mapper needs --------------------

class _FakeShopifyProducts:
    """Answers the products query with canned nodes; records the query."""

    def __init__(self, nodes):
        self.nodes = nodes
        self.queries = []

    def __call__(self, method, url, headers, body):
        payload = json.loads(body) if body else {}
        q = payload.get("query", "")
        self.queries.append(q)
        assert "products(first" in q
        return 200, json.dumps({"data": {"products": {
            "nodes": self.nodes,
            "pageInfo": {"hasNextPage": False, "endCursor": None}}}})


def test_list_products_fetches_media_and_description():
    fake = _FakeShopifyProducts([_product()])
    adapter = ShopifyCommerceAdapter("demo.myshopify.com",
                                     access_token="shpat_test", transport=fake,
                                     sleep=lambda s: None)
    nodes = adapter.list_products()
    assert len(nodes) == 1
    assert nodes[0]["featuredMedia"]["image"]["url"] == "https://img.example/1.jpg"
    assert nodes[0]["descriptionHtml"].startswith("<p>Holds laptops")
    # The query itself must ask for the fields the mapper needs.
    assert "featuredMedia" in fake.queries[0]
    assert "descriptionHtml" in fake.queries[0]
    assert "MediaImage" in fake.queries[0]
