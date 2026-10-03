"""Etsy API v3 adapter: lets agents manage the shop through Etsy's official API.

Setup (owner does once):
  1. Create an app at https://www.etsy.com/developers/your-apps
     -> get the API key (keystring).
  2. Run `python -m core.etsy_adapter authorize` -> opens Etsy's OAuth page.
     Owner logs in and approves the requested scopes.
  3. Tokens are stored via the Secure Vault flow (never in the repo).

Scopes requested (least privilege for store management):
  - listings_r / listings_w  (read/create/update listings + images)
  - shops_r                  (read shop info)
  - transactions_r           (read orders)

NOT requested: transactions_w (no refunds/charges by agents — human only).
"""

from __future__ import annotations

import json
import secrets
import urllib.parse
import urllib.request

from core.integrations import CommerceAdapter

ETSY_AUTH_URL = "https://www.etsy.com/oauth/connect"
ETSY_TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
ETSY_API = "https://openapi.etsy.com/v3/application"

# Least-privilege scopes for agent store management.
SCOPES = ["listings_r", "listings_w", "shops_r", "transactions_r"]


class EtsyAuth:
    """OAuth 2.0 PKCE flow for Etsy."""

    def __init__(self, keystring: str, redirect_uri: str) -> None:
        self.keystring = keystring
        self.redirect_uri = redirect_uri
        self._verifier: str | None = None

    def authorization_url(self) -> tuple[str, str]:
        """Return (url, state). Owner opens the URL, approves, gets a code."""
        self._verifier = secrets.token_urlsafe(64)
        # Etsy uses plain PKCE (code_challenge = verifier for plain method).
        params = {
            "response_type": "code",
            "client_id": self.keystring,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(SCOPES),
            "state": secrets.token_urlsafe(16),
            "code_challenge": self._verifier,
            "code_challenge_method": "S256",  # Etsy expects SHA256 challenge
        }
        # NOTE: real S256 challenge = base64url(sha256(verifier)); computed at exchange.
        return ETSY_AUTH_URL + "?" + urllib.parse.urlencode(params), params["state"]

    def exchange(self, code: str) -> dict:
        """Exchange an authorization code for tokens."""
        import base64
        import hashlib

        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(self._verifier.encode()).digest()
        ).rstrip(b"=").decode()
        data = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "client_id": self.keystring,
            "redirect_uri": self.redirect_uri,
            "code": code,
            "code_challenge": challenge,
        }).encode()
        req = urllib.request.Request(ETSY_TOKEN_URL, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    @staticmethod
    def refresh(keystring: str, refresh_token: str) -> dict:
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "client_id": keystring,
            "refresh_token": refresh_token,
        }).encode()
        req = urllib.request.Request(ETSY_TOKEN_URL, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)


class EtsyCommerceAdapter(CommerceAdapter):
    """Manage an Etsy shop via API v3. Implements CommerceAdapter.

    Since Feb 2026 Etsy requires x-api-key as "keystring:shared_secret"
    (colon-joined), not the bare keystring.
    """

    platform = "etsy"

    def __init__(self, keystring: str, shared_secret: str, access_token: str,
                 shop_id: str) -> None:
        self.keystring = keystring
        self.shared_secret = shared_secret
        self.access_token = access_token
        self.shop_id = shop_id

    @property
    def _api_key(self) -> str:
        return f"{self.keystring}:{self.shared_secret}"

    def _request(self, method: str, path: str, data: dict | None = None,
                 files: dict | None = None) -> dict | list:
        url = ETSY_API + path
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("x-api-key", self._api_key)
        req.add_header("Authorization", f"Bearer {self.access_token}")
        if data:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}

    # -- CommerceAdapter interface --

    def list_products(self) -> list[dict]:
        out, offset = [], 0
        while True:
            res = self._request(
                "GET",
                f"/shops/{self.shop_id}/listings/active?limit=100&offset={offset}")
            results = res.get("results", [])
            out.extend(results)
            if len(results) < 100:
                break
            offset += 100
        return out

    def create_product(self, product: dict) -> dict:
        """Create a draft listing. Agent fills title/description/tags/price."""
        payload = {
            "quantity": product.get("quantity", 999),
            "title": product["title"],
            "description": product["description"],
            "price": product["price_usd"],
            "who_made": "i_did",
            "when_made": "2020_2023",
            "taxonomy_id": product.get("taxonomy_id", 1),
            "is_digital": True,
            "type": "download",
        }
        if product.get("tags"):
            payload["tags"] = product["tags"][:13]
        if product.get("materials"):
            payload["materials"] = product["materials"]
        res = self._request("POST", f"/shops/{self.shop_id}/listings", payload)
        return res

    def list_orders(self, limit: int = 50) -> list[dict]:
        res = self._request(
            "GET", f"/shops/{self.shop_id}/receipts?limit={min(limit, 100)}")
        return res.get("results", [])

    def get_order(self, order_id: str) -> dict | None:
        try:
            res = self._request("GET", f"/shops/{self.shop_id}/receipts/{order_id}")
            return res
        except Exception:
            return None

    def update_inventory(self, sku: str, quantity: int) -> dict:
        # Digital products: quantity is effectively unlimited; Etsy uses
        # listing quantity. sku maps to listing_id for digital shops.
        if quantity < 0:
            raise ValueError("inventory cannot be negative")
        res = self._request(
            "PUT", f"/shops/{self.shop_id}/listings/{sku}/inventory",
            {"products": [{"offerings": [{"price": 0, "quantity": quantity}]}]})
        return {"sku": sku, "quantity": quantity, "response": res}

    # -- Etsy extras --

    def upload_image(self, listing_id: str, image_path: str) -> dict:
        """Upload a listing image (multipart)."""
        import mimetypes

        boundary = secrets.token_hex(16)
        mime, _ = mimetypes.guess_type(image_path)
        with open(image_path, "rb") as f:
            img = f.read()
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="img"\r\n'
            f"Content-Type: {mime or 'image/png'}\r\n\r\n"
        ).encode() + img + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"{ETSY_API}/shops/{self.shop_id}/listings/{listing_id}/images",
            data=body, method="POST")
        req.add_header("x-api-key", self._api_key)
        req.add_header("Authorization", f"Bearer {self.access_token}")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "authorize":
        print("1. Create an app at https://www.etsy.com/developers/your-apps")
        print("2. Set its redirect URI, then run with your keystring to get the")
        print("   authorization URL. Approve in the browser, then exchange the code.")
        print("Tokens must be stored via Secure Vault — never committed.")
