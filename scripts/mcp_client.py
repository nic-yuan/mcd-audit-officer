#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
mcd-audit-officer · MCP 客户端（麦当劳 MCP Server）

只依赖 Python 标准库，无第三方包。
传输：Streamable HTTP（实测服务端协商版本 2025-06-18）
鉴权：Authorization: Bearer <MCD_MCP_TOKEN>

设计原则：
- 任何网络/鉴权失败都降级为 None，由稽核引擎走"离线基准"，不阻断审计流程。
- 审计工具必须可离线复现，这是内控可复核性的要求。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

MCP_URL = "https://mcp.mcd.cn"
# 实测：服务端实际返回 protocolVersion = 2025-06-18（会与客户端取交集）。
PROTOCOL_VERSION = "2025-06-18"


class McpError(RuntimeError):
    """MCP 调用失败。"""


class McdMcpClient:
    """麦当劳 MCP 的极简 JSON-RPC 客户端。"""

    def __init__(self, token: str | None = None, url: str = MCP_URL, timeout: int = 20):
        self.token = token or os.environ.get("MCD_MCP_TOKEN", "")
        self.url = url
        self.timeout = timeout
        self._session_id: str | None = None
        self._request_id = 0

    # ---------- 底层 RPC ----------

    def _rpc(self, method: str, params: dict | None = None, notify: bool = False) -> dict | None:
        self._request_id += 1
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        if notify:
            payload.pop("id")

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raise McpError(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}")
        except (urllib.error.URLError, TimeoutError) as e:
            raise McpError(f"网络不可达: {e}")

        if notify or not raw.strip():
            return None
        return self._parse_response(raw)

    @staticmethod
    def _parse_response(raw: str) -> dict:
        """兼容 JSON 与 SSE 两种响应体。"""
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        raise McpError(f"无法解析响应: {raw[:200]}")

    @staticmethod
    def _extract_json(text: str) -> Any | None:
        """
        官方工具返回的 text 是「Markdown 字段说明 + 原始 JSON」的混合体，
        直接 json.loads 会失败。此处扫描所有 '{' 起点，取跨度最大的合法 JSON。

        这是踩过的坑：按纯 JSON 解析会导致 35 个工具全部「调用成功但取不到值」，
        比报错更危险 —— 稽核会静默降级成「无法取证」。
        """
        dec = json.JSONDecoder()
        best: tuple[int, Any] | None = None
        i = text.find("{")
        while i != -1:
            try:
                obj, end = dec.raw_decode(text[i:])
            except json.JSONDecodeError:
                i = text.find("{", i + 1)
                continue
            if best is None or end > best[0]:
                best = (end, obj)
            i = text.find("{", i + 1)
        return best[1] if best else None


    def initialize(self) -> dict | None:
        return self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcd-audit-officer", "version": "1.0.0"},
            },
        )

    # ---------- 工具层 ----------

    def list_tools(self) -> list[dict]:
        """拉取 MCP 工具清单。失败返回空列表（离线模式）。"""
        try:
            resp = self._rpc("tools/list")
        except McpError:
            return []
        return (resp or {}).get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        """
        调用指定工具，返回结构化 content。
        失败时抛 McpError，由调用方决定是否降级。
        """
        resp = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        result = (resp or {}).get("result", {})
        if (resp or {}).get("error"):
            raise McpError(str(resp["error"]))
        return self._unwrap(result)

    @classmethod
    def _unwrap(cls, result: dict) -> Any:
        """把 MCP content 数组还原成 Python 对象（兼容混合文本）。"""
        content = result.get("content", [])
        texts: list[str] = []
        for item in content:
            if item.get("type") == "text":
                texts.append(item.get("text", ""))
        joined = "\n".join(texts).strip()
        if not joined:
            return result
        got = cls._extract_json(joined)
        return got if got is not None else joined

    # ---------- 审计业务封装 ----------
    # 参数名以 2026-10-20 对 mcp.mcd.cn 实测的 inputSchema 为准（非推测）。
    # 单位约定：calculate-price 返回「分」，订单接口返回「元」—— 详见 cents_to_yuan 说明。

    def fetch_order(self, order_id: str) -> dict | None:
        """query-order：订单事实。必填 orderId（订单编码，不是自定义单号）。"""
        try:
            return self.call_tool("query-order", {"orderId": order_id})
        except McpError:
            return None

    def list_orders(self) -> Any:
        """order-list：历史订单列表。该工具不接受任何参数。"""
        try:
            return self.call_tool("order-list", {})
        except McpError:
            return None

    def query_meals(self, store_code: str, order_type: int = 1, be_type: int = 1) -> Any:
        """
        query-meals：门店菜单。
        实测返回 data.meals 是 {productCode: {name, currentPrice, originalPrice}} 映射，
        不是数组 —— 取数时勿按 list 迭代。
        """
        try:
            return self.call_tool(
                "query-meals",
                {"storeCode": store_code, "orderType": order_type, "beType": be_type},
            )
        except McpError:
            return None

    def benchmark_price(
        self,
        store_code: str,
        items: list[dict],
        order_type: int = 1,
        be_type: int = 1,
        be_code: str | None = None,
        gm_service_code: str | None = None,
        reservation_date: str | None = None,
    ) -> dict | None:
        """
        calculate-price：官方核价，作为价差异常的「应然基准」。

        items 结构： [{"productCode": "9900005456", "quantity": 1}]
        beType 取值：1 到店 / 2 麦乐送 / 5 得来速 / 6 企业团餐
        返回字段： price / originalPrice / productPrice（**单位：分**）
        """
        args: dict[str, Any] = {
            "storeCode": store_code,
            "orderType": order_type,
            "beType": be_type,
            "items": items,
        }
        if be_code:
            args["beCode"] = be_code
        if gm_service_code:
            args["gmServiceCode"] = gm_service_code
        if reservation_date:
            args["reservationDate"] = reservation_date
        try:
            return self.call_tool("calculate-price", args)
        except McpError:
            return None

    def meal_detail(
        self, store_code: str, code: str, order_type: int = 1, be_type: int = 1
    ) -> dict | None:
        """query-meal-detail：套餐构成，用于串项核查。参数是 code（餐品编码）不是 mealCode。"""
        try:
            return self.call_tool(
                "query-meal-detail",
                {"storeCode": store_code, "code": code, "orderType": order_type, "beType": be_type},
            )
        except McpError:
            return None

    def my_coupons(self, page: int = 1, page_size: int = 200) -> Any:
        """query-my-coupons：卡包券资产。注意它不校验门店/渠道可用性。"""
        try:
            return self.call_tool("query-my-coupons", {"page": str(page), "pageSize": str(page_size)})
        except McpError:
            return None

    def store_coupons(self, store_code: str, order_type: int = 1, be_type: int = 1) -> Any:
        """query-store-coupons：当前门店/渠道可用的券（比 my_coupons 更贴近下单规则）。"""
        try:
            return self.call_tool(
                "query-store-coupons",
                {"storeCode": store_code, "orderType": order_type, "beType": be_type},
            )
        except McpError:
            return None

    def meal_assistance(self, store_code: str, order_type: int = 2) -> Any:
        """query-meal-assistance：企业团餐助餐服务。beType 固定 6，仅团餐场景。"""
        try:
            return self.call_tool(
                "query-meal-assistance", {"storeCode": store_code, "beType": 6, "orderType": order_type}
            )
        except McpError:
            return None

    def nutrition(self, **kwargs) -> Any:
        """list-nutrition-foods：营养数据，作为附加稽核维度。"""
        try:
            return self.call_tool("list-nutrition-foods", kwargs)
        except McpError:
            return None



# ---------- 单位换算 ----------
# 实测差异（2026-10-20）：
#   calculate-price  -> price / originalPrice / productPrice  单位「分」（3350 即 33.5 元）
#   query-order      -> realTotalAmount / totalAmount          单位「元」（"112.4"）
# 审核必须先统一计量单位。漏了这一步，价差会凭空放大 100 倍，
# 整个底稿会变成一堆假缺陷 —— 对审计来说，假缺陷比漏报更伤可信度。

def to_yuan(value: Any, unit: str = "yuan") -> float | None:
    """把金额统一成「元」。unit 取 'yuan' 或 'cent'。"""
    if value is None:
        return None
    try:
        v = float(str(value).replace(",", "").replace("¥", "").strip())
    except (TypeError, ValueError):
        return None
    return round(v / 100, 2) if unit == "cent" else round(v, 2)


def probe(token: str | None = None) -> dict:
    """
    连通性自检。用于 README 的"跑通第一次调用"，也用于确认 Token 有效。
    返回 {ok, tool_count, tools, server, error}
    """
    client = McdMcpClient(token=token)
    try:
        init = client.initialize() or {}
        tools = client.list_tools()
        server = (init.get("result") or {}).get("serverInfo") or {}
        return {
            "ok": len(tools) > 0,
            "tool_count": len(tools),
            "server": server.get("name") or "",
            "protocol": (init.get("result") or {}).get("protocolVersion") or "",
            "tools": [t.get("name") for t in tools][:40],
            "error": None,
        }
    except McpError as e:
        return {"ok": False, "tool_count": 0, "tools": [], "server": "", "protocol": "", "error": str(e)}



if __name__ == "__main__":
    print(json.dumps(probe(), ensure_ascii=False, indent=2))
