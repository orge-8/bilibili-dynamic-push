# -*- coding: utf-8 -*-
"""跨插件只读 API 契约测试（v1.5.0 新增）。

覆盖两个 API：
- ``get_subscriptions``：只读订阅表（零新增存储）
- ``get_recent_pushes``：只读最近推送记录（v1.5.0 新增的有界环形记录）

以及记录点本身的行为：**只有真的推送成功才记**、手动 ``/dyn test`` 不记
（它推的是旧动态，记了会被消费方当成"UP 主刚发了新动态"）。
"""

import asyncio
import importlib
import json
import logging
import os
import pathlib
import sys
import time
from types import SimpleNamespace

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(_PLUGIN_DIR), str(_PLUGIN_DIR.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_MOD = importlib.import_module(_PLUGIN_DIR.name)
if not hasattr(_MOD, "create_plugin"):
    _MOD = importlib.import_module(f"{_PLUGIN_DIR.name}.plugin")

from subscription_store import PUSH_LOG_LIMIT, PushHistory, PushLog, SubscriptionStore  # noqa: E402

ALL_SUB_KEYS = {"schema_version", "reason", "active", "count", "up"}
ALL_UP_KEYS = {"uid", "name", "groups", "fixed"}
ALL_PUSH_KEYS = {"schema_version", "reason", "active", "pushes"}
ALL_ENTRY_KEYS = {"uid", "name", "dyn_type", "title", "url", "at"}


def _item(dyn_id="1234567890", title="夏天再见，逃离人间", desc="", pub_ts=None, dyn_type="DYNAMIC_TYPE_DRAW"):
    """一条最小可解析的动态载荷（OPUS 无图：解析路径不触发任何图片下载）。"""
    return {
        "id_str": dyn_id,
        "type": dyn_type,
        "modules": {
            "module_author": {"name": "测试UP", "pub_ts": int(pub_ts or time.time())},
            "module_dynamic": {
                "desc": {"text": desc},
                "major": {
                    "type": "MAJOR_TYPE_OPUS",
                    "opus": {"title": title, "summary": {"text": ""}, "pics": []},
                },
            },
        },
    }


class _Ctx:
    def __init__(self):
        self.logger = logging.getLogger("test-cross-api")


def _plugin(tmp_path):
    plug = _MOD.create_plugin()
    plug.set_plugin_config({})            # 真默认配置（无 ctx 也能构建）
    plug._ctx = _Ctx()                    # 只用到 logger
    data_dir = str(tmp_path)
    plug._subs = SubscriptionStore(data_dir)
    plug._hist = None
    plug._push_log = PushLog(data_dir)
    return plug


def _wire_push(plug, *, fail=False):
    """把 _push_dynamic 的外围协作对象换成桩，返回记录调用次数的容器。"""
    calls = {"sent": 0}

    async def _stream_id(group_id):
        return f"stream-{group_id}"

    async def _send(stream_id, text, image_b64s, *, where, sender_name):
        calls["sent"] += 1
        if fail:
            raise RuntimeError("模拟发送失败")

    plug._get_stream_id = _stream_id            # type: ignore[assignment]
    plug._send_dynamic_content = _send          # type: ignore[assignment]
    return calls


# ---------------------------------------------------------------- get_subscriptions


def test_subscriptions_empty(tmp_path):
    plug = _plugin(tmp_path)
    out = asyncio.run(plug.api_get_subscriptions())
    assert set(out) == ALL_SUB_KEYS
    assert out["schema_version"] == 1 and out["up"] == [] and out["count"] == 0
    assert out["active"] is True
    assert out["reason"] == "暂无订阅"


def test_subscriptions_not_started(tmp_path):
    plug = _MOD.create_plugin()  # 不建存储，模拟 on_load 未跑
    out = asyncio.run(plug.api_get_subscriptions())
    assert out["active"] is False and out["up"] == [] and out["count"] == 0
    assert "未完成启动" in out["reason"]


def test_subscriptions_fields_and_fixed_flag(tmp_path):
    plug = _plugin(tmp_path)
    # 命令订阅：无 name（不猜）
    asyncio.run(plug._subs.add("114514", 111))
    # 配置行订阅：fixed=True，带 name
    plug._subs.sync_from_config(["2233 => 222, 333"])
    asyncio.run(plug._subs.set_name("2233", "罗翔说刑法"))

    out = asyncio.run(plug.api_get_subscriptions())
    assert out["count"] == 2 and out["reason"] == ""
    by_uid = {u["uid"]: u for u in out["up"]}
    assert set(by_uid) == {"114514", "2233"}
    assert set(by_uid["114514"]) == ALL_UP_KEYS
    assert by_uid["114514"]["name"] == "", "name 为空必须原样返回空串，不得拿 uid 顶替"
    assert by_uid["114514"]["groups"] == [111] and by_uid["114514"]["fixed"] is False
    assert by_uid["2233"]["name"] == "罗翔说刑法"
    assert by_uid["2233"]["groups"] == [222, 333] and by_uid["2233"]["fixed"] is True
    assert all(isinstance(g, int) for u in out["up"] for g in u["groups"])


def test_subscriptions_corrupt_file_degrades(tmp_path):
    (tmp_path / "subscriptions.json").write_text("{ not json", encoding="utf-8")
    plug = _plugin(tmp_path)
    out = asyncio.run(plug.api_get_subscriptions())
    assert out["up"] == [] and out["reason"] == "暂无订阅"
    assert (tmp_path / "subscriptions.json.broken").exists(), "坏文件应被备份重建"


def test_subscriptions_readonly(tmp_path):
    plug = _plugin(tmp_path)
    asyncio.run(plug._subs.add("114514", 111))
    path = tmp_path / "subscriptions.json"
    before = path.read_bytes()
    mtime = path.stat().st_mtime
    for _ in range(3):
        asyncio.run(plug.api_get_subscriptions())
    assert path.read_bytes() == before and path.stat().st_mtime == mtime


# ---------------------------------------------------------------- get_recent_pushes


def test_recent_pushes_empty(tmp_path):
    plug = _plugin(tmp_path)
    out = asyncio.run(plug.api_get_recent_pushes())
    assert set(out) == ALL_PUSH_KEYS
    assert out["pushes"] == [] and out["active"] is True
    assert out["reason"] == "暂无推送记录"


def test_recent_pushes_not_started(tmp_path):
    plug = _MOD.create_plugin()
    out = asyncio.run(plug.api_get_recent_pushes())
    assert out["active"] is False and out["pushes"] == []
    assert "未完成启动" in out["reason"]


def test_recent_pushes_ring_drops_oldest(tmp_path):
    plug = _plugin(tmp_path)
    now = time.time()
    for i in range(PUSH_LOG_LIMIT + 5):
        asyncio.run(
            plug._push_log.append(
                uid="114514", name="测试UP", dyn_type="DYNAMIC_TYPE_DRAW",
                title=f"第 {i} 条", url=f"https://t.bilibili.com/{i}", at=now + i,
            )
        )
    assert plug._push_log.count == PUSH_LOG_LIMIT, "超出上限必须丢最旧"
    out = asyncio.run(plug.api_get_recent_pushes(limit=100))
    assert len(out["pushes"]) == PUSH_LOG_LIMIT
    titles = [p["title"] for p in out["pushes"]]
    assert titles[0] == f"第 {PUSH_LOG_LIMIT + 4} 条", "最新在前"
    assert f"第 0 条" not in titles, "最旧的应被丢"
    # 落盘同样只保留上限条
    on_disk = json.loads((tmp_path / "push_log.json").read_text(encoding="utf-8"))
    assert len(on_disk) == PUSH_LOG_LIMIT


def test_recent_pushes_limit_and_window(tmp_path):
    plug = _plugin(tmp_path)
    now = time.time()
    plug._push_log._entries = [
        {"uid": "1", "name": "A", "dyn_type": "T", "title": "老", "url": "u1", "at": now - 100000},
        {"uid": "2", "name": "B", "dyn_type": "T", "title": "中", "url": "u2", "at": now - 100},
        {"uid": "3", "name": "C", "dyn_type": "T", "title": "新", "url": "u3", "at": now - 10},
    ]
    out = asyncio.run(plug.api_get_recent_pushes(limit=2))
    assert [p["title"] for p in out["pushes"]] == ["新", "中"]

    win = asyncio.run(plug.api_get_recent_pushes(limit=10, since_seconds=50))
    assert [p["title"] for p in win["pushes"]] == ["新"]

    none_in_window = asyncio.run(plug.api_get_recent_pushes(limit=10, since_seconds=1))
    assert none_in_window["pushes"] == []
    assert none_in_window["reason"] == "时间窗内没有推送记录"


def test_recent_pushes_entry_shape_and_title_sanitized(tmp_path):
    plug = _plugin(tmp_path)
    long_title = "行一\n行二\r\n" + "字" * 300
    asyncio.run(
        plug._push_log.append(
            uid=114514, name="测试UP", dyn_type="DYNAMIC_TYPE_AV",
            title=long_title, url="https://www.bilibili.com/video/BV1xx", at=time.time(),
        )
    )
    out = asyncio.run(plug.api_get_recent_pushes())
    entry = out["pushes"][0]
    assert set(entry) == ALL_ENTRY_KEYS
    assert "\n" not in entry["title"] and "\r" not in entry["title"]
    assert len(entry["title"]) <= 120
    assert entry["uid"] == "114514", "uid 一律字符串化（msgspec/JSON 两边一致）"
    assert isinstance(entry["at"], float)
    for v in (entry["uid"], entry["name"], entry["dyn_type"], entry["title"], entry["url"]):
        assert isinstance(v, str)
    for v in out.values():
        assert v is None or isinstance(v, (bool, int, float, str, list, dict))


def test_recent_pushes_corrupt_file_degrades(tmp_path):
    (tmp_path / "push_log.json").write_text("[not, json", encoding="utf-8")
    plug = _plugin(tmp_path)
    out = asyncio.run(plug.api_get_recent_pushes())
    assert out["pushes"] == [] and out["reason"] == "暂无推送记录"
    assert (tmp_path / "push_log.json.broken").exists(), "坏文件应备份重建而非抛出"

    # 非 list 形态（例如被改写成了 dict）也不炸
    (tmp_path / "push_log.json").write_text('{"oops": 1}', encoding="utf-8")
    plug2 = _plugin(tmp_path)
    assert plug2._push_log.count == 0
    assert asyncio.run(plug2.api_get_recent_pushes())["pushes"] == []


def test_recent_pushes_readonly(tmp_path):
    plug = _plugin(tmp_path)
    asyncio.run(
        plug._push_log.append(
            uid="1", name="A", dyn_type="T", title="标题", url="u", at=time.time()
        )
    )
    path = tmp_path / "push_log.json"
    before = path.read_bytes()
    stats = path.stat()
    for _ in range(3):
        out = asyncio.run(plug.api_get_recent_pushes())
        assert len(out["pushes"]) == 1
    after = path.stat()
    assert path.read_bytes() == before, "只读 API 不得改写记录"
    assert (after.st_mtime, after.st_size) == (stats.st_mtime, stats.st_size)


def test_log_write_failure_does_not_repeat_push(tmp_path):
    """全检发现：推送记录写盘失败**不得**中断推送流水线。

    `_push_log.append` 位于 `_check_uid` 里"推送"与"推进基准 set_last"之间。
    若它的异常冒泡，基准就不推进 ⇒ 下一轮轮询会把**同一条动态再推一遍**
    （群内重复消息），而且每次轮询都重复，直到磁盘恢复。
    """
    plug = _plugin(tmp_path)
    _wire_push(plug)
    plug._hist = PushHistory(str(tmp_path))
    asyncio.run(plug._subs.add("114514", 12345))
    asyncio.run(plug._hist.set_last("114514", "100"))  # 已有基准

    plug._client = _FakeClient([_item(dyn_id="200", pub_ts=time.time())])

    async def _boom(**kwargs):
        raise OSError("磁盘只读")

    plug._push_log.append = _boom  # type: ignore[assignment]

    asyncio.run(plug._check_uid("114514"))  # 当前实现：OSError 冒泡 → 用例红
    assert plug._hist.get("114514").get("dyn_id") == "200", (
        "推送记录写盘失败后基准没有推进：下轮会重复推送同一条动态"
    )


class _FakeClient:
    def __init__(self, items):
        self._items = items

    async def fetch_dynamics(self, uid):
        return self._items


def test_subscriptions_shape_error_degrades_with_log(tmp_path, caplog):
    """可达降级：subscriptions.json 被写成 JSON 数组 → .items() 抛错，必须降级且留痕。"""
    (tmp_path / "subscriptions.json").write_text('["不是对象"]', encoding="utf-8")
    plug = _plugin(tmp_path)          # SubscriptionStore 会读成 list
    with caplog.at_level(logging.WARNING):
        out = asyncio.run(plug.api_get_subscriptions())
    assert out["up"] == [] and out["count"] == 0
    assert "不可读" in out["reason"] or "降级" in out["reason"]
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "订阅表形状异常被静默降级：日志里找不到痕迹（全检第 12 项）"
    )


# ---------------------------------------------------------------- 组件注册


def test_api_components_registered():
    """Runner 视角：两个 API 必须被 collect_components 收集到（防装饰器被插队错绑）。"""
    from maibot_sdk.components import collect_components

    plug = _MOD.create_plugin()
    apis = {c.get("name"): c for c in collect_components(plug) if c.get("type") == "API"}
    assert set(apis) == {"get_subscriptions", "get_recent_pushes"}, apis
    expected = {
        "get_subscriptions": "api_get_subscriptions",
        "get_recent_pushes": "api_get_recent_pushes",
    }
    for name, handler in expected.items():
        meta = apis[name].get("metadata") or {}
        assert str(meta.get("version")) == "1", (name, meta)
        assert meta.get("public") is True, (name, meta)
        assert meta.get("handler_name") == handler, (name, meta)


# ---------------------------------------------------------------- 记录点行为


def test_push_success_records_entry(tmp_path):
    """自动推送成功 → 落一条记录（uid/name/dyn_type/title/url/at 全在）。"""
    plug = _plugin(tmp_path)
    _wire_push(plug)
    asyncio.run(plug._push_dynamic("114514", _item(), [12345]))

    out = asyncio.run(plug.api_get_recent_pushes())
    assert out["reason"] == "" and len(out["pushes"]) == 1
    entry = out["pushes"][0]
    assert entry["uid"] == "114514"
    assert entry["name"] == "测试UP"
    assert entry["dyn_type"] == "DYNAMIC_TYPE_DRAW"
    assert entry["title"] == "夏天再见，逃离人间"
    assert entry["url"] == "https://t.bilibili.com/1234567890"
    assert entry["at"] > 0


def test_push_title_falls_back_to_text_without_video(tmp_path):
    """非视频动态：标题取正文（换行压成空格）。"""
    plug = _plugin(tmp_path)
    _wire_push(plug)
    item = _item(title="", desc="今天更新了\n第二行")
    asyncio.run(plug._push_dynamic("114514", item, [12345]))
    entry = asyncio.run(plug.api_get_recent_pushes())["pushes"][0]
    assert entry["title"] == "今天更新了 第二行"


def test_push_manual_test_not_recorded(tmp_path):
    """`/dyn test` 推的是旧动态：record=False 时不得产生记录（防假世界事件）。"""
    plug = _plugin(tmp_path)
    calls = _wire_push(plug)
    asyncio.run(plug._push_dynamic("114514", _item(), [12345], record=False))
    assert calls["sent"] == 1, "手动推送本身仍要真的发出去"
    out = asyncio.run(plug.api_get_recent_pushes())
    assert out["pushes"] == [] and out["reason"] == "暂无推送记录"


def test_push_failure_not_recorded(tmp_path):
    """一个群都没发成功 → 不记（"最近推了什么"必须是真推出去的）。"""
    plug = _plugin(tmp_path)
    _wire_push(plug, fail=True)
    asyncio.run(plug._push_dynamic("114514", _item(), [12345]))
    assert asyncio.run(plug.api_get_recent_pushes())["pushes"] == []


def test_push_no_groups_not_recorded(tmp_path):
    plug = _plugin(tmp_path)
    _wire_push(plug)
    asyncio.run(plug._push_dynamic("114514", _item(), []))
    assert asyncio.run(plug.api_get_recent_pushes())["pushes"] == []
