"""B 站 web 端动态 API 客户端（纯网络层，不依赖 maibot_sdk）。

不使用 bilibili-api-python，自行实现 B 站 web 接口访问所需的：
  1. buvid3 / buvid4 浏览器指纹（x/frontend/finger/spi）
  2. WBI 签名（x/web-interface/nav 取 wbi_img → mixin key → w_rid）

参考 beiyu504/beiyu_bilibili-dynamic-push 的订阅/去重设计思路。
"""

import asyncio
import hashlib
import re
import time
import urllib.parse
from typing import Any, Optional

import httpx

from buvid_activation import activate_buvid

FEED_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
USER_INFO_URL = "https://api.bilibili.com/x/space/wbi/acc/info"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 一整套自洽的浏览器请求头。
#
# 关键：B 站 WAF 会校验 UA / sec-ch-ua / sec-fetch-* 的一致性。只发
# UA + Referer 的"半成品"请求在家庭宽带 IP 上通常能过，但换成服务器/机房 IP
# 就会被判为脚本，直接回 HTTP 412 + HTML 风控页（表现为"接口返回非 JSON"）。
# sec-ch-ua 里的版本号必须与 UA 中的 Chrome 版本一致。
DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Origin": "https://www.bilibili.com",
    "Referer": "https://www.bilibili.com/",
    "sec-ch-ua": '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "Connection": "keep-alive",
}

# WAF 拦截特征状态码（响应体通常是 HTML 而非 JSON）。
# 这是 HTTP 状态码，不是 B 站 JSON 里的 code 字段——两者都可能出现 412，别混淆。
WAF_STATUS = (403, 412, 429, 503)

# 业务层风控码：响应体仍是合法 JSON，但内容是拒绝。
# -412: "request was banned" 请求被禁（频率过高 / IP 被限制）
# -352: 风控校验失败
# 这些不该重试，但**应该触发退避**——拉长下次轮询间隔才有机会恢复。
RISK_CODES = (-412, -352)

# 图片下载域名白名单：动态图片只可能来自 B 站自家 CDN（hdslb.com），
# 收紧白名单防止把非 B 站 URL 拿来下载（SSRF 面）。
IMAGE_HOST_SUFFIXES = (
    ".hdslb.com",
    ".bilibili.com",
)

# 重试退避（秒）。被风控时立刻重试只会让计数器雪上加霜。
_RETRY_BACKOFF = (3.0, 8.0)

# WBI mixin key 重排表（B 站固定常量）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

# WBI key 有效期（B 站每日轮换，本地缓存 1 小时足够）
WBI_CACHE_TTL = 3600

# 开奖类动态文本特征（互动抽奖通知）
LOTTERY_RE = re.compile(r"恭喜@.*?中奖.*?详情请点击.*?查看", re.DOTALL)


class BiliApiError(Exception):
    """B 站接口返回异常。"""

    def __init__(self, code: int, message: str):
        super().__init__(f"接口错误 code={code} msg={message}")
        self.code = code
        self.message = message


def _build_mixin_key(img_url: str, sub_url: str) -> str:
    """由 wbi_img 的 img_url / sub_url 生成 mixin key。"""
    def _raw(url: str) -> str:
        return url.rsplit("/", 1)[-1].split(".")[0]

    raw = _raw(img_url) + _raw(sub_url)
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def _sign_params(params: dict[str, Any], mixin_key: str) -> dict[str, Any]:
    """给参数加上 wts 与 w_rid（WBI 签名）。"""
    signed = dict(params)
    signed["wts"] = int(time.time())
    query = urllib.parse.urlencode(sorted(signed.items()))
    query = "".join(ch for ch in query if ch not in "!'()*")
    signed["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return signed


class BiliClient:
    """轻量 B 站动态客户端（自带 buvid 指纹与 WBI 签名）。"""

    def __init__(
        self,
        cookie: str = "",
        timeout: float = 15.0,
        verify: bool = True,
    ):
        self._cookie = cookie.strip()
        self._verify = verify
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._client_stale = False
        self._lock = asyncio.Lock()
        self._mixin_key = ""
        self._mixin_ts = 0.0
        self._bootstrapped = False
        # ExClimbWuzhi 指纹激活状态（见 _maybe_activate）
        self._activated = False
        self._activation_failed = False

    @property
    def cookie(self) -> str:
        return self._cookie

    def set_cookie(self, cookie: str) -> None:
        """更新用户 Cookie；变更后会重建连接并重新握手。"""
        new_cookie = cookie.strip()
        if new_cookie != self._cookie:
            self._cookie = new_cookie
            self._mixin_key = ""
            self._mixin_ts = 0.0
            # Cookie 播种发生在建连时，变更必须让下次取 client 时重建
            self._client_stale = True

    @staticmethod
    def parse_cookie(cookie_str: str) -> dict[str, str]:
        """把整行 Cookie 字符串解析成键值字典。"""
        out: dict[str, str] = {}
        for part in (cookie_str or "").split(";"):
            name, sep, value = part.partition("=")
            if not sep:
                continue
            name, value = name.strip(), value.strip()
            if name and value:
                out[name] = value
        return out

    def _seed_cookies(self, client: httpx.AsyncClient) -> None:
        """把用户 Cookie 播种进 cookie jar，而不是塞进 headers。

        关键：http.cookiejar 在请求已自带 Cookie 头时不会再追加 jar 内容。
        若用 headers["Cookie"] 传用户 Cookie，后续 _bootstrap 写入 jar 的
        buvid3/buvid4 指纹将永远不会随请求发出，风控概率反而上升。
        """
        for name, value in self.parse_cookie(self._cookie).items():
            client.cookies.set(name, value, domain=".bilibili.com")

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client_stale and self._client is not None:
            await self.close()
        self._client_stale = False
        if self._client is None or self._client.is_closed:
            headers = dict(DEFAULT_HEADERS)
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                headers=headers,
                verify=self._verify,
                follow_redirects=True,
            )
            self._seed_cookies(client)
            self._client = client
        return self._client

    async def download_bytes(self, url: str) -> bytes:
        """下载图片二进制（限 B 站 CDN 域名白名单，供推送图片用）。

        白名单外域名直接抛 ValueError（如 i0.hdslb.com、boss.hdslb.com
        均以 hdslb.com 结尾，正常动态配图全部命中白名单）。
        """
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if not any(host == s.lstrip(".") or host.endswith(s) for s in IMAGE_HOST_SUFFIXES):
            raise ValueError(f"非 B 站 CDN 域名，拒绝下载: {host or url}")
        client = await self._ensure_client()
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # ---------- 握手：buvid 指纹 + 激活 + WBI key ----------

    async def _bootstrap(self) -> None:
        """拉取 buvid3/4 指纹、激活指纹、计算 WBI mixin key（幂等，带缓存）。"""
        if self._bootstrapped and self._mixin_key and (
            time.time() - self._mixin_ts < WBI_CACHE_TTL
        ):
            return
        async with self._lock:
            if self._bootstrapped and self._mixin_key and (
                time.time() - self._mixin_ts < WBI_CACHE_TTL
            ):
                return
            client = await self._ensure_client()

            # 1) 浏览器指纹 buvid3 / buvid4
            try:
                resp = await client.get(SPI_URL)
                data = resp.json()
                b3 = ((data.get("data") or {}).get("b_3") or "")
                b4 = ((data.get("data") or {}).get("b_4") or "")
                # 用户 Cookie 里自带的 buvid 是已跟账号绑定的"老"指纹，
                # 可信度高于临时申请的，不要覆盖
                if b3 and not client.cookies.get("buvid3", domain=".bilibili.com"):
                    client.cookies.set("buvid3", b3, domain=".bilibili.com")
                if b4 and not client.cookies.get("buvid4", domain=".bilibili.com"):
                    client.cookies.set("buvid4", b4, domain=".bilibili.com")
            except Exception:
                # 指纹失败不致命，继续尝试取 WBI key
                pass

            # 1.5) 激活指纹（ExClimbWuzhi 上报设备指纹）。
            # 通过 spi 拿到的 buvid 是"死指纹"，未激活会被 feed/space 等接口
            # 以 -412 request was banned 拒收。激活失败不阻断流程（老指纹
            # 可能本来就激活过），但记住状态，被拒时重试激活。
            await self._maybe_activate(client)

            # 2) WBI key（nav 未登录也会返回 wbi_img，只需该字段）
            resp = await client.get(NAV_URL)
            # nav 是后续一切请求的前提，被拦就没必要继续了；
            # 这里必须转成 BiliApiError，否则 JSONDecodeError 会冒泡成未捕获异常
            if resp.status_code in WAF_STATUS:
                raise BiliApiError(
                    resp.status_code,
                    f"nav 接口被风控拦截（HTTP {resp.status_code}），无法获取 WBI 签名素材",
                )
            try:
                nav = resp.json()
            except Exception:
                raise BiliApiError(
                    resp.status_code, "nav 接口返回非 JSON（疑似风控拦截）"
                )
            wbi = ((nav.get("data") or {}).get("wbi_img") or {})
            img_url = wbi.get("img_url") or ""
            sub_url = wbi.get("sub_url") or ""
            if not img_url or not sub_url:
                raise BiliApiError(
                    int(nav.get("code") or -1),
                    f"未能获取 WBI 签名素材（nav msg={nav.get('message')}）",
                )
            self._mixin_key = _build_mixin_key(img_url, sub_url)
            self._mixin_ts = time.time()
            self._bootstrapped = True

    async def _maybe_activate(self, client: httpx.AsyncClient, force: bool = False) -> None:
        """激活 buvid 指纹（ExClimbWuzhi 上报设备指纹）。

        - 用户 Cookie 自带 buvid3 → 已随账号绑定，视为已激活，不上报
        - spi 申请的新 buvid3 → 死指纹，需要上报激活
        - force=True（被 -412 拒后）：旧指纹可能已被标记，丢弃并换新指纹重新激活
        - 激活失败只标记，不反复重试（每次请求都是风控计数），
          等下次被 -412 拒时 force 重来
        """
        if self._activated and not force:
            return

        b3 = client.cookies.get("buvid3", domain=".bilibili.com") or ""
        user_has_b3 = bool(self._cookie) and "buvid3" in self.parse_cookie(self._cookie)

        if user_has_b3 and not force:
            # 登录态会话，指纹随账号走
            self._activated = True
            return

        if not b3 or force:
            # 无指纹，或需要强制换新：清掉旧的重新申请
            if b3:
                client.cookies.delete("buvid3", domain=".bilibili.com")
                client.cookies.delete("buvid4", domain=".bilibili.com")
            try:
                resp = await client.get(SPI_URL)
                data = resp.json()
                b3 = ((data.get("data") or {}).get("b_3") or "")
                b4 = ((data.get("data") or {}).get("b_4") or "")
                if b3:
                    client.cookies.set("buvid3", b3, domain=".bilibili.com")
                if b4:
                    client.cookies.set("buvid4", b4, domain=".bilibili.com")
            except Exception:
                pass
            if not b3:
                return

        if self._activation_failed and not force:
            return  # 之前失败过，不反复撞

        ok = await activate_buvid(client, b3, "", DEFAULT_UA)
        self._activated = ok
        self._activation_failed = not ok

    def _invalidate_wbi(self) -> None:
        self._mixin_key = ""
        self._mixin_ts = 0.0
        self._bootstrapped = False

    async def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """带 WBI 签名的 GET；签名失效（-403/412）或 WAF 拦截时退避重试一次。"""
        for attempt in (0, 1):
            await self._bootstrap()
            client = await self._ensure_client()
            signed = _sign_params(params, self._mixin_key)
            resp = await client.get(url, params=signed)

            # 判断顺序至关重要：必须先尝试解析 JSON，再谈状态码。
            #
            # B 站会在 HTTP 412 的同时返回合法 JSON：
            #     {"code":-412,"message":"request was banned","ttl":1}
            # 这是**业务层风控**，content-type 仍是 application/json。
            # 而 WAF 拦截返回的是 HTML 风控页，resp.json() 会失败。
            # 两者对策完全不同，按状态码一刀切会把前者的真实原因吞掉。
            #
            # 另外 412 与 -412 必须区分：
            #     412  = WBI 签名失效，重建签名重试有效
            #     -412 = request was banned，重试只会加重风控
            try:
                data = resp.json()
            except Exception:
                data = None

            if data is None:
                # 非 JSON + WAF 状态码 = HTML 风控页，这才是真 WAF 拦截
                if resp.status_code in WAF_STATUS:
                    if attempt == 0:
                        self._invalidate_wbi()
                        await asyncio.sleep(_RETRY_BACKOFF[attempt])
                        continue
                    raise BiliApiError(
                        resp.status_code,
                        f"B 站 WAF 拦截（HTTP {resp.status_code}，响应非 JSON）。"
                        "可尝试：① 填写 bili.cookie ② 调大 poll_interval "
                        "③ 若长期持续，多半是本机 IP 被限制",
                    )
                if attempt == 0:
                    self._invalidate_wbi()
                    await asyncio.sleep(_RETRY_BACKOFF[attempt])
                    continue
                raise BiliApiError(
                    resp.status_code or -1,
                    f"接口返回非 JSON（HTTP {resp.status_code}）",
                )

            code = int(data.get("code") or 0)
            if code == 0:
                return data.get("data") or {}
            msg = str(data.get("message") or "")
            if attempt == 0 and code in (-403, 412):
                # 签名失效：重建签名后重试一次
                self._invalidate_wbi()
                await asyncio.sleep(_RETRY_BACKOFF[attempt])
                continue
            if code in RISK_CODES:
                # -412 "request was banned" / -352：业务层风控。
                # 响应体是合法 JSON，请求已送达 B 站应用层——这不是签名问题，
                # 重建签名或立刻重试都没用，只会加重风控。
                if code == -412 and attempt == 0 and not self._cookie:
                    # 首选自救：丢弃当前指纹，换新指纹 + ExClimbWuzhi 重新激活，
                    # 再试最后一次。实测未激活的"死指纹"是 -412 的主要成因。
                    client = await self._ensure_client()
                    await self._maybe_activate(client, force=True)
                    await asyncio.sleep(_RETRY_BACKOFF[attempt])
                    continue
                hint = (
                    "请求被 B 站风控拒绝（可能是轮询频率过高或 IP 信誉不足）。"
                    "可尝试：① 填写 bili.cookie ② 调大 poll_interval"
                ) if code == -412 else "风控校验失败"
                raise BiliApiError(code, f"{msg}。{hint}" if msg else hint)
            raise BiliApiError(code, msg or f"接口返回 code={code}")
        return {}

    # ---------- 业务接口 ----------

    async def fetch_user_info(self, uid: int) -> dict[str, Any]:
        """拉取 UP 主基础信息（昵称、头像）。"""
        data = await self._get_json(USER_INFO_URL, {"mid": uid})
        return {
            "uid": uid,
            "name": data.get("name") or f"UID:{uid}",
            "face": data.get("face") or "",
            "sign": data.get("sign") or "",
        }

    async def fetch_dynamics(self, uid: int) -> list[dict[str, Any]]:
        """拉取 UP 主最新动态列表。

        该接口在无 Cookie 访问时偶发返回空 items（B 站后端/CDN 抖动，非错误码），
        因此空结果重试一次。调用方拿到空列表时不应视为"该 UP 主无动态"，
        更不应据此推进去重基准，否则会漏推。

        features=itemOpusStyle 至关重要（真机实测踩坑）：
        不带它时，B 站会把新版 OPUS 图文动态降级为 MAJOR_TYPE_DRAW 返回，
        标题(opus.title)与正文(opus.summary.text)被整体丢弃，只剩图片，
        推送出来就是"只有图没有文字"。带上后图文动态以 MAJOR_TYPE_OPUS
        返回，标题和正文齐全。官方文档（bilibili-API-collect space.md）
        对该参数有明确说明。
        """
        for attempt in (0, 1):
            data = await self._get_json(
                FEED_URL, {"host_mid": uid, "features": "itemOpusStyle"}
            )
            items = data.get("items") or []
            if not isinstance(items, list):
                return []
            if items:
                return items
            if attempt == 0:
                await asyncio.sleep(2)
        return []

    async def resolve_up_name(self, uid: int) -> str:
        """获取 UP 主昵称（从动态流取，永不抛异常）。

        刻意不调用 acc/info：该接口风控极严，未登录时返回 -352，
        且实测一旦返回 -352，同一会话随后的 feed 请求会被连累返回 0 条
        （B 站风控是按会话/IP 累积的）。昵称在动态流 module_author 里本就有。
        """
        try:
            for item in await self.fetch_dynamics(uid):
                name = ((item.get("modules") or {}).get("module_author") or {}).get("name")
                if name:
                    return str(name)
        except Exception:
            pass
        return f"UID:{uid}"


# ---------------- 动态解析（静态方法，可独立测试） ----------------

def is_top_dynamic(item: dict[str, Any]) -> bool:
    """置顶动态：module_tag.text 含「置顶」。"""
    try:
        tag_text = (item.get("modules", {}).get("module_tag") or {}).get("text") or ""
        return "置顶" in tag_text
    except Exception:
        return False


def is_forward_dynamic(item: dict[str, Any]) -> bool:
    """转发动态。"""
    return item.get("type") == "DYNAMIC_TYPE_FORWARD"


def is_live_rcmd(item: dict[str, Any]) -> bool:
    """直播开播通知动态（直播状态另行查询，动态流里跳过）。"""
    if item.get("type") == "DYNAMIC_TYPE_LIVE_RCMD":
        return True
    try:
        major = (item.get("modules", {}).get("module_dynamic") or {}).get("major") or {}
        return major.get("type") == "MAJOR_TYPE_LIVE_RCMD"
    except Exception:
        return False


def _extract_major(module_dynamic: dict[str, Any]) -> tuple[str, list[str], Optional[dict[str, Any]]]:
    """从 module_dynamic 提取 (正文文本, 图片列表, 视频信息)。"""
    text = ""
    images: list[str] = []
    video: Optional[dict[str, Any]] = None
    major = module_dynamic.get("major") or {}
    mtype = major.get("type")

    if mtype in ("MAJOR_TYPE_OPUS", "MAJOR_TYPE_ARTICLE"):
        opus = major.get("opus") or {}
        summary_text = (opus.get("summary") or {}).get("text") or ""
        title = opus.get("title") or ""
        # 标题与正文都是内容（APP 样式：标题加粗在正文上方），
        # 只取 summary 会把"夏天再见，逃离人间"这类标题丢掉
        if title and summary_text:
            text = f"{title}\n{summary_text}"
        else:
            text = title or summary_text
        images = [p.get("url") for p in (opus.get("pics") or []) if p.get("url")]
    elif mtype == "MAJOR_TYPE_DRAW":
        for it in (major.get("draw") or {}).get("items") or []:
            if it.get("src"):
                images.append(it["src"])
    elif mtype in ("MAJOR_TYPE_ARCHIVE", "MAJOR_TYPE_VIDEO"):
        v = major.get("archive") or major.get("video") or {}
        title = v.get("title") or "视频投稿"
        cover = v.get("cover") or ""
        bvid = v.get("bvid") or ""
        text = f"📺 {title}"
        if cover:
            images.append(cover)
        video = {"bvid": bvid, "title": title, "cover": cover}

    return text, images, video


def parse_dynamic(item: dict[str, Any], ignore_lottery: bool = True) -> Optional[dict[str, Any]]:
    """把一条原始动态解析成推送所需结构；开奖动态返回 None。

    ignore_lottery=False 时不做开奖过滤（对应插件配置 ignore_lottery）。
    """
    try:
        id_str = str(item.get("id_str") or item.get("id") or "")
        if not id_str:
            return None
        modules = item.get("modules") or {}
        module_dynamic = modules.get("module_dynamic") or {}
        module_author = modules.get("module_author") or {}

        major_text, images, video = _extract_major(module_dynamic)
        desc_text = (module_dynamic.get("desc") or {}).get("text") or ""

        # 开奖过滤（ignore_lottery=False 时保留开奖动态）
        if ignore_lottery and LOTTERY_RE.search(f"{desc_text}\n{major_text}"):
            return None

        pub_ts = 0
        try:
            pub_ts = int(module_author.get("pub_ts") or 0)
        except (ValueError, TypeError):
            pub_ts = 0

        result: dict[str, Any] = {
            "id": id_str,
            "author": module_author.get("name") or "UP主",
            "pub_ts": pub_ts,
            "text": "",
            "images": images[:9],
            "video": video,
            "url": f"https://t.bilibili.com/{id_str}",
            "top": is_top_dynamic(item),
            "forward": is_forward_dynamic(item),
            # 被转发动态的地址（非转发动态为空）。转发视频投稿时用它给出原视频入口，
            # 而不是转发动态本身——转发动态点进去还要再跳一次。
            "orig_url": "",
        }
        if desc_text:
            result["text"] += desc_text
        if major_text:
            result["text"] += ("\n" if result["text"] else "") + major_text

        # 转发：附加原作者与原动态内容
        if result["forward"]:
            orig = item.get("orig") or {}
            if orig.get("type") == "DYNAMIC_TYPE_NONE":
                result["text"] += "\n\n[原动态已被删除]"
            else:
                om = orig.get("modules") or {}
                oauthor = (om.get("module_author") or {}).get("name") or "未知用户"
                orig_id = str(orig.get("id_str") or "")
                if orig_id:
                    result["orig_url"] = f"https://t.bilibili.com/{orig_id}"
                omd = om.get("module_dynamic") or {}
                odesc = (omd.get("desc") or {}).get("text") or ""
                otext, oimgs, ovideo = _extract_major(omd)
                result["text"] += f"\n\n🔁 转发 @{oauthor}:"
                if odesc:
                    result["text"] += f"\n{odesc}"
                if otext:
                    result["text"] += f"\n{otext}"
                result["images"] = (result["images"] + oimgs)[:9]
                if ovideo:
                    result["video"] = ovideo

        return result
    except Exception:
        return None
