"""B 站接口诊断工具（在真机 / MaiBot 运行环境执行，不需要启动 MaiBot）。

用法：
    <MaiBot 的 python> diagnose_bili.py [UID] ["cookie字符串"]

    # 例
    python diagnose_bili.py 517327498
    python diagnose_bili.py 517327498 "SESSDATA=xxx; DedeUserID=yyy"

也可把本文件放在插件目录内，不填 cookie 参数时会尝试读取同目录的 config.toml。

只依赖 httpx（MaiBot 主程序已带）。
"""

import os
import sys
import time
import urllib.parse
import hashlib
from pathlib import Path

try:
    import httpx
except ImportError:
    print("!! 缺少 httpx。请用 MaiBot 的 python 执行本脚本。")
    sys.exit(1)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
FULL_HEADERS = {
    "User-Agent": UA,
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
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]
FEED = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
NAV = "https://api.bilibili.com/x/web-interface/nav"
SPI = "https://api.bilibili.com/x/frontend/finger/spi"

line = "-" * 68


def build_mixin_key(img: str, sub: str) -> str:
    raw = (img.rsplit("/", 1)[-1].split(".")[0]) + (sub.rsplit("/", 1)[-1].split(".")[0])
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def sign(params: dict, mixin: str) -> dict:
    s = dict(params)
    s["wts"] = int(time.time())
    q = urllib.parse.urlencode(sorted(s.items()))
    q = "".join(c for c in q if c not in "!'()*")
    s["w_rid"] = hashlib.md5((q + mixin).encode()).hexdigest()
    return s


def seed(client: httpx.Client, cookie: str) -> None:
    for part in (cookie or "").split(";"):
        name, sep, val = part.partition("=")
        if sep and name.strip() and val.strip():
            client.cookies.set(name.strip(), val.strip(), domain=".bilibili.com")


def probe(label: str, client: httpx.Client, url: str, params: dict, mixin: str):
    """返回 (HTTP 状态码, 响应文本, 业务 code, 业务 message)。"""
    try:
        r = client.get(url, params=sign(params, mixin) if mixin else params)
    except Exception as exc:  # noqa: BLE001
        print(f"  [{label}] 请求异常: {type(exc).__name__}: {exc}")
        return -1, "", None, ""
    ct = r.headers.get("content-type", "")
    # HTTP 412 也可能带合法 JSON（业务层风控），所以照常尝试解析，
    # 否则会把 B 站给的真实原因（如 request was banned）丢掉
    code = None
    msg = ""
    try:
        j = r.json()
        code = j.get("code")
        msg = str(j.get("message") or "")
    except Exception:  # noqa: BLE001
        pass
    print(f"  [{label}] HTTP {r.status_code}  content-type={ct}")
    print(f"          biz_code={code}  message={msg or '(空)'}")
    print(f"          响应预览: {r.text[:220].replace(chr(10), ' ')}")
    return r.status_code, r.text, code, msg


def main() -> None:
    uid = sys.argv[1] if len(sys.argv) > 1 else "517327498"
    cookie = sys.argv[2] if len(sys.argv) > 2 else ""

    if not cookie:
        cfg = Path(__file__).resolve().parent / "config.toml"
        if cfg.exists():
            try:
                import tomllib

                data = tomllib.loads(cfg.read_text(encoding="utf-8"))
                cookie = (data.get("bili") or {}).get("cookie", "")
                if cookie:
                    print(f"（已从 {cfg.name} 读取 Cookie）")
            except Exception as exc:  # noqa: BLE001
                print(f"（读取 config.toml 失败: {exc}）")

    print(line)
    print("B 站接口诊断")
    print(line)

    # 1) 环境
    print("[1] 运行环境")
    print(f"  python   : {sys.version.split()[0]}  ({sys.executable})")
    print(f"  httpx    : {httpx.__version__}")
    try:
        import h2  # noqa: F401

        h2_state = "可用（可尝试 HTTP/2）"
    except ImportError:
        h2_state = "未安装"
    print(f"  h2(HTTP2): {h2_state}")
    proxies = {k: v for k, v in os.environ.items()
               if k.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    print(f"  代理变量 : {proxies or '无'}")
    for k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        if os.environ.get(k):
            print(f"  {k} = {os.environ[k]}")
    print(f"  Cookie   : {'已提供（长度 %d）' % len(cookie) if cookie else '未提供（无登录态）'}")

    # 2) 路由对比：走系统代理 vs 绕过代理直连
    #
    # 决定性的一个测试：HTTP 头补全了仍 412，问题往往不在应用层而在传输层——
    # WAF 用 TLS 握手特征（JA3/JA4）识别爬虫。Python httpx 的 TLS 指纹与
    # 真实 Chrome 差别很大；走代理时握手是跟代理做的，指纹被"洗"成代理的，
    # 反而可能通过。若"走代理 200 / 直连 412"，即可确认是 TLS 指纹问题。
    print()
    print("[2] 路由对比（判断是否为 TLS 指纹 / 代理问题）")
    route_result: dict[str, int] = {}
    for label, trust_env in (("走系统代理", True), ("绕过代理直连", False)):
        try:
            with httpx.Client(timeout=15, verify=True, headers=FULL_HEADERS,
                              trust_env=trust_env) as c:
                r = c.get(NAV)
            try:
                code = r.json().get("code")
            except Exception:  # noqa: BLE001
                code = "-"
            route_result[label] = r.status_code
            print(f"  {label:<12} -> HTTP {r.status_code}, nav code={code}")
        except Exception as exc:  # noqa: BLE001
            route_result[label] = -1
            print(f"  {label:<12} -> 异常 {type(exc).__name__}: {str(exc)[:140]}")
            if "SSL" in type(exc).__name__ or "Certif" in str(exc):
                print("               ↑ MITM 代理导致的证书问题，"
                      "应把插件的 bili.verify_ssl 设为 false（或配好 CA）")

    if route_result.get("走系统代理") == 200 and route_result.get("绕过代理直连") in (403, 412, 429, 503):
        print()
        print("  >>> 命中 TLS 指纹拦截：走代理正常、直连被拦。")
        print("      说明 B 站 WAF 识别的是传输层特征，再怎么改 HTTP 头都没用。")
        print("      对策：① 填 Cookie（登录态能显著放宽指纹检查，最有效）")
        print("            ② 让本机也走代理 ③ 换网络（机房 IP 是重灾区）")

    # 后续流程采用可用的路由方式
    use_trust_env = route_result.get("走系统代理") == 200

    # 3) 完整流程：spi -> nav -> feed
    print(f"  路由方式 : {'走系统代理' if use_trust_env else '绕过代理直连'}")
    print()
    print("[3] 完整握手流程（spi 指纹 -> nav 取 WBI key -> feed 拉动态）")
    mixin = ""
    with httpx.Client(timeout=15, verify=True, headers=FULL_HEADERS,
                      follow_redirects=True, trust_env=use_trust_env) as c:
        seed(c, cookie)
        b3 = b4 = ""
        try:
            d = c.get(SPI).json()
            b3 = ((d.get("data") or {}).get("b_3") or "")
            b4 = ((d.get("data") or {}).get("b_4") or "")
            if b3:
                c.cookies.set("buvid3", b3, domain=".bilibili.com")
            if b4:
                c.cookies.set("buvid4", b4, domain=".bilibili.com")
            print(f"  spi 指纹 : {'ok' if b3 else '失败'}")
        except Exception as exc:  # noqa: BLE001
            print(f"  spi 指纹 : 失败 {type(exc).__name__}")
            b3 = ""

        # 激活指纹（ExClimbWuzhi 上报设备指纹）——死指纹是 -412 的主要成因。
        # 诊断脚本是同步的，用 buvid_activation 的纯函数 + 同步 POST。
        act_note = "跳过（用户 Cookie 自带 buvid3，视为已激活）"
        if b3 and not ("buvid3" in (cookie or "")):
            try:
                from buvid_activation import (
                    build_payload as _bp,
                    gen_buvid_fp as _gfp,
                    gen_uuid_infoc as _gu,
                )

                _uuid = _gu()
                _payload = _bp(FULL_HEADERS["User-Agent"], _uuid)
                _fp = _gfp(_payload)
                r = c.post(
                    "https://api.bilibili.com/x/internal/gaia-gateway/ExClimbWuzhi",
                    content=_payload,
                    headers={**FULL_HEADERS, "Content-Type": "application/json"},
                    cookies={
                        "buvid3": b3,
                        "buvid4": b4 or "",
                        "buvid_fp": _fp,
                        "_uuid": _uuid,
                    },
                )
                _code = r.json().get("code")
                act_note = (
                    "✅ 成功" if _code == 0 else f"❌ 失败（code={_code} {r.text[:100]}）"
                )
            except Exception as exc:  # noqa: BLE001
                act_note = f"异常 {type(exc).__name__}: {str(exc)[:80]}"
        print(f"  指纹激活 : {act_note}")

        nav = c.get(NAV)
        nav_json = {}
        try:
            nav_json = nav.json()
        except Exception:  # noqa: BLE001
            pass
        wbi = (nav_json.get("data") or {}).get("wbi_img") or {}

        # Cookie 是否真的生效 —— 填了却没生效是最常见的"我明明配了为什么还被拦"
        if cookie:
            keys = [p.split("=")[0].strip() for p in cookie.split(";") if "=" in p]
            print(f"  Cookie 字段: {', '.join(keys) if keys else '（解析失败）'}")
            if "SESSDATA" not in keys:
                print("  ❌ 缺少 SESSDATA —— 登录态不会生效，请重新复制完整 Cookie")

        if wbi.get("img_url"):
            mixin = build_mixin_key(wbi["img_url"], wbi["sub_url"])
            print(f"  WBI key  : ok (mixin {mixin[:8]}...)")
        else:
            print(f"  WBI key  : 失败（HTTP {nav.status_code}，无法签名后续请求）")
            print(f"          响应预览: {nav.text[:200]}")
            return

        nav_code = nav_json.get("code")
        uname = ((nav_json.get("data") or {}).get("uname") or "")
        if nav_code == 0 and uname:
            print(f"  登录状态 : ✅ 已登录（{uname}）")
        elif cookie:
            print(f"  登录状态 : ❌ Cookie 未生效（nav code={nav_code} "
                  f"{nav_json.get('message')}）")
            print("             → Cookie 多半已过期，请按 README 重新获取")
        else:
            print(f"  登录状态 : 未填 Cookie（nav code={nav_code}）")

        st, body, biz_code, biz_msg = probe("feed", c, FEED, {"host_mid": uid}, mixin)
        n_items = -1
        if biz_code == 0:
            try:
                n_items = len(__import__("json").loads(body).get("data", {}).get("items") or [])
            except Exception:  # noqa: BLE001
                pass
            print(f"          动态条数: {n_items}")

    # 4) 结论
    print()
    print(line)
    print("[4] 结论")
    if biz_code == 0 and n_items > 0:
        print(f"  ✅ 接口完全正常，拉到 {n_items} 条动态。")
        print("     若插件仍报错，检查配置是否热更新生效、UID 是否写对。")
    elif biz_code == 0 and n_items == 0:
        print("  ⚠️ 接口通了，但返回 0 条动态。两种可能：")
        print("     1. 接口抖动（无 Cookie 时常见，插件已内置空结果重试）——换个时间再测")
        print("     2. UID 没有动态，或 UID 填错了")
        print("     → 建议：填 Cookie 后重测，若仍为 0 则核对 UID")
    elif biz_code in (-412, -352):
        print(f"  ❌ 业务层风控：code={biz_code}（{biz_msg}）——不是 WAF 的 HTML 拦截，"
              "重试无用。")
        if biz_code == -412:
            print("     'request was banned' = 该请求被禁，通常是频率过高或 IP/账号被限：")
            print("     1. 把 poll_interval 调到 600 秒以上（当前若已很小，是最常见原因）")
            print("     2. 确认上面 [3] 的登录状态是 ✅ —— Cookie 失效会被当作可疑匿名请求")
            print("     3. 减少订阅数量；别反复用 /dyn test 手动触发")
            print("     4. 若几分钟内连续被禁，先停一段时间（风控计数需要时间衰减）")
        else:
            print("     -352 风控校验失败：确认 Cookie 有效，降低请求频率。")
    elif st in (403, 412, 429, 503):
        print(f"  ❌ 被 WAF 拦截（HTTP {st}）。按优先级尝试：")
        print("     1. 填 Cookie —— 登录态能放宽 TLS 指纹与频率检查，最有效的一招")
        print("     2. 若上文 [2] 显示'走代理正常、直连被拦'，让本机也走代理")
        print("     3. 调大 poll_interval 到 300+ 秒")
        print("     4. 本机 IP 被限（云服务器/机房 IP 为重灾区），换网络或换机器")
        if not cookie:
            print()
            print("  ⚠️  本次诊断未提供 Cookie。请用登录态 Cookie 重跑一次再下结论：")
            print(f'     python {Path(__file__).name} {uid} "SESSDATA=xxx; DedeUserID=yyy"')
    else:
        print(f"  ⚠️ 未预期状态：{st}")
    print(line)


if __name__ == "__main__":
    main()
