# MCP 集成说明

> 本文件说明项目实际使用的麦当劳 MCP Server、Tool、调用流程与业务价值。
> **所有参数名、字段名、返回结构、单位均来自 2026-10-20 对 `mcp.mcd.cn` 的真实调用实测，非推测。**

## 一、接入方式

| 项 | 值 |
|---|---|
| MCP Server | 麦当劳中国官方 MCP（`M-China/mcd-mcp-server`） |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP |
| 鉴权 | `Authorization: Bearer ${MCD_MCP_TOKEN}` |
| 协议版本 | `2025-06-18`（实测：client 发 2025-11-25，服务端协商回落到 2025-06-18） |
| Server 标识 | `mcd-mcp` |
| 可见工具数 | 35 |
| 限流 | 600 次/分钟/Token（超限返回 429） |

配置文件见 `mcp-config.example.json`（脱敏，仅环境变量占位符）。
客户端实现见 `scripts/mcp_client.py`——纯标准库 `urllib`，无第三方依赖。

连通性自检：

```bash
export MCD_MCP_TOKEN=你的Token
python scripts/run_audit.py --probe
# {"ok": true, "tool_count": 35, "server": "mcd-mcp", "protocol": "2025-06-18", ...}
```

## 二、实际调用的 Tool（真实参数契约）

以下 required 标记 `*`。参数名照抄官方 `inputSchema`，**没有一处按语义猜**。

### 2.1 `query-order` — 订单事实（单笔）

| 参数 | 类型 | 说明 |
|---|---|---|
| `orderId` * | string | 订单编码（不是自定义单号，不长这样的一律查不到） |

返回关键字段：`orderId` / `storeName` / `storeAddress` / `createTime` /
`realTotalAmount`（**元**）/ `totalAmount` / `totalDiscountAmount` /
`orderProductList[{productCode, productName, quantity, comboItemList}]` / `couponList` / `takeWay`

> ⚠️ **不返回 `storeCode`**。只给门店名和地址，见第五节陷阱 ②。

### 2.2 `order-list` — 历史订单

**不接受任何参数**（传了也会被忽略）。返回 `data.list[]`，每条含
`orderId` / `storeCode` / `storeName` / `orderType` / `beType` / `beCode` / `createTime` / `realTotalAmount`。

> 这是本项目获取 `storeCode` 的唯一来源 —— 用于给 `query-order` 补齐门店编码。

### 2.3 `calculate-price` — 官方核价（核心）

| 参数 | 类型 | 说明 |
|---|---|---|
| `storeCode` * | string | 门店编码，**必填不可空** |
| `orderType` * | integer | 1=到店（含得来速）／2=外送（含麦乐送、企业团餐） |
| `beType` * | integer | 1=到店取餐／2=麦乐送／5=得来速 DT／**6=企业团餐** |
| `items` | array | `[{"productCode": "9900005456", "quantity": 1}]` |
| `beCode` | string | 与 storeCode 配对，从门店查询结果取 |
| `gmServiceCode` | string | 团餐-助餐服务码 |
| `reservationDate` | string | 预约场景才传，`yyyy-MM-dd HH:mm` |

真实返回（单点"麦辣鸡腿汉堡三件套"×1）：

```json
{
  "code": 200,
  "data": {
    "productOriginalPrice": 3350,
    "productPrice": 3350,
    "originalPrice": 3350,
    "discount": 0,
    "price": 3350,
    "productList": [
      {"productCode": "9900005456", "productName": "麦辣鸡腿汉堡三件套",
       "quantity": 1, "originalSubtotal": 3350, "subtotal": 3350}
    ],
    "takeWayList": [{"code": "eat-in", "title": "堂食"}, {"code": "take-in-store", "title": "外带"}]
  }
}
```

> ⚠️ **金额单位是「分」**：`3350` = 33.5 元。而订单接口的 `realTotalAmount` 是「元」。
> 见第五节陷阱 ①。

### 2.4 `query-meals` — 门店菜单（品项名 → 编码）

| 参数 | 类型 | 说明 |
|---|---|---|
| `storeCode` * | string | 门店编码 |
| `orderType` * | integer | 1 / 2 |
| `beType` * | integer | 1 / 2 / 5 / 6 |

返回 `data.meals` 是 **映射对象，不是数组**：

```json
{"data": {"categories": [...], "meals": {
  "9900005456": {"name": "麦辣鸡腿汉堡三件套", "currentPrice": "33.5",
                 "originalPrice": "47", "canWithOrder": false, "image": "..."}
}}}
```

> ⚠️ key 才是 `productCode`，value 里没有 code 字段。按 list 迭代会直接踩空。
> 见第五节陷阱 ③。

### 2.5 `query-meal-detail` — 套餐构成（R04 串项用）

| 参数 | 类型 | 说明 |
|---|---|---|
| `storeCode` * | string | 门店编码 |
| `code` * | string | 餐品编码（**不是 `mealCode`**） |
| `orderType` * | integer | 1=到店时 beCode 不要传，传了报错 |
| `beType` * | integer | 1 / 2 / 5 / 6 |

### 2.6 其它

| Tool | 参数 | 用途 |
|---|---|---|
| `query-my-coupons` | `page`、`pageSize`（均为 string，默认 1/200） | 卡包券资产。**不校验门店/渠道可用性**，不能等同于「本单可用券」 |
| `query-store-coupons` | `storeCode`、`orderType`、`beType` | 当前门店/渠道可用券，更贴近下单规则 |
| `query-meal-assistance` | `storeCode`、`beType` 固定 6、`orderType` | 企业团餐助餐服务，**仅团餐场景** |
| `list-nutrition-foods` | 视品类传参 | 营养数据，可扩展为"工作餐营养合规"附加维度 |

## 三、控制点与 Tool 映射

| Control | MCP Tool | 在稽核中的作用 | 业务价值 |
|---|---|---|---|
| **R01 价差异常** | `calculate-price` | 按同品项重算应付总额，作为"应然价格基准" | 核心。没有官方核价，价差只能靠票面自证；有了它，每笔申报都有独立于报销人的权威比价基准 |
| **R02 券未核销** | `query-my-coupons` / `query-store-coupons` | 取可用券与订单已核销券比对 | 把"该省没省"量化成可考核项 |
| **R03 超预算** | `query-order` / `order-list` | 取实付金额与渠道 | 支撑单笔/单人/部门三级阈值 |
| **R04 串项** | `query-meal-detail` | 取套餐实际构成，与申报品项比对 | 防止"申报了但没点" |
| **R05 重复报销** | `order-list` / `query-order` | 按订单号建唯一性索引 | 一单多报是最典型的费用舞弊手法 |
| **R06 非工时订单** | `query-order` | 取真实下单时间 | 加班餐需附加班审批，时间是硬证据 |
| **R07 超期申报** | `order-list` | 取订单发生日期 | 期间费用截止性测试 |
| 团餐场景 | `query-meal-assistance` | 门店企业团餐助餐服务 | `beType=6` 是官方原生 B 端通道，不是硬凑的场景 |

## 四、调用流程（含真实依赖顺序）

```
申报表(CSV)
    │
    ├─► order-list ──────────────► ① 建 orderId → storeCode 索引
    │                                （因为 query-order 不返回 storeCode）
    │
    ├─► query-order(orderId) ────► ② 订单事实：实付金额 / 品项 / 时间 / 渠道
    │
    ├─► query-meals(storeCode) ──► ③ 菜单索引：品项名 → productCode
    │                                （calculate-price 只认编码，不认名字）
    │
    ├─► calculate-price ─────────► ④ 官方核价（分 → 元 换算后作为基准）
    │
    ├─► query-my-coupons ────────► ⑤ 可用券（券未核销核查）
    │
    └─► query-meal-detail ───────► ⑥ 套餐构成（串项核查，按需）
                                        │
                                        ▼
                          三单交叉：申报单 × 订单 × 官方核价
                                        │
                                        ▼
                          7 条规则逐笔测试 → 缺陷分级 → 底稿
```

①③ 是两次「索引构建」，各只做一次并缓存；②④ 才是每笔申报的取数。这样 N 笔申报的调用量约为
`2 + 2N`，而不是 `4N`。

## 五、三个真实陷阱（实测踩出来的）

这三个不是理论风险，是实际跑出错误结论后才发现的。写在这里，是因为**它们全都不会报错**——
接口返回 200，程序正常往下走，只有结果悄悄错了。

### ① 金额单位不一致：核价是「分」，订单是「元」

`calculate-price` 返回 `price: 3350`（分），`query-order` 返回 `realTotalAmount: "112.4"`（元）。
直接相减会得到 3237.6 元的天价价差，把每一笔正常报销都判成重大舞弊。

处理：`mcp_client.to_yuan(v, unit)` 显式声明单位，**不做自动猜测**——猜错方向的代价比缺数据大。

### ② `query-order` 不返回 `storeCode`，而核价必须要它

订单详情只有 `storeName` 和 `storeAddress`。没有 `storeCode` 就调不了 `calculate-price`，
结果是 **R01 永远不命中，而底稿上看不出任何异常**——最危险的那类静默失败。

处理：先用 `order-list` 建 `orderId → storeCode` 索引补齐，补不到就明确告警并跳过核价。

### ③ 返回体是「Markdown 说明 + 原始 JSON」的混合文本，且 `meals` 是对象

工具返回的 `content[].text` 长这样：

```
# API Response Information
...（字段说明表格）
## Original Response
{"success":true,"code":200,"data":{...}}
```

直接 `json.loads` 必失败；而 `## Original Response` 这个标题**并非每次都出现**（实测有两次返回
直接从 JSON 开始）。按固定标题切分同样不可靠。

处理：扫描文本中所有 `{` 起点，用 `JSONDecoder.raw_decode` 取跨度最大的合法 JSON。
失败才退回原文，绝不抛异常中断审计。

### ④ 品项映射不全时，必须放弃价差判定

`calculate-price` 只认 `productCode`。若某个品项在菜单里找不到（已下架、套餐名变更），
核价基准就是**残缺的**。此时若仍与完整订单金额比较，会凭空造出一条假价差——
实测中真出现过：两个品项只映射上一个，核出 72.50 元，对 112.40 元的订单得出 39.90 元"差异"，
而实际只是少算了那份小食。

处理：只要有品项映射不上，**整笔放弃价差判定**，并在 verbose 日志里标明原因。
残缺基准下的结论，比没有结论更有害。

## 六、降级设计（重要）

内控审计要求结论可复核、可重放，因此本项目**不依赖网络也能完整执行**：

- MCP 取数失败（无 Token / 网络异常 / 429 / 鉴权失败）时，引擎自动降级为内置脱敏样例基准；
- 降级会在底稿"数据来源"字段显式标注 `sample`，**不会伪装成 `mcp`**；
- 未取到订单事实的申报，会在底稿"结论与局限"中单列，**不构成无缺陷结论**；
- 真实模式下**绝不注入演示券**。早期版本在 mcp 模式 `setdefault` 了示例券，会让 R02 拿假券当
  真实证据，已移除。

这是审计工具与玩具脚本的分界线：宁可标注"无法取证"，也不给出虚假的干净结论。

## 七、一次真实运行记录（脱敏）

```bash
export MCD_MCP_TOKEN=***
python scripts/run_audit.py --csv my_claims.csv --source mcp -v
```

```
[mcp] order-list 索引 2 笔
[warn] 1030…4103 有 1 个品项未映射到 productCode（['G.O.A.T.小食拼盘']），
       核价基准不完整 → 放弃价差判定
- 数据来源：**mcp**
- 缺陷合计：1 条（重大 0 / 重要 1 / 一般 0）
- 可量化敞口：32.40 元
```

这条记录说明三件事：真实订单取到了、核价链路走通了、残缺基准被正确拦下（没有误报价差）。
`数据来源：mcp` 与告警同时出现，正好体现"降级/局限可见"的设计。

## 八、业务价值总结

1. **把"报销审核"从经验判断变成程序化测试**。7 个控制点固定，任何人执行结果一致。
2. **引入官方核价作为独立基准**，解决报销审核最大的难题——审核方只能看票面，无法独立验证价格。
3. **缺陷分级 + 证据链**，输出可直接进审计档案，可追溯、可复核。
4. **企业团餐有官方原生通道**（`beType=6` + `gmServiceCode`），场景不是硬凑的。
