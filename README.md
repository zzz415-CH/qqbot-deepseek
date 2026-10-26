# 从「论坛里的 DeepSeek」到「自己的 QQ 机器人」

## 0. 一句话原理

论坛上那些能和你聊天的 DeepSeek，**没有一个是真的「DeepSeek 住在论坛里」**。它们都是同一套东西：

```
用户消息 ──→ 接入层 ──→ 会话层 ──→ 模型层(DeepSeek API) ──→ 回包
```

模型本身**没有记忆、没有状态、不认识你**。你看到的「它记得我上句说了什么」，是因为程序每次都把聊天记录重新拼进 prompt 再发一遍。

网页套壳站和 QQ 机器人，差的只是第一段「接入层」。后面 90% 的代码是同一份。

---

## 1. 三段各自在干什么

| 层 | 网页版套壳站 | QQ 机器人 |
|---|---|---|
| 接入层 | 浏览器页面 + 后端转发接口 | QQ 协议（官方 API 或 NapCat） |
| 会话层 | 登录态 / 访客 cookie → 对话 ID → 历史表 | `group_openid` / `user_openid` → 历史表 |
| 模型层 | `POST /chat/completions` | 同左，一模一样 |

所以「做一个 QQ 机器人」= 把接入层从 HTTP 换成 QQ 协议，再加两件小事：**去重**和**限流**（QQ 会重推事件，QQ 有频控）。

---

## 2. QQ 机器人：三条路，先选路

这是最关键的一步，选错了后面全是白工。

| | A. 官方开放平台 | B. 第三方协议端 | C. 成品框架 |
|---|---|---|---|
| 怎么接 | 建应用拿 AppID/Secret，连 WS | NapCat / Lagrange 挂小号，转 OneBot 11 | AstrBot / dsh-qqbot 插件 |
| 跑在 | 机器人账号（不是真人号） | **你自己的 QQ 号** | 同 A 或 B |
| 能收什么 | 默认只有被 @ 的消息 | 群里全部消息 | 看底层 |
| 主动推送 | 受严格频控 + 每日上限 | 基本无限制 | 同底层 |
| 封号风险 | 合规，无 | **违反用户协议，有** | 同底层 |
| 适合 | 想长期稳定跑、能接受「@ 才回」 | 想要「像真人一样在群里潜水接话」 | 不想写代码 |

### A. 官方开放平台（推荐先走这条）

1. 打开 <https://q.qq.com/qqbot/openclaw/login.html>，QQ 登录，点「创建机器人」——**三步就能拿到 AppID / AppSecret**，不用等审核。
2. 连的方式二选一：
   - **WebSocket**：本地就能跑，不用公网 IP ← 本目录代码走这条
   - **Webhook**：需要公网 HTTPS 回调地址
3. 加群：现在是群主直接把你这个机器人拉进群即可（对方需新版手机 QQ）。

**必须知道的三个限制**（官方文档原文）：

- 群聊里机器人**默认只能收到 @ 它的消息**（全量消息需要群主给权限 + 申请）
- **被动回复**：带 `msg_id` 回复，群聊窗口 **5 分钟**、每条消息最多回 **5 次**
- **主动消息**：群聊 60 条/分钟、单群每天 1000 条，且用户可关闭接收

这三条直接决定了你的产品形态：它是「@ 就答」的问答机器人，不是「主动找你唠嗑」的陪聊。

### B. 第三方协议端（NapCat / Lagrange）

原理：用一个**真实的 QQ 号**登录，客户端协议被逆向实现，程序劫持登录态，把 QQ 的消息流翻译成通用的 **OneBot 11** 协议，框架（NoneBot2 / Koishi / AstrBot）再接这个协议。

- 能干官方干不了的事：读群里所有消息、发图/表情/语音、撤回、禁言、多号
- 代价：**这是违反 QQ 用户协议的**，腾讯一直在打击，轻则限制登录，重则封号。**绝对别拿主号跑**
- 生态：NapCat（基于 NTQQ 客户端）目前是主流，go-cqhttp 早已停止维护

### C. 成品框架（零代码）

- **AstrBot**：Python 写的多平台 LLM 机器人框架，自带 Web 管理面板，内置 DeepSeek / OpenAI / Ollama 等 provider。装好后在面板里选「QQ 个人号 (aiocqhttp)」填 `ws://127.0.0.1:3001`，模型里填 DeepSeek API Key，就能聊。
- **dsh-qqbot 官方插件**：你**现在这个环境**就是 DeepSeek Harness，腾讯官方有个插件直接把这个 agent 接进 QQ，两条命令（见下）。

---

## 3. 最快跑通：三条命令级方案

### 方案一：你已经装了 DSH（最省事）

```bash
npx @deepseek-ai/dsh plugin --profile qqbot add @tencent-connect/dsh-qqbot
npx @deepseek-ai/dsh --profile qqbot
```

首次启动会打印二维码，手机 QQ 扫码绑定即可，**不用去开放平台手动建应用**。
注意它用的是独立的 `qqbot` profile，不会动你当前的会话配置。

想让机器人在群里不用 @ 也能接话，在 `cordis.patch.yaml` 里加：

```yaml
- id: im-qqbot
  name: '@tencent-connect/dsh-qqbot'
  config:
    requireMention: false
```

### 方案二：自己写（本目录代码）

```bash
pip install -r requirements.txt

# Windows PowerShell
$env:QQ_APP_ID="你的AppID"
$env:QQ_APP_SECRET="你的AppSecret"
$env:DS_API_KEY="sk-..."

python qqbot_deepseek.py
```

日志里出现 `就绪: xxx (bot_id)` 就是连上了。去 QQ 群里 @ 它试试。

### 方案三：AstrBot + NapCat（要「像真人」的玩法）

```bash
docker run -d --name napcat -p 3000:3000 -p 6099:6099 \
  -v ./napcat:/app/napcat/config mlikiowa/napcat-docker:latest
```

扫码登录小号 → 在 NapCat 面板开 OneBot 11 的 WS 服务端（3001）→ AstrBot 里加 QQ 平台指向它 → 模型选 DeepSeek 填 key。

---

## 4. 自己写：真正要搞懂的 5 件事

代码见 [`qqbot_deepseek.py`](./qqbot_deepseek.py)，约 300 行，每一段都对应下面一条。

### ① 鉴权：token 两小时就过期

```
POST https://api.bot.qq.com/app/getAppAccessToken
{"appId": "...", "clientSecret": "..."}   →  {"access_token": "...", "expires_in": 7200}
```

**坑**：这个接口失败时 HTTP 状态码仍是 200，只能看响应体里的 `code` 判断成败。

之后所有请求带 `Authorization: QQBot {access_token}`。

### ② 长连接：握手是有固定时序的

```
连上 ws  →  收到 op10 Hello(内含 heartbeat_interval≈45s)
         →  发 op2 Identify {token, intents, shard:[0,1]}
         →  收到 op0 READY(内含 session_id)
         →  每 45s 发 op1 心跳，d = 最近一次事件的 s
         →  掉线后发 op6 Resume {session_id, seq} 补发漏掉的事件
```

`intents` 是个位掩码，`1<<25`（`GROUP_AND_C2C_EVENT`）就是群聊 @ 消息 + 单聊消息。接频道再加别的位。

### ③ 被动回复：这是最容易翻车的地方

发消息必须带 `msg_id`（触发它的那条消息的 id），并**递增** `msg_seq`：

```json
POST /v2/groups/{group_openid}/messages
{"content": "…", "msg_type": 0, "msg_id": "事件里的 id", "msg_seq": 1}
```

- 同一条 `msg_id` + 同一个 `msg_seq` 重发会失败 → 长回答要切成多段、`msg_seq` 递增
- 群聊窗口 **5 分钟**、最多 **5 条** → 所以要 `MAX_CHARS` 切块 + `REPLY_BUDGET` 截断
- 模型如果走「思考模式」思考 6 分钟，回复直接过期。**聊天机器人用 `deepseek-flash` 的非思考模式**，别用 pro 的深度思考

### ④ 上下文：自己记账，别指望模型

```python
key = f"g:{group_openid}"      # 群里一个会话
key = f"u:{user_openid}"       # 私聊一个会话
```

历史只留最近 N 轮（代码里是滑窗），再久就摘要或丢掉。**上下文越长越贵**：DeepSeek 的缓存命中价是未命中的 1/25，但前提是前缀稳定——所以 system prompt 放最前面且别乱改。

### ⑤ 并发与去重

- 同一条 `msg_id` 可能被推多次 → 用有序字典按 id 去重，10 分钟过期
- 每条消息开一个 `asyncio.create_task`，但用信号量卡住并发（代码里是 4），不然群里一刷屏就是一堆 429
- 发送之间 `sleep(0.3)`，别把频控打爆

---

## 5. 上线前会踩的坑

| 现象 | 原因 | 处理 |
|---|---|---|
| 机器人没反应 | 群聊只收 @ 消息 | 检查有没有 @，或去申请全量消息权限 |
| 「回复超时」/ 发不出去 | 超过 5 分钟被动窗口 | 缩短模型耗时，加超时兜底 |
| 只有第一条回得去 | 复用了同一个 `msg_seq` | 每次 `msg_seq` 递增 |
| 群里疯狂刷屏 | 没做「只回 @」判断 | `requireMention` 模式 |
| 答非所问，上下文串了 | 所有群共用一个会话 key | 按群/人分 key |
| 回复「内容安全」被拦 | QQ 侧审核 | 加敏感词过滤，或用更保守的人设 |
| 账单暴涨 | 历史无限增长 + 用 pro | 滑窗 + `deepseek-flash` + 看缓存命中率 |

**关于成本**（2026-10 官方价，`deepseek-flash`）：输入未命中 1~2 元/百万 token，命中缓存 0.02~0.04 元，输出 4~8 元。峰值时段是工作日 9-12、14-18 点，**其余时间半价**。一个日常聊天的群机器人，一个月通常就几块钱。

---

## 6. 本目录文件

| 文件 | 说明 |
|---|---|
| [`qqbot_deepseek.py`](./qqbot_deepseek.py) | 官方 QQ 机器人 + DeepSeek，单文件最小实现 |
| [`requirements.txt`](./requirements.txt) | `websockets` + `httpx` |

代码里留了这些人设/行为开关（环境变量）：

| 变量 | 默认 | 作用 |
|---|---|---|
| `BOT_PERSONA` | 群聊朋友口吻 | system prompt |
| `DS_MODEL` | `deepseek-flash` | 想更强可换 `deepseek-v4-pro`（贵、慢） |
| `MAX_TURNS` | 6 | 每个会话保留几轮 |
| `MAX_CHARS` | 1500 | 单条超长就切块 |
| `DS_TIMEOUT` | 40 | 模型超时秒数，别超过 5 分钟窗口 |

---

## 7. 参考

- [DeepSeek API 模型与价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)
- [QQ 机器人 · 消息收发概述](https://bot.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)
- [QQ 机器人 · 获取访问凭证](https://bot.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html)
- [QQ 机器人 · WebSocket 方式](https://bot.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/websocket.html)
- [QQ 机器人 · Agent 接入（含 dsh-qqbot 插件）](https://bot.qq.com/wiki/agent-qqbot/)
- [AstrBot 多平台 LLM 机器人框架](https://github.com/AstrBotDevs/AstrBot)
