"""CJ Dropshipping order placement and tracking.

CJ API v2 calls used:
  GET  /product/variant/queryByVid      current unit price of a variant
  POST /logistic/freightCalculate       current shipping for the order
  POST /shopping/order/createOrderV2    create the supplier order
  POST /shopping/pay/payBalance         pay it from the CJ wallet (only
                                        when CJ_AUTO_PAY=true)
  GET  /shopping/order/getOrderDetail   status + tracking number

Paying from the CJ wallet caps spend at whatever balance the owner
loaded. With auto-pay off, orders are created in CJ and the owner pays
them in the CJ dashboard (still no manual data entry).
"""

from __future__ import annotations

from dataclasses import dataclass

from core.supplier_search import (CJClient, SupplierSearchError, _first_price,
                                  _parse_days)

TRACKING_URL = "https://t.17track.net/en#nums={number}"


@dataclass
class OrderQuote:
    product_cost_usd: float
    shipping_cost_usd: float
    shipping_method: str
    lead_time_days: int | None

    @property
    def total_usd(self) -> float:
        return round(self.product_cost_usd + self.shipping_cost_usd, 2)


@dataclass
class SupplierOrderStatus:
    status: str
    tracking_number: str | None
    carrier: str | None
    amount_usd: float | None


class CJOrders(CJClient):
    def quote(self, items: list[dict], country_code: str,
              preferred_method: str = "") -> OrderQuote:
        """items: [{"vid": str, "quantity": int}] -> current landed cost."""
        product_cost = 0.0
        for it in items:
            data = self._call("GET", "/product/variant/queryByVid",
                              params={"vid": it["vid"]}) or {}
            price = _first_price(data.get("variantSellPrice"))
            if price is None:
                raise SupplierSearchError(f"no current price for variant {it['vid']}")
            product_cost += price * int(it["quantity"])
        options = self._call("POST", "/logistic/freightCalculate", body={
            "startCountryCode": "CN", "endCountryCode": country_code,
            "products": [{"vid": i["vid"], "quantity": int(i["quantity"])} for i in items],
        }) or []
        priced = [(_first_price(o.get("logisticPrice")), o) for o in options]
        priced = [(p, o) for p, o in priced if p is not None]
        if not priced:
            raise SupplierSearchError(f"CJ cannot ship this order to {country_code}")
        chosen = next(((p, o) for p, o in priced
                       if preferred_method and o.get("logisticName") == preferred_method),
                      min(priced, key=lambda x: x[0]))
        price, opt = chosen
        return OrderQuote(round(product_cost, 2), round(price, 2),
                          str(opt.get("logisticName", "")),
                          _parse_days(opt.get("logisticAging")))

    def create_order(self, order_number: str, address: dict, items: list[dict],
                     shipping_method: str) -> str:
        """Create the supplier order; returns CJ's order id.

        address keys: name, phone, email, address1, address2, city,
        province, zip, country, country_code.
        """
        data = self._call("POST", "/shopping/order/createOrderV2", body={
            "orderNumber": order_number,
            "shippingCustomerName": address.get("name", ""),
            "shippingPhone": address.get("phone") or "",
            "email": address.get("email") or "",
            "shippingAddress": address.get("address1", ""),
            "shippingAddress2": address.get("address2") or "",
            "shippingCity": address.get("city", ""),
            "shippingProvince": address.get("province") or "",
            "shippingZip": address.get("zip") or "",
            "shippingCountry": address.get("country", ""),
            "shippingCountryCode": address.get("country_code", ""),
            "logisticName": shipping_method,
            "fromCountryCode": "CN",
            "products": [{"vid": i["vid"], "quantity": int(i["quantity"])} for i in items],
        }) or {}
        order_id = data.get("orderId") if isinstance(data, dict) else data
        if not order_id:
            raise SupplierSearchError(f"CJ returned no order id: {data}")
        return str(order_id)

    def pay_from_balance(self, order_id: str) -> None:
        self._call("POST", "/shopping/pay/payBalance", body={"orderId": order_id})

    def order_status(self, order_id: str) -> SupplierOrderStatus:
        data = self._call("GET", "/shopping/order/getOrderDetail",
                          params={"orderId": order_id}) or {}
        tracking = data.get("trackNumber") or data.get("trackingNumber") or None
        return SupplierOrderStatus(
            status=str(data.get("orderStatus", "")),
            tracking_number=str(tracking) if tracking else None,
            carrier=data.get("logisticName"),
            amount_usd=_first_price(data.get("orderAmount")),
        )
