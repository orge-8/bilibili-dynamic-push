"""订阅与推送历史的持久化存储（纯 JSON 文件层，不依赖 maibot_sdk）。

数据文件由调用方指定（插件运行时指向 self.ctx.paths.data_dir）。
"""

import asyncio
import json
import os
import time
from typing import Any

SUBS_FILE = "subscriptions.json"
HISTORY_FILE = "push_history.json"
#: 最近推送记录（跨插件只读 API ``get_recent_pushes`` 的数据源）
PUSH_LOG_FILE = "push_log.json"
#: 环形上限：够调用方回溯一天，又不让文件无限增长
PUSH_LOG_LIMIT = 50
#: 标题是外部文本：入库前截断 + 去换行，避免把长正文塞进跨插件 API
TITLE_MAX_CHARS = 120


def _clean_title(text: Any, limit: int = TITLE_MAX_CHARS) -> str:
    """外部文本入库前归一化：真实换行与字面 ``\\n`` 都压成空格，超长截断。"""
    value = str(text or "")
    value = value.replace("\\n", " ").replace("\r", " ").replace("\n", " ")
    return " ".join(value.split())[:limit]


def _atomic_write(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _load_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # 损坏时备份重建，避免全部订阅/历史丢失后漏推刷屏
        try:
            os.replace(path, path + ".broken")
        except OSError:
            pass
        return default


class SubscriptionStore:
    """订阅：{uid: {"groups": [群号], "name": UP主名}}；命令添加与配置添加合并管理。"""

    def __init__(self, data_dir: str):
        self._dir = data_dir
        os.makedirs(self._dir, exist_ok=True)
        self._path = os.path.join(self._dir, SUBS_FILE)
        self._lock = asyncio.Lock()
        self._subs: dict[str, dict[str, Any]] = _load_json(self._path, {})

    @property
    def data(self) -> dict[str, dict[str, Any]]:
        return self._subs

    def uid_list(self) -> list[str]:
        return list(self._subs.keys())

    def groups_of(self, uid: str) -> list[int]:
        entry = self._subs.get(str(uid)) or {}
        return [int(g) for g in entry.get("groups", [])]

    def get_name(self, uid: str) -> str:
        return (self._subs.get(str(uid)) or {}).get("name") or ""

    def merged_groups(self) -> dict[str, list[int]]:
        return {uid: [int(g) for g in e.get("groups", [])] for uid, e in self._subs.items()}

    def _save_sync(self) -> None:
        _atomic_write(self._path, self._subs)

    async def save(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_sync)

    async def add(self, uid: str, group_id: int, name: str = "") -> bool:
        """给群订阅 UP 主。返回是否为新订阅关系。"""
        uid = str(uid)
        async with self._lock:
            entry = self._subs.setdefault(uid, {"groups": [], "name": name})
            if name:
                entry["name"] = name
            if group_id in entry["groups"]:
                return False
            entry["groups"].append(group_id)
            await asyncio.to_thread(self._save_sync)
            return True

    async def remove(self, uid: str, group_id: int) -> bool:
        """移除某群对 UP 主的订阅。返回是否存在该关系。"""
        uid = str(uid)
        async with self._lock:
            entry = self._subs.get(uid)
            if not entry or group_id not in entry.get("groups", []):
                return False
            entry["groups"].remove(group_id)
            if not entry["groups"]:
                self._subs.pop(uid, None)
            await asyncio.to_thread(self._save_sync)
            return True

    def sync_from_config(self, lines: list[str]) -> None:
        """把配置里的 "UID => 群1, 群2" 行合并进存储（配置行不可被命令移除，标记 fixed）。"""
        import re

        for raw in lines or []:
            if not isinstance(raw, str):
                continue
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = re.split(r"\s*(?:=>|->|:|：|\|)\s*", line, maxsplit=1)
            if len(parts) != 2:
                parts = line.split(None, 1)
                if len(parts) != 2:
                    continue
            uid_str = parts[0].strip()
            if not uid_str.isdigit():
                continue
            groups = [g for g in re.split(r"[,，\s]+", parts[1].strip()) if g.isdigit()]
            if not groups:
                continue
            entry = self._subs.setdefault(uid_str, {"groups": [], "name": ""})
            entry["fixed"] = True
            for g in groups:
                if int(g) not in entry["groups"]:
                    entry["groups"].append(int(g))

    def is_fixed(self, uid: str) -> bool:
        return bool((self._subs.get(str(uid)) or {}).get("fixed"))

    async def set_name(self, uid: str, name: str) -> None:
        """记录 UP 主昵称（不动群组关系）。"""
        if not name:
            return
        async with self._lock:
            entry = self._subs.setdefault(str(uid), {"groups": [], "name": ""})
            if entry.get("name") == name:
                return
            entry["name"] = name
            await asyncio.to_thread(self._save_sync)


class PushHistory:
    """每个 UID 记录最后推送的动态 ID（去重基准）。"""

    def __init__(self, data_dir: str):
        self._dir = data_dir
        os.makedirs(self._dir, exist_ok=True)
        self._path = os.path.join(self._dir, HISTORY_FILE)
        self._lock = asyncio.Lock()
        self._hist: dict[str, dict[str, Any]] = _load_json(self._path, {})

    def get(self, uid: str) -> dict[str, Any]:
        entry = self._hist.get(str(uid))
        if isinstance(entry, dict):
            return entry
        # 兼容旧格式（纯 ID 字符串）
        if isinstance(entry, str):
            return {"dyn_id": entry}
        return {}

    async def set_last(self, uid: str, dyn_id: str, top_id: str = "") -> None:
        async with self._lock:
            entry = self._hist.get(str(uid)) or {}
            if not isinstance(entry, dict):
                entry = {}
            entry["dyn_id"] = str(dyn_id)
            if top_id:
                entry["top_dyn_id"] = str(top_id)
            self._hist[str(uid)] = entry
            await asyncio.to_thread(_atomic_write, self._path, self._hist)


class PushLog:
    """最近**推送成功**的动态（有界环形，只记结构性元数据，供跨插件只读查询）。

    为什么要在 ``PushHistory`` 之外另记一条：``push_history.json`` 只存
    ``dyn_id`` / ``top_dyn_id``（去重基准），既没有标题也没有链接——想回答
    "最近推了什么"必须在推送成功那一刻顺手记下来。

    与 ``PushHistory`` 同目录、复用 ``_atomic_write``；坏文件沿用 ``_load_json``
    的备份重建语义（重命名成 ``.broken`` 后当空档案），不让畸形 JSON 阻断推送链路。
    只记 ``uid / name / dyn_type / title / url / at``，标题经 ``_clean_title``
    截断去换行——聊天原文永不入库。
    """

    def __init__(self, data_dir: str, limit: int = PUSH_LOG_LIMIT):
        self._dir = data_dir
        os.makedirs(self._dir, exist_ok=True)
        self._path = os.path.join(self._dir, PUSH_LOG_FILE)
        self._lock = asyncio.Lock()
        self._limit = max(1, int(limit))
        raw = _load_json(self._path, [])
        entries = raw if isinstance(raw, list) else []
        self._entries: list[dict[str, Any]] = [e for e in entries if isinstance(e, dict)][
            -self._limit:
        ]

    @property
    def path(self) -> str:
        return self._path

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def count(self) -> int:
        """当前记录条数（供 API 区分"从未推送过"与"时间窗内没有"）。"""
        return len(self._entries)

    def recent(self, limit: int = 10, since_seconds: float = 0) -> list[dict[str, Any]]:
        """只读筛选最近推送（最新在前）。

        不改状态、不写盘、不抛异常——供跨插件 API 直接调用。畸形记录
        （不是 dict 或时间戳不可用）被跳过；``since_seconds > 0`` 时只回
        时间窗内的记录；``limit`` 钳到 1..100。
        """
        try:
            cap = int(limit)
        except (TypeError, ValueError):
            cap = 10
        cap = min(max(cap, 1), 100)
        try:
            window = float(since_seconds)
        except (TypeError, ValueError):
            window = 0.0
        cutoff = (time.time() - window) if window > 0 else 0.0

        out: list[dict[str, Any]] = []
        for raw in reversed(self._entries):
            if not isinstance(raw, dict):
                continue
            try:
                at = float(raw.get("at"))
            except (TypeError, ValueError):
                continue
            if cutoff and at < cutoff:
                continue
            out.append(
                {
                    "uid": str(raw.get("uid") or ""),
                    "name": str(raw.get("name") or ""),
                    "dyn_type": str(raw.get("dyn_type") or ""),
                    "title": _clean_title(raw.get("title")),
                    "url": str(raw.get("url") or ""),
                    "at": at,
                }
            )
            if len(out) >= cap:
                break
        return out

    async def append(
        self,
        *,
        uid: str,
        name: str,
        dyn_type: str,
        title: str,
        url: str,
        at: float | None = None,
    ) -> None:
        """追加一条推送记录；超出上限丢最旧，原子写盘。"""
        entry = {
            "uid": str(uid),
            "name": str(name or ""),
            "dyn_type": str(dyn_type or ""),
            "title": _clean_title(title),
            "url": str(url or ""),
            "at": float(at) if at is not None else time.time(),
        }
        async with self._lock:
            self._entries.append(entry)
            if len(self._entries) > self._limit:
                del self._entries[: len(self._entries) - self._limit]
            await asyncio.to_thread(_atomic_write, self._path, self._entries)
