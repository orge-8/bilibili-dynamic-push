"""订阅与推送历史的持久化存储（纯 JSON 文件层，不依赖 maibot_sdk）。

数据文件由调用方指定（插件运行时指向 self.ctx.paths.data_dir）。
"""

import asyncio
import json
import os
from typing import Any

SUBS_FILE = "subscriptions.json"
HISTORY_FILE = "push_history.json"


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
