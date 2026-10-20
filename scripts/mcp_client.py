#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
mcd-audit-officer · MCP 客户端（麦当劳 MCP Server）

只依赖 Python 标准库，无第三方包。
传输：Streamable HTTP（MCP spec 2025-11-25+）
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
PROTOCOL_VERSION = "2025-11-25"


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

    @staticmethod
    def _unwrap(result: dict) -> Any:
        """把 MCP content 数组还原成 Python 对象。"""
        content = result.get("content", [])
        texts: list[str] = []
        for item in content:
            if item.get("type") == "text":
                texts.append(item.get("text", ""))
        joined = "\n".join(texts).strip()
        if not joined:
            return result
        try:
            return json.loads(joined)
        except json.JSONDecodeError:
            return joined

    # ---------- 审计业务封装 ----------
    # 每个方法都对应一条稽核规则的"应然基准"取数路径。

    def fetch_order(self, order_no: str) -> dict | None:
        """query-order：订单事实（金额、品项、时间、门店）。"""
        try:
            return self.call_tool("query-order", {"orderNo": order_no})
        except McpError:
            return None

    def list_orders(self, **kwargs) -> Any:
        """order-list：历史订单（到店/外送）。"""
        try:
            return self.call_tool("order-list", kwargs)
        except McpError:
            return None

    def benchmark_price(self, store_id: str, items: list[dict], coupon_ids: list[str] | None = None) -> dict | None:
        """calculate-price：官方核价，作为价差异常的'应然基准'。"""
        try:
            return self.call_tool(
                "calculate-price",
                {"storeId": store_id, "items": items, "couponIds": coupon_ids or []},
            )
        except McpError:
            return None

    def meal_detail(self, meal_code: str) -> dict | None:
        """query-meal-detail：套餐构成，用于串项核查。"""
        try:
            return self.call_tool("query-meal-detail", {"mealCode": meal_code})
        except McpError:
            return None

    def my_coupons(self) -> Any:
        """query-my-coupons：券持有情况，用于券未核销核查。"""
        try:
            return self.call_tool("query-my-coupons", {})
        except McpError:
            return None

    def store_coupons(self, store_id: str) -> Any:
        """query-store-coupons：门店可用券。"""
        try:
            return self.call_tool("query-store-coupons", {"storeId": store_id})
        except McpError:
            return None

    def meal_assistance(self, store_id: str) -> Any:
        """query-meal-assistance：企业团餐助餐服务（B 端场景）。"""
        try:
            return self.call_tool("query-meal-assistance", {"storeId": store_id})
        except McpError:
            return None

    def nutrition(self, **kwargs) -> Any:
        """list-nutrition-foods：营养数据，作为附加稽核维度。"""
        try:
            return self.call_tool("list-nutrition-foods", kwargs)
        except McpError:
            return None


def probe(token: str | None = None) -> dict:
    """
    连通性自检。用于 README 的"跑通第一次调用"，也用于确认 Token 有效。
    返回 {ok, tools, error}
    """
    client = McdMcpClient(token=token)
    try:
        client.initialize()
        tools = client.list_tools()
        return {
            "ok": len(tools) > 0,
            "tool_count": len(tools),
            "tools": [t.get("name") for t in tools][:40],
            "error": None,
        }
    except McpError as e:
        return {"ok": False, "tool_count": 0, "tools": [], "error": str(e)}


if __name__ == "__main__":
    print(json.dumps(probe(), ensure_ascii=False, indent=2))
