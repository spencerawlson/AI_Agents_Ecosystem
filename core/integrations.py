"""Integration layer: adapter interface for external platforms.

Commerce, marketing, and finance providers plug in through adapters.
Swapping a provider never requires redesigning the agent system.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class CommerceAdapter(ABC):
    """Interface for storefront platforms (Shopify, Etsy, custom)."""

    platform: str = "base"

    @abstractmethod
    def list_products(self) -> list[dict]: ...

    @abstractmethod
    def create_product(self, product: dict) -> dict: ...

    @abstractmethod
    def list_orders(self, limit: int = 50) -> list[dict]: ...

    @abstractmethod
    def get_order(self, order_id: str) -> dict | None: ...

    @abstractmethod
    def update_inventory(self, sku: str, quantity: int) -> dict: ...


class MockCommerceAdapter(CommerceAdapter):
    """In-memory commerce adapter for development and tests."""

    platform = "mock"

    def __init__(self) -> None:
        self._products: dict[str, dict] = {}
        self._orders: dict[str, dict] = {}
        self._inventory: dict[str, int] = {}
        self._counter = 0

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self._counter:04d}"

    def list_products(self) -> list[dict]:
        return list(self._products.values())

    def create_product(self, product: dict) -> dict:
        pid = self._next_id("prod")
        record = {"id": pid, **product}
        self._products[pid] = record
        if "sku" in product:
            self._inventory[product["sku"]] = product.get("inventory", 0)
        return record

    def list_orders(self, limit: int = 50) -> list[dict]:
        return list(self._orders.values())[:limit]

    def get_order(self, order_id: str) -> dict | None:
        return self._orders.get(order_id)

    def update_inventory(self, sku: str, quantity: int) -> dict:
        if quantity < 0:
            raise ValueError("inventory cannot be negative")
        self._inventory[sku] = quantity
        return {"sku": sku, "quantity": quantity}

    # Test helper: simulate an incoming order.
    def simulate_order(self, product_id: str, quantity: int = 1) -> dict:
        product = self._products.get(product_id)
        if product is None:
            raise ValueError(f"unknown product: {product_id}")
        oid = self._next_id("order")
        order = {
            "id": oid,
            "product_id": product_id,
            "quantity": quantity,
            "total_usd": product.get("price_usd", 0) * quantity,
            "status": "pending",
        }
        self._orders[oid] = order
        return order


class AdapterRegistry:
    """Holds the configured adapters per business."""

    def __init__(self) -> None:
        self._commerce: dict[str, CommerceAdapter] = {}

    def register_commerce(self, business_id: str, adapter: CommerceAdapter) -> None:
        self._commerce[business_id] = adapter

    def commerce(self, business_id: str) -> CommerceAdapter | None:
        return self._commerce.get(business_id)


def adapter_info(adapter: Any) -> dict:
    return {"platform": getattr(adapter, "platform", "unknown")}