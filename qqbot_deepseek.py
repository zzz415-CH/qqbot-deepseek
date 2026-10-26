"""QQ 官方机器人 + DeepSeek 最小可用实现（单文件）。

跑通需要三样东西：
  1. QQ 开放平台建一个机器人，拿到 AppID / AppSecret
  2. DeepSeek 开放平台的 API Key
  3. pip install websockets httpx

配置全走环境变量，见文件末尾 __main__ 里的说明，或同级目录的 README.md。

设计上刻意保持“够用就好”，但把上线会踩的坑都留了位置：
  - access_token 2 小时过期 → 自动续期
  - 网关掉线 → 带 session 重连（Resume），补发漏掉的事件
  - 事件可能重复推送 → 按 msg_id 去重
  - 被动回复 5 分钟 / 群聊最多 5 条 → 长回答切块发送
  - 上下文按“群 / 人”分别保存 → 滑窗截断，不会无限涨
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import OrderedDict

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("qqbot")

# ---------------------------------------------------------------- 配置

APP_ID = os.getenv("QQ_APP_ID", "")
APP_SECRET = os.getenv("QQ_APP_SECRET", "")

QQ_API = os.getenv("QQ_API_BASE", "https://api.bot.qq.com")
DS_API = os.getenv("DS_BASE_URL", "https://api.deepseek.com").rstrip("/")
DS_KEY = os.getenv("DS_API_KEY", "")
DS_MODEL = os.getenv("DS_MODEL", "deepseek-flash")

# 人设。群里聊天和“问答助手”是两种写法，按需改。
SYSTEM_PROMPT = os.getenv(
    "BOT_PERSONA",
    "你是一个在 QQ 群里聊天的朋友。说话口语化、简短，一般不超过 80 字，"
    "不要用 Markdown 标题和列表，不要自称 AI 助手。被问到不懂的就直说。",
)

MAX_TURNS = int(os.getenv("MAX_TURNS", "6"))      # 每个会话保留多少轮对话
MAX_CHARS = int(os.getenv("MAX_CHARS", "1500"))   # 单条消息最大字符数
REPLY_BUDGET = 5                                   # 被动回复最多发几条
DS_TIMEOUT = float(os.getenv("DS_TIMEOUT", "40"))  # 模型调用超时（秒）

# 事件订阅：1<<25 = GROUP_AND_C2C_EVENT，群聊 @ 消息 + 单聊消息。
# 想接频道（Guild）再按文档把对应的位或进来。
INTENTS = 1 << 25

MENTION_RE = re.compile(r"<@!?\d+>")


# ---------------------------------------------------------------- 凭证

class TokenManager:
    """QQ 的 access_token 有效期 7200 秒，过期要自己换新的。"""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._token: str | None = None
        self._expire_at = 0.0

    async def get(self) -> str:
        # 提前 60 秒续期，避免边界上刚好过期
        if self._token and time.time() < self._expire_at - 60:
            return self._token

        resp = await self._client.post(
            f"{QQ_API}/app/getAppAccessToken",
            json={"appId": APP_ID, "clientSecret": APP_SECRET},
        )
        data = resp.json()
        # 注意：这个接口失败时 HTTP 仍然是 200，只能看 code
        if "access_token" not in data:
            raise RuntimeError(f"获取 access_token 失败: {data}")

        self._token = data["access_token"]
        self._expire_at = time.time() + int(data.get("expires_in", 7200))
        log.info("access_token 已刷新，%ss 后过期", data.get("expires_in"))
        return self._token

    async def headers(self) -> dict[str, str]:
        return {"Authorization": f"QQBot {await self.get()}",
                "X-Union-Appid": APP_ID}


# ---------------------------------------------------------------- 会话

class SessionStore:
    """按会话 key 保存历史。模型本身没有记忆，全靠这里把上文拼回去。"""

    def __init__(self, max_turns: int = MAX_TURNS) -> None:
        self._data: dict[str, list[dict]] = {}
        self._touched: dict[str, float] = {}
        self._max_turns = max_turns

    def get(self, key: str) -> list[dict]:
        self._touched[key] = time.time()
        return list(self._data.get(key, []))

    def append(self, key: str, role: str, content: str) -> None:
        hist = self._data.setdefault(key, [])
        hist.append({"role": role, "content": content})
        # 一轮 = 一条 user + 一条 assistant，留够 2*max_turns 条
        if len(hist) > self._max_turns * 2:
            del hist[: len(hist) - self._max_turns * 2]
        self._touched[key] = time.time()

    def gc(self, ttl: float = 3600 * 6) -> None:
        now = time.time()
        for key, ts in list(self._touched.items()):
            if now - ts > ttl:
                self._data.pop(key, None)
                self._touched.pop(key, None)


# ---------------------------------------------------------------- 机器人

class QQBot:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=20)
        self._ds = httpx.AsyncClient(timeout=DS_TIMEOUT)
        self._tokens = TokenManager(self._http)
        self._sessions = SessionStore()
        self._seen: OrderedDict[str, float] = OrderedDict()  # msg_id 去重
        self._sem = asyncio.Semaphore(4)                     # 同时最多处理 4 条

        self.bot_id: str | None = None
        self.session_id: str | None = None
        self.seq: int | None = None

    # ------------------------------------------------------ 网关

    async def run(self) -> None:
        meta = (await self._http.get(
            f"{QQ_API}/gateway", headers=await self._tokens.headers()
        )).json()
        url = meta["url"]
        log.info("网关地址: %s", url)

        while True:
            try:
                await self._connect(url)
            except ConnectionClosed as exc:
                # 4006/4007 说明 session 失效了，下次必须重新 Identify
                if exc.code in (4006, 4007):
                    log.warning("session 失效 (code=%s)，重新登录", exc.code)
                    self.session_id = None
                    self.seq = None
                else:
                    log.warning("连接断开 (code=%s)", exc.code)
            except Exception as exc:  # noqa: BLE001
                log.warning("连接异常: %r", exc)
            await asyncio.sleep(3)

    async def _connect(self, url: str) -> None:
        async with websockets.connect(url, max_size=2 ** 22) as ws:
            hello = json.loads(await ws.recv())
            if hello.get("op") != 10:
                raise RuntimeError(f"期望 Hello(10)，收到 {hello}")
            interval = hello["d"]["heartbeat_interval"] / 1000
            log.info("Hello，心跳周期 %.0fs", interval)

            if self.session_id is not None and self.seq is not None:
                payload = {"op": 6, "d": {
                    "token": f"QQBot {await self._tokens.get()}",
                    "session_id": self.session_id,
                    "seq": self.seq,
                }}
                log.info("尝试 Resume (seq=%s)", self.seq)
            else:
                payload = {"op": 2, "d": {
                    "token": f"QQBot {await self._tokens.get()}",
                    "intents": INTENTS,
                    "shard": [0, 1],
                    "properties": {"$os": "linux", "$browser": "dsh", "$device": "dsh"},
                }}
            await ws.send(json.dumps(payload))

            hb = asyncio.create_task(self._heartbeat(ws, interval))
            try:
                async for raw in ws:
                    await self._dispatch(ws, json.loads(raw))
            finally:
                hb.cancel()

    async def _heartbeat(self, ws, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await ws.send(json.dumps({"op": 1, "d": self.seq}))
            except Exception:  # noqa: BLE001
                return

    async def _dispatch(self, ws, payload: dict) -> None:
        op = payload.get("op")

        if op == 0:
            if payload.get("s") is not None:
                self.seq = payload["s"]
            event = payload.get("t")
            data = payload.get("d") or {}

            if event == "READY":
                self.session_id = data.get("session_id")
                self.bot_id = (data.get("user") or {}).get("id")
                log.info("就绪: %s (%s)", (data.get("user") or {}).get("username"),
                         self.bot_id)
                return

            if event in ("GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"):
                # 不阻塞收包：每条消息开一个 task
                asyncio.create_task(self._handle(event, data))

        elif op == 7:  # 服务端要求重连
            log.info("服务端要求重连")
            await ws.close()

        elif op == 9:  # 无效 session，必须重新 Identify
            log.warning("收到 OpCode 9，清除 session")
            self.session_id = None
            self.seq = None
            await ws.close()

    # ------------------------------------------------------ 消息

    async def _handle(self, event: str, data: dict) -> None:
        msg_id = data.get("id", "")
        if not msg_id or self._dup(msg_id):
            return

        is_group = event == "GROUP_AT_MESSAGE_CREATE"
        target = data.get("group_openid") if is_group else (data.get("author") or {}).get("user_openid")
        if not target:
            return

        text = MENTION_RE.sub("", data.get("content") or "").strip()
        if not text:
            await self._reply(is_group, target, msg_id, "在的，说吧～")
            return

        key = f"{'g' if is_group else 'u'}:{target}"
        log.info("[%s] %s", key, text[:60])

        async with self._sem:
            try:
                answer = await self._ask(key, text)
            except Exception as exc:  # noqa: BLE001
                log.exception("模型调用失败")
                answer = f"脑袋卡住了（{type(exc).__name__}），再说一次？"

        await self._reply(is_group, target, msg_id, answer)

    async def _ask(self, key: str, text: str) -> str:
        hist = self._sessions.get(key)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(hist)
        messages.append({"role": "user", "content": text})

        resp = await self._ds.post(
            f"{DS_API}/chat/completions",
            headers={"Authorization": f"Bearer {DS_KEY}"},
            json={
                "model": DS_MODEL,
                "messages": messages,
                "max_tokens": 800,
                "temperature": 1.0,
                "stream": False,
            },
        )
        resp.raise_for_status()
        answer = resp.json()["choices"][0]["message"]["content"].strip()

        self._sessions.append(key, "user", text)
        self._sessions.append(key, "assistant", answer)
        return answer

    async def _reply(self, is_group: bool, target: str, msg_id: str, content: str) -> None:
        """被动回复：必须带 msg_id，群聊 5 分钟内最多 5 条，所以这里切块发。"""
        path = f"/v2/groups/{target}/messages" if is_group else f"/v2/users/{target}/messages"
        chunks = [content[i: i + MAX_CHARS] for i in range(0, len(content), MAX_CHARS)]
        if len(chunks) > REPLY_BUDGET:
            chunks = chunks[: REPLY_BUDGET]
            chunks[-1] = chunks[-1][: MAX_CHARS - 12] + "……（太长了，先说到这）"

        for seq, chunk in enumerate(chunks, start=1):
            body = {"content": chunk, "msg_type": 0, "msg_id": msg_id, "msg_seq": seq}
            try:
                resp = await self._http.post(
                    f"{QQ_API}{path}", json=body, headers=await self._tokens.headers()
                )
                if resp.status_code >= 300:
                    log.warning("发送失败 %s: %s", resp.status_code, resp.text[:200])
            except httpx.HTTPError as exc:
                log.warning("发送异常: %r", exc)
            await asyncio.sleep(0.3)  # 别把频控打爆

    def _dup(self, msg_id: str) -> bool:
        now = time.time()
        while self._seen and now - next(iter(self._seen.values())) > 600:
            self._seen.popitem(last=False)
        if msg_id in self._seen:
            return True
        self._seen[msg_id] = now
        return False


async def main() -> None:
    missing = [k for k, v in {
        "QQ_APP_ID": APP_ID, "QQ_APP_SECRET": APP_SECRET, "DS_API_KEY": DS_KEY,
    }.items() if not v]
    if missing:
        raise SystemExit(f"缺少环境变量: {', '.join(missing)}")

    bot = QQBot()
    log.info("模型 %s @ %s", DS_MODEL, DS_API)
    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
