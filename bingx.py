"""Cliente mínimo de BingX Perpetual Futures (API v2, firmado con HMAC-SHA256).

Docs: https://bingx-api.github.io/docs/#/en-us/swapV2/
Demo (VST): https://open-api-vst.bingx.com  (mismas rutas, saldo virtual)
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from decimal import ROUND_DOWN, Decimal
from typing import Any, Optional

import httpx

log = logging.getLogger("bingx")


class BingXError(RuntimeError):
    pass


def floor_to(value: float, decimals: int) -> float:
    q = Decimal(1).scaleb(-decimals)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_DOWN))


class BingX:
    def __init__(self, base_url: str, api_key: str, api_secret: str, position_mode: str = "hedge",
                 client: Optional[httpx.AsyncClient] = None) -> None:
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.secret = api_secret
        self.position_mode = position_mode
        self.http = client or httpx.AsyncClient(timeout=10.0)
        self._contracts: dict[str, dict] = {}

    # ------------------------------------------------------------------ firma
    def _sign(self, query: str) -> str:
        return hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    async def _request(self, method: str, path: str, params: Optional[dict] = None, signed: bool = True) -> Any:
        p = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {}
        if signed:
            p["timestamp"] = int(time.time() * 1000)
            p["recvWindow"] = 5000
            query = "&".join(f"{k}={p[k]}" for k in sorted(p))
            query += f"&signature={self._sign(query)}"
            headers["X-BX-APIKEY"] = self.key
        else:
            query = "&".join(f"{k}={p[k]}" for k in sorted(p))
        url = f"{self.base}{path}" + (f"?{query}" if query else "")
        r = await self.http.request(method, url, headers=headers)
        try:
            data = r.json()
        except ValueError:
            raise BingXError(f"Respuesta no JSON ({r.status_code}): {r.text[:200]}")
        if isinstance(data, dict) and data.get("code", 0) not in (0, "0"):
            raise BingXError(f"{path}: code={data.get('code')} msg={data.get('msg')}")
        return data.get("data") if isinstance(data, dict) else data

    # ------------------------------------------------------------- mercado
    async def contract(self, symbol: str) -> dict:
        if not self._contracts:
            data = await self._request("GET", "/openApi/swap/v2/quote/contracts", signed=False)
            self._contracts = {c["symbol"]: c for c in (data or [])}
        c = self._contracts.get(symbol)
        if not c:
            raise BingXError(f"Contrato {symbol} no existe en BingX")
        return c

    async def qty_rules(self, symbol: str) -> tuple[int, float]:
        c = await self.contract(symbol)
        prec = int(c.get("quantityPrecision", 3))
        min_qty = float(c.get("tradeMinQuantity") or c.get("size") or 0)
        return prec, min_qty

    async def price_precision(self, symbol: str) -> int:
        c = await self.contract(symbol)
        return int(c.get("pricePrecision", 2))

    async def last_price(self, symbol: str) -> float:
        data = await self._request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol}, signed=False)
        if isinstance(data, list):
            data = data[0]
        return float(data["price"])

    # --------------------------------------------------------------- cuenta
    async def equity(self) -> float:
        data = await self._request("GET", "/openApi/swap/v2/user/balance")
        bal = data.get("balance", data) if isinstance(data, dict) else data[0]
        return float(bal.get("equity") or bal.get("balance"))

    async def positions(self, symbol: str) -> list[dict]:
        data = await self._request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
        return [p for p in (data or []) if abs(float(p.get("positionAmt", 0))) > 0]

    async def open_orders(self, symbol: str) -> list[dict]:
        data = await self._request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol})
        return (data or {}).get("orders", []) if isinstance(data, dict) else (data or [])

    # ------------------------------------------------------------ ajustes
    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        try:
            await self._request("POST", "/openApi/swap/v2/trade/marginType", {"symbol": symbol, "marginType": margin_type})
        except BingXError as e:   # ya estaba configurado
            log.info("marginType: %s", e)

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        for s in ("LONG", "SHORT"):
            try:
                await self._request("POST", "/openApi/swap/v2/trade/leverage", {"symbol": symbol, "side": s, "leverage": leverage})
            except BingXError as e:
                log.info("leverage %s: %s", s, e)

    # ------------------------------------------------------------- órdenes
    def _pos_side(self, trade_side: str) -> str:
        return trade_side if self.position_mode == "hedge" else "BOTH"

    @staticmethod
    def _order_id(data: Any) -> str:
        if isinstance(data, dict):
            o = data.get("order", data)
            return str(o.get("orderId"))
        raise BingXError(f"Respuesta de orden inesperada: {data}")

    async def market_open(self, symbol: str, trade_side: str, qty: float) -> str:
        data = await self._request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol,
            "type": "MARKET",
            "side": "BUY" if trade_side == "LONG" else "SELL",
            "positionSide": self._pos_side(trade_side),
            "quantity": qty,
        })
        return self._order_id(data)

    async def market_close(self, symbol: str, trade_side: str, qty: float) -> str:
        params = {
            "symbol": symbol,
            "type": "MARKET",
            "side": "SELL" if trade_side == "LONG" else "BUY",
            "positionSide": self._pos_side(trade_side),
            "quantity": qty,
        }
        if self.position_mode == "oneway":
            params["reduceOnly"] = "true"
        return self._order_id(await self._request("POST", "/openApi/swap/v2/trade/order", params))

    async def conditional(self, symbol: str, trade_side: str, qty: float, stop_price: float, kind: str) -> str:
        """kind: STOP_MARKET (stop-loss) | TAKE_PROFIT_MARKET (objetivo). Siempre cierra la posición."""
        params = {
            "symbol": symbol,
            "type": kind,
            "side": "SELL" if trade_side == "LONG" else "BUY",
            "positionSide": self._pos_side(trade_side),
            "quantity": qty,
            "stopPrice": stop_price,
            "workingType": "MARK_PRICE",
        }
        if self.position_mode == "oneway":
            params["reduceOnly"] = "true"
        return self._order_id(await self._request("POST", "/openApi/swap/v2/trade/order", params))

    async def cancel(self, symbol: str, order_id: str) -> None:
        try:
            await self._request("DELETE", "/openApi/swap/v2/trade/order", {"symbol": symbol, "orderId": order_id})
        except BingXError as e:
            log.warning("cancel %s: %s", order_id, e)

    async def aclose(self) -> None:
        await self.http.aclose()
