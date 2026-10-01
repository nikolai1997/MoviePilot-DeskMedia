# 桌面摆件影视插件

DeskMedia 是面向局域网桌面屏、ESP32-S3 摆件和其他轻量客户端的 MoviePilot V2
原生插件。它直接复用 MoviePilot 的推荐、搜索和订阅能力，由 NAS 统一提供影视数据、
订阅状态与适配小屏显示的海报，不需要 Mac 中转，也不需要部署额外 Docker 服务。

插件把能力收敛成一组小型 HTTP 接口，并使用独立设备密钥进行请求签名。客户端可以
分别浏览热门电影、热门电视剧和已订阅内容，也可以接入语音助手完成“搜索候选 -> 用户
确认 -> 新增订阅”的安全流程。

## 功能

- 从 MoviePilot 获取热门电影、热门电视剧和已订阅内容。
- 使用 MoviePilot 媒体搜索能力，为小智 AI 提供最多 3 个候选结果。
- 通过 MoviePilot 新增订阅；语音链路要求先搜索、再由用户明确确认。
- 海报按需转换为摆件直接显示的 `150x225` RGB565LE 数据。
- 使用独立设备密钥进行 HMAC-SHA256 请求签名，不向网络发送设备密钥，也不暴露 MoviePilot 管理令牌。

## 安装

将 `plugins.v2/deskmedia` 复制到 MoviePilot V2 的本地插件目录，并将
`package.v2.json` 放在本地插件仓库根目录。重启 MoviePilot 后，在插件页启用
“桌面摆件影视”。首次启用会自动生成至少 24 位的设备令牌。

## 客户端兼容

客户端需要实现本页约定的 DeskMedia 0.3.0 接口和 HMAC-SHA256 签名协议。仓库只发布
MoviePilot 插件，不包含特定设备的固件；任何能够发起 HTTP 请求、解析 JSON，并显示
`150x225` RGB565LE 图像的局域网设备都可以自行接入。

用于语音订阅时，客户端必须先调用搜索接口展示或播报候选项，只能把最近一次搜索返回的
候选 ID 交给订阅接口，并在调用订阅前取得用户明确确认。

基础路径：

```text
https://<moviepilot-host>/api/v1/plugin/DeskMedia
```

每个请求都必须生成 16 字节随机 nonce，并带：

```text
X-Desk-Nonce: <32 位小写十六进制随机数>
X-Desk-Signature: <64 位小写十六进制 HMAC-SHA256>
```

签名原文为 `METHOD + "\n" + PATH_WITH_QUERY + "\n" + SHA256(BODY) + "\n" + NONCE`，
密钥是插件设置页中的设备令牌。插件会缓存最近的 nonce 并拒绝重放。

## 接口

- `GET /health`
- `GET /feed?view=movies&limit=12`
- `GET /feed?view=tv&limit=12`
- `GET /feed?view=subscribed&limit=12`
- `POST /search` with `{"query":"片名","type":"movie|tv|all","limit":3}`
- `GET /poster/{poster_id}`
- `POST /subscribe` with `{"id":"<feed-or-search item id>"}`

插件故意不提供删除、暂停、下载或传入任意订阅元数据的接口。订阅 ID 只能来自最近
30 分钟内由本插件返回的列表或搜索结果。

## 安全说明

- 推荐通过 HTTPS 反向代理访问 MoviePilot。
- HMAC 可防止设备密钥泄露、请求伪造与重放，但不会加密影视标题等响应内容；HTTP 只能用于可信且隔离的局域网，公网必须使用 HTTPS。
- 海报只允许来自内置的公开图片域名，禁止跳转，并限制下载体积、像素数与并发处理数。
- JSON 响应为私有且不可缓存；设备令牌不会出现在响应或日志中。
- 发现安全问题请参阅 [SECURITY.md](SECURITY.md)，不要在公开 Issue 中粘贴令牌或网络配置。

## 配对摆件

摆件连入同一可信局域网后，向它的本地配置接口写入 MoviePilot 地址和设备令牌：

```bash
curl -X POST http://<desk-ip>:47872/v1/moviepilot/config \
  -H 'Content-Type: application/json' \
  -d '{"host":"<moviepilot-host>","port":3000,"device_token":"<device-token>"}'
```

配置保存在摆件 NVS 中，接口只返回是否保存成功，不回显令牌。

## 验证

```bash
export DESKMEDIA_BASE_URL='https://<moviepilot-host>/api/v1/plugin/DeskMedia'
export DESKMEDIA_DEVICE_TOKEN='<device-token>'
python3 tests/verify_live.py
```

## 许可

GPL-3.0-or-later，详见 [LICENSE](LICENSE)。
