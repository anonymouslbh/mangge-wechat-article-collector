# 曼格云接口映射

来源：用户提供的公开接口契约与实时产品目录。脚本每次预估和执行前均重新读取目录；这里不保存价格。

## 通用

- 根地址：`https://api.we-media.cn`
- 认证：请求头 `X-API-Key`
- 实时目录：`GET /api/v1/products`，免费；最多每页 100 条。
- 商品地址由目录中的 `slug`、`publicMethod`、`publicPath` 组成：`/openapi/{slug}{publicPath}`。脚本只接受 HTTPS、预期 slug、POST 方法和以 `/` 开头且不含 `..` 的公开路径。
- 计费金额优先读取响应 `X-Charge-Micros`，并与 `consumption` 一并记录。价格单位换算：1 元 = 1,000,000 微元。
- 非 GET 自动重试时必须复用同一个 `Idempotency-Key`。脚本为每个付费操作创建一个随机键，网络瞬断时最多重试一次。

## 按名称搜索公众号

- slug：`wechat-native-search-accounts`
- 请求：`query` 必填；`sort` 可选，使用 `latest`；`limit` 1–50，默认 10；第一页不传 `cursor`。
- 响应：`data.items`；候选字段只使用 `accountName`、`description`、`alias`、`username`、`verification`、`latestUpdate`。
- 历史接口可接受的标识来自候选 `username` 或 `alias`，必须满足：`gh_`、数字 `wxid_` 或 6–32 位展示 ID。无法得到合规标识时不得猜测。

## 公众号历史文章

- slug：`wechat-native-account-articles`
- 只用页码模式。首次请求：`{"ghid":"…","page":1}`。
- 续采：同一 `ghid`，原样携带第一页返回的 `collectionId`，并使用响应 `nextPage`。
- 每页固定 20 篇展开后的文章，真实末页可能不足；仅以 `hasMore=false` 判断结束。
- 不冷跳页、不回查任意旧页、不混用 `cursor`、`offset`、`limit`。
- 保存 `data.items` 中的稳定链接、标题、摘要、作者和发布时间；长期归档优先 `canonicalUrl`，其次 `url`。无公开链接的条目保留元数据，但不能购买正文。
- 断点长期有效；预算或批次页数用尽时保留 `collectionId` 与 `nextPage`，下次顺序续采。

## 文章正文

- slug：`wechat-native-article-content`
- 请求：`url` 必填，`format` 使用 `text`。
- `data.content` 为纯文本；`data.article` 可补充标题、账号、作者和发布时间。
- 只对缺失正文的稳定文章链接调用。默认不请求 `analysis`，因此不触发其 87.5% 加价。

## 费用

每次计划都按 slug 读取实时 `priceMicros`、`billingUnit` 和相关加价字段。数量默认为 1；本技能三个请求均逐次计费。若目录出现当前请求适用的加价，多个加价只取最高值：

```text
预计微元 = floor(priceMicros × 数量 × (10000 + 最高加价Bps) / 10000)
```

目录价格只用于上限控制，最终实际费用以调用响应为准。
