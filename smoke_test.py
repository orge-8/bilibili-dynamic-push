"""冒烟测试：不启动 MaiBot，用假 Host 跑通插件命令与监控逻辑。"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN_DIR))

from maibot_sdk.context import PluginContext, PluginPaths

import plugin as plugin_mod
from bili_client import BiliClient, parse_dynamic

SENT: list[tuple[str, str]] = []
CONSECUTIVE_EMPTY_OK = True


async def rpc_call(method, plugin_id, payload, timeout_ms=None):
    cap = payload["capability"]
    args = payload.get("args") or {}
    if cap == "send.text":
        SENT.append(("text", args.get("text", "")))
        return True
    if cap == "send.image":
        SENT.append(("image", f"<{len(args.get('image_base64',''))} chars b64>"))
        return True
    if cap == "chat.open_session":
        return {"stream_id": f"stream-group-{args.get('group_id')}"}
    if cap.startswith("config."):
        return {}
    print("  [rpc]", method, cap, args)
    return True


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="bili_push_smoke_"))

    plug = plugin_mod.create_plugin()
    plug._set_context(
        PluginContext(
            "org.mai-mai.bilibili-dynamic-push",
            rpc_call,
            PluginPaths(data_dir=tmp / "data", runtime_dir=tmp / "runtime"),
        )
    )
    plug.set_plugin_config(plug.get_default_config())

    print("== on_load ==")
    await plug.on_load()
    assert plug._running, "监控循环应自动启动"

    print("== 命令 pattern：/dyn /动态 /订阅 可用，/bili 必须失效 ==")
    import re

    cmd_info = plugin_mod.BiliPushPlugin.cmd_dyn.__maibot_component_info__
    pat = cmd_info.command_pattern
    print(f"  name = {cmd_info.name!r}  aliases = {cmd_info.aliases}")
    assert cmd_info.name == "dyn", f"命令名应为 dyn，实际 {cmd_info.name!r}"

    for good in (
        "/dyn", "/dyn help", "/dyn add 517327498", "/dyn  add  517327498",
        "／dyn list", "/  dyn status",           # 全角斜杠、多余空格
        "/动态", "/动态 add 123", "/订阅", "/订阅 remove 9",
    ):
        assert re.fullmatch(pat, good), f"应匹配但未匹配: {good!r}"

    # 用户明确要求：命令不再以 /bili 开头
    for bad in ("/bili", "/bili add 1", "/bilibili", "/dyna", "dyn", "/dynx", "/其他", "/dy n"):
        assert not re.fullmatch(pat, bad), f"不应匹配但匹配了: {bad!r}"

    m = re.fullmatch(pat, "/dyn add 517327498")
    assert m.group("action") == "add" and m.group("arg") == "517327498", m.groupdict()
    m = re.fullmatch(pat, "/动态")
    assert m.group("action") is None, m.groupdict()  # 无子命令 -> 回落 help
    print("  -> 三个前缀均可用，/bili 已失效，分组捕获正确")

    print("== /dyn help ==")
    r = await plug.cmd_dyn(matched_groups={"action": "help"}, stream_id="s-1")
    print("  ->", r)
    assert SENT[-1][0] == "text"

    print("== /dyn status ==")
    r = await plug.cmd_dyn(matched_groups={"action": "status"}, stream_id="s-1")
    print("  ->", r)

    print("== /dyn status：Cookie 三态（且不得回显内容）==")
    for raw, keyword in (
        ("", "未配置"),
        ("DedeUserID=123; bili_jct=xyz", "缺少 SESSDATA"),
        ('SESSDATA=SUPERSECRET123; DedeUserID=123', "含 SESSDATA"),
    ):
        plug.config.bili.cookie = raw
        r = await plug.cmd_dyn(matched_groups={"action": "status"}, stream_id="s-1")
        assert keyword in r[1], f"Cookie 状态未正确显示（期望 {keyword}）: {r[1]}"
        # 安全红线：任何情况下都不得把 Cookie 原始值回显到群里
        assert "SUPERSECRET123" not in r[1], "Cookie 内容被回显到聊天！"
    plug.config.bili.cookie = ""
    print("  -> 三态显示正确，无 Cookie 回显")

    print("== /dyn list (无订阅) ==")
    r = await plug.cmd_dyn(
        matched_groups={"action": "list"}, stream_id="s-1", group_id="12345"
    )
    print("  ->", r)

    print("== store: add / sync_from_config 单测 ==")
    from subscription_store import SubscriptionStore, PushHistory

    subs = SubscriptionStore(str(tmp / "data"))
    await subs.add("114514", 1919810, name="测试UP")
    assert subs.groups_of("114514") == [1919810]
    assert subs.get_name("114514") == "测试UP"
    subs.sync_from_config(["36081646 => 111, 222", "bad line", "999 => notnum"])
    assert 111 in subs.groups_of("36081646") and 222 in subs.groups_of("36081646")
    assert "999" not in subs.uid_list()  # 无效群号行被忽略
    assert subs.is_fixed("36081646") and not subs.is_fixed("114514")

    hist = PushHistory(str(tmp / "data"))
    await hist.set_last("114514", "900", "800")
    assert hist.get("114514")["dyn_id"] == "900"

    print("== bili_client Cookie 播种回归测试 ==")
    # 回归：用户 Cookie 曾走 headers["Cookie"]，导致 jar 里的 buvid 指纹
    # 永远发不出去（http.cookiejar 不会往已有 Cookie 头的请求里追加）。
    from bili_client import BiliClient

    ck = "SESSDATA=abc123; bili_jct=deadbeef; DedeUserID=517327498; buvid3=OLD-B3"
    assert BiliClient.parse_cookie(ck)["SESSDATA"] == "abc123"
    assert BiliClient.parse_cookie(ck)["DedeUserID"] == "517327498"
    assert BiliClient.parse_cookie("garbage; =; ;;SESSDATA=x")["SESSDATA"] == "x"

    bc = BiliClient(cookie=ck)
    c = await bc._ensure_client()
    # 模拟 _bootstrap 写入临时申请的指纹
    c.cookies.set("buvid4", "NEW-B4", domain=".bilibili.com")
    req = c.build_request("GET", "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space")
    sent = req.headers.get("cookie", "")
    assert "SESSDATA=abc123" in sent, f"用户 Cookie 丢失: {sent}"
    assert "buvid4=NEW-B4" in sent, f"jar 中的 buvid 指纹被吞: {sent}"
    assert "buvid3=OLD-B3" in sent, f"用户自带 buvid 丢失: {sent}"
    assert "Cookie" not in c.headers, "用户 Cookie 不应再写进默认 headers"
    await bc.close()

    # Cookie 热更新后必须重建连接，否则新 Cookie 不会生效
    bc2 = BiliClient(cookie="")
    await bc2._ensure_client()
    bc2.set_cookie("SESSDATA=newvalue")
    c2 = await bc2._ensure_client()
    req2 = c2.build_request("GET", "https://api.bilibili.com/x/test")
    assert "SESSDATA=newvalue" in req2.headers.get("cookie", ""), "Cookie 热更新未生效"
    await bc2.close()
    print("  -> 用户 Cookie 与 buvid 指纹共存，热更新生效")

    print("== bili_client.parse_dynamic 单测 ==")
    from bili_client import parse_dynamic, is_top_dynamic, is_forward_dynamic

    fake_item = {
        "id_str": "123456",
        "type": "DYNAMIC_TYPE_DRAW",
        "modules": {
            "module_author": {"name": "测试UP", "pub_ts": 1700000000},
            "module_dynamic": {
                "desc": {"text": "新歌发布啦"},
                "major": {
                    "type": "MAJOR_TYPE_DRAW",
                    "draw": {"items": [{"src": "http://img/1.jpg"}]},
                },
            },
        },
    }
    p = parse_dynamic(fake_item)
    assert p and p["id"] == "123456" and "新歌发布啦" in p["text"]
    assert p["url"] == "https://t.bilibili.com/123456"

    top_item = {"id_str": "1", "modules": {"module_tag": {"text": "置顶"}}}
    assert is_top_dynamic(top_item)
    assert is_forward_dynamic({"type": "DYNAMIC_TYPE_FORWARD"})

    lottery = {
        "id_str": "2",
        "modules": {"module_dynamic": {"desc": {"text": "恭喜@xx中奖了！详情请点击链接查看"}}},
    }
    # 开奖正则需同时匹配"恭喜@xx中奖"与"详情请点击…查看"
    assert parse_dynamic(lottery) is None

    print("== 推送格式：图文不带链接，视频自动附链接 ==")
    # 用户需求：不要动态链接，要动态里的图片和文字本身
    draw_text = plug._render_push_text(p, "测试UP")
    assert "新歌发布啦" in draw_text and "测试UP" in draw_text
    assert "t.bilibili.com" not in draw_text, f"图文推送不应带链接: {draw_text!r}"

    video_item = {
        "id_str": "789",
        "type": "DYNAMIC_TYPE_AV",
        "modules": {
            "module_author": {"name": "测试UP", "pub_ts": 1700000000},
            "module_dynamic": {
                "desc": {"text": ""},
                "major": {
                    "type": "MAJOR_TYPE_ARCHIVE",
                    "archive": {
                        "title": "新视频标题",
                        "cover": "http://img/cover.jpg",
                        "bvid": "BV1xx411c7mD",
                    },
                },
            },
        },
    }
    pv = parse_dynamic(video_item)
    assert pv and pv["video"] and pv["video"]["bvid"] == "BV1xx411c7mD", pv
    video_text = plug._render_push_text(pv, "测试UP")
    assert "新视频标题" in video_text, video_text
    assert "🔗 https://t.bilibili.com/789" in video_text, f"视频推送应附链接: {video_text!r}"

    # 模板显式含 {url} 时：视频链接不得重复附加；图文也带上链接
    plug.config.settings.push_text_template = (
        "📢 {name} 发布了新动态\n{text}\n\n🔗 {url}"
    )
    video_text2 = plug._render_push_text(pv, "测试UP")
    assert video_text2.count("https://t.bilibili.com/789") == 1, video_text2
    draw_text2 = plug._render_push_text(p, "测试UP")
    assert "https://t.bilibili.com/123456" in draw_text2, draw_text2
    plug.config.settings.push_text_template = (
        "📢 {name} 发布了新动态\n{text}"
    )
    print("  -> 图文只推文字图片、视频附链接、模板含 {url} 时不重复")

    print("== OPUS 图文动态：标题+正文完整解析 ==")
    # 真机实测踩坑（共鸣电台 1242522747324596258，miku 生日动态）：
    # feed/space 不带 features=itemOpusStyle 时，B 站把 OPUS 图文降级为
    # MAJOR_TYPE_DRAW 返回，标题与正文被整体丢弃只剩图片——
    # 推送出来就是"只有图没有文字"。
    # 修复后：请求必须带 features=itemOpusStyle，且 OPUS 解析要拼标题。
    opus_item = {
        "id_str": "1242522747324596258",
        "type": "DYNAMIC_TYPE_DRAW",
        "modules": {
            "module_author": {"name": "共鸣电台_FMInfinity", "pub_ts": 1788135771},
            "module_dynamic": {
                "major": {
                    "type": "MAJOR_TYPE_OPUS",
                    "opus": {
                        "title": "夏天再见，逃离人间",
                        "summary": {"text": "另祝我们的miku生日快乐\n如果把这条消息转发到5个群……"},
                        "pics": [{"url": "http://img/miku.jpg"}],
                    },
                },
            },
        },
    }
    po = parse_dynamic(opus_item)
    assert po, "OPUS 动态解析失败"
    assert "夏天再见，逃离人间" in po["text"], f"OPUS 标题丢失: {po['text']!r}"
    assert "另祝我们的miku生日快乐" in po["text"], f"OPUS 正文丢失: {po['text']!r}"
    assert po["text"].startswith("夏天再见，逃离人间\n"), "标题应在正文前"
    assert po["images"] == ["http://img/miku.jpg"]
    # 标题为空时退化到正文；正文为空时退化到标题
    only_title = parse_dynamic({
        "id_str": "3",
        "modules": {"module_dynamic": {"major": {
            "type": "MAJOR_TYPE_OPUS",
            "opus": {"title": "只有标题", "summary": {"text": ""}, "pics": []},
        }}},
    })
    assert only_title and only_title["text"] == "只有标题", only_title

    # features 参数必须随请求发出（打桩 _get_json 捕获 params）
    captured = {}

    async def stub_get_json(url, params):
        captured["url"] = url
        captured["params"] = dict(params)
        return {"items": [{"id_str": "1"}]}

    bc3 = BiliClient()
    orig_gj = bc3._get_json
    bc3._get_json = stub_get_json
    try:
        await bc3.fetch_dynamics(3690973091596928)
    finally:
        bc3._get_json = orig_gj
    assert captured["params"].get("features") == "itemOpusStyle", (
        f"feed 请求未带 features=itemOpusStyle: {captured.get('params')}"
    )
    print("  -> OPUS 标题+正文齐全，feed 请求带 features=itemOpusStyle")

    print("== 纯配图动态（无文字有图片）不显示占位符 ==")
    # DRAW 类型且确实无文字的纯配图动态（真机常见），图片本身就是内容
    no_text_draw = {
        "id_str": "555",
        "type": "DYNAMIC_TYPE_DRAW",
        "modules": {
            "module_author": {"name": "测试UP", "pub_ts": 1700000000},
            "module_dynamic": {
                "major": {
                    "type": "MAJOR_TYPE_DRAW",
                    "draw": {"items": [{"src": "http://img/x.jpg"}]},
                },
            },
        },
    }
    pn = parse_dynamic(no_text_draw)
    assert pn and pn["text"] == "" and pn["images"], pn
    nt_text = plug._render_push_text(pn, "测试UP")
    assert "（无文字内容）" not in nt_text, f"纯配图动态不应显示占位符: {nt_text!r}"
    assert "测试UP" in nt_text
    # 真正无任何内容（无文字无图片）才显示占位符
    empty_dyn = {"id_str": "556", "modules": {"module_author": {"name": "测试UP"}}}
    pe = parse_dynamic(empty_dyn)
    assert pe is not None
    pe_text = plug._render_push_text(pe, "测试UP")
    assert "（无文字内容）" in pe_text, pe_text
    print("  -> 纯配图无占位符；完全无内容才显示占位符")

    print("== 旧默认模板自动迁移 ==")
    # 真机实测：config.toml 写死旧模板后，代码升级不改变已存配置，
    # 格式升级永远到不了老部署——必须在加载时识别并迁移
    OLD_T = "📢 {name} 发布了新动态\n{text}\n\n🔗 {url}"
    plug.config.settings.push_text_template = OLD_T
    plug._migrate_push_template()
    assert plug.config.settings.push_text_template == (
        "📢 {name} 发布了新动态\n{text}"
    ), "旧默认模板应被迁移为新默认"
    # 用户自定义模板（哪怕只差一个字符）必须原样保留
    custom = "自定义 {name}: {text} {url}"
    plug.config.settings.push_text_template = custom
    plug._migrate_push_template()
    assert plug.config.settings.push_text_template == custom, "自定义模板被误改！"
    plug.config.settings.push_text_template = "📢 {name} 发布了新动态\n{text}"
    print("  -> 旧默认迁移、自定义保留")

    print("== 私聊 /dyn test 也推送图片 ==")
    # 真机实测：私聊 test 原先只回文本，用户永远看不到图片效果
    SENT.clear()

    async def stub_fetch(uid: int):
        return [{
            "id_str": "777",
            "type": "DYNAMIC_TYPE_DRAW",
            "modules": {
                "module_author": {"name": "测试UP", "pub_ts": 1700000000},
                "module_dynamic": {
                    "desc": {"text": "私聊测试动态"},
                    "major": {
                        "type": "MAJOR_TYPE_DRAW",
                        "draw": {"items": [{"src": "http://img/1.jpg"}]},
                    },
                },
            },
        }]

    orig_fetch = plug._client.fetch_dynamics
    orig_dl = plug._download_image_b64
    plug._client.fetch_dynamics = stub_fetch

    async def stub_dl(url: str) -> str:
        return "ZmFrZWJhc2U2NA=="  # fakebase64

    plug._download_image_b64 = stub_dl
    r = await plug.cmd_dyn(
        matched_groups={"action": "test", "arg": "123"},
        stream_id="s-private",
    )
    plug._client.fetch_dynamics = orig_fetch
    plug._download_image_b64 = orig_dl
    kinds = [s[0] for s in SENT]
    assert "text" in kinds and "image" in kinds, f"私聊测试应发文本+图片: {SENT}"
    assert "私聊测试动态" in r[1], r[1]
    print(f"  -> 私聊 test 发送了 {kinds}")

    print("== 请求头自洽性（UA/version 不一致会被 WAF 判为脚本）==")
    import re as _re

    from bili_client import DEFAULT_HEADERS, DEFAULT_UA, WAF_STATUS

    assert (403, 412, 429, 503) == WAF_STATUS
    ua_ver = _re.search(r"Chrome/(\d+)", DEFAULT_UA)
    assert ua_ver, f"UA 中找不到 Chrome 版本: {DEFAULT_UA}"
    # WAF 会比对 UA 与 sec-ch-ua 的浏览器版本，不一致 = 伪装脚本
    assert f'v="{ua_ver.group(1)}"' in DEFAULT_HEADERS["sec-ch-ua"], (
        f"sec-ch-ua({DEFAULT_HEADERS['sec-ch-ua']}) 与 UA({DEFAULT_UA}) 版本不一致"
    )
    for key in (
        "Accept", "Accept-Language", "Origin", "Referer",
        "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
    ):
        assert DEFAULT_HEADERS.get(key), f"缺少请求头 {key}"
    print(f"  -> Chrome {ua_ver.group(1)} 与 sec-ch-ua 一致，共 {len(DEFAULT_HEADERS)} 个头")

    print("== buvid_activation 单测（死指纹激活模块）==")
    from buvid_activation import build_payload, gen_buvid_fp, gen_uuid_infoc

    u = gen_uuid_infoc()
    assert u.endswith("infoc") and u.count("-") == 4, u
    # murmur3 确定性：同输入同输出，不同输入不同输出
    assert gen_buvid_fp("abc") == gen_buvid_fp("abc")
    assert gen_buvid_fp("abc") != gen_buvid_fp("abd")
    fp = gen_buvid_fp("abc")
    assert all(c in "0123456789abcdef" for c in fp), fp
    # payload 双层 JSON 结构
    import json as _json

    outer = _json.loads(build_payload("UA-X", u))
    inner = _json.loads(outer["payload"])
    assert inner["df35"] == u and inner["3c43"]["b8ce"] == "UA-X"
    assert inner["3c43"]["adca"] == "Win32"  # 与插件 UA 的 Windows 风格一致
    print(f"  -> uuid/payload/buvid_fp({fp[:8]}...) 结构与确定性 OK")

    print("== /dyn add：拉不到动态才提示核对，且不得误伤合法长 UID ==")
    # 用打桩隔离网络抖动，专测提示逻辑本身
    orig_resolve = plug._client.resolve_up_name

    async def stub(uid: int) -> str:
        return stub.result  # type: ignore[attr-defined]

    plug._client.resolve_up_name = stub  # type: ignore[assignment]

    # ① 拉不到昵称 + 超长 -> 应提示位数与取法
    stub.result = "UID:9999999999999999"  # type: ignore[attr-defined]
    r = await plug.cmd_dyn(
        matched_groups={"action": "add", "arg": "9999999999999999"},
        stream_id="s-1", group_id="12345",
    )
    assert "未拉到" in r[1], f"未提示未拉到动态: {r[1]}"
    assert "16 位" in r[1], f"未提示位数异常: {r[1]}"
    assert "space.bilibili.com" in r[1], f"未给出 UID 取法: {r[1]}"
    await plug._subs.remove("9999999999999999", 12345)

    # ② 能拉到昵称的超长 UID（真机实测 3690973091596928 合法）-> 不该有任何警告
    stub.result = "共鸣电台_FMInfinity"  # type: ignore[attr-defined]
    r = await plug.cmd_dyn(
        matched_groups={"action": "add", "arg": "3690973091596928"},
        stream_id="s-1", group_id="12345",
    )
    assert "⚠️" not in r[1], f"合法的长 UID 被误判: {r[1]}"
    assert "共鸣电台" in r[1], r[1]
    await plug._subs.remove("3690973091596928", 12345)

    plug._client.resolve_up_name = orig_resolve  # type: ignore[assignment]
    print("  -> 无效 UID 有提示，合法长 UID 无误伤")

    print("== 真实网络: resolve_up_name + /dyn add ==")
    try:
        # 注意：B 站该接口无 Cookie 时偶发返回空，网络断言需容忍抖动
        c2 = BiliClient()
        name = await c2.resolve_up_name(517327498)
        items = await c2.fetch_dynamics(517327498)
        parsed = [p for p in (parse_dynamic(i) for i in items) if p]
        print(f"  resolved name={name!r} dynamics={len(items)} parsed={len(parsed)}")
        if parsed:
            assert parsed[0]["id"] and parsed[0]["url"].startswith("https://t.bilibili.com/")
        else:
            print("  [注意] 本次拉到 0 条（接口抖动），跳过解析断言")
        await c2.close()

        # /dyn add 必须成功，且拿不到昵称时要优雅降级为 UID:xxx
        SENT.clear()
        r = await plug.cmd_dyn(
            matched_groups={"action": "add", "arg": "517327498"},
            stream_id="s-1", group_id="12345",
        )
        print("  /dyn add ->", r[1])
        assert plug._subs.groups_of("517327498") == [12345], "订阅应写入"
        assert SENT and SENT[-1][0] == "text", "应有回复消息"
        assert "517327498" in r[1], "回复应包含 UID"
    except Exception as exc:
        print(f"  [跳过] 网络不可用: {type(exc).__name__}: {exc}")

    print("== on_unload ==")
    await plug.on_unload()
    assert not plug._running

    print()
    print("ALL SMOKE TESTS PASSED")


asyncio.run(main())
