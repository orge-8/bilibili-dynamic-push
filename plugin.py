"""B 站 UP 主动态自动推送插件。

参考 beimyu504/beiyu_bilibili-dynamic-push 的订阅/去重/置顶识别设计，
改为 web 端 polymer API 直连（不依赖 bilibili-api-python），
推送走 SDK 能力代理（chat.open_session + send.text / send.image）。
"""

import asyncio
import base64
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase

from bili_client import (
    RISK_CODES,
    WAF_STATUS,
    BiliApiError,
    BiliClient,
    is_forward_dynamic,
    is_live_rcmd,
    is_top_dynamic,
    parse_dynamic,
)
from subscription_store import PushHistory, SubscriptionStore

# 风控退避上限（秒）。IP 被风控时指数拉长轮询间隔，但不超过 30 分钟，
# 避免风控解除后长时间收不到动态。
_MAX_BACKOFF = 1800.0

# 推送模板新旧默认值。v1.1.1 起默认不再附带动态链接（图文直接推图片和文字），
# 但旧版部署的 config.toml 已把旧模板写死在配置里，代码升级不会改变已存配置——
# 必须在加载时识别"未改过的旧默认值"并自动迁移，否则格式升级永远到不了老部署。
_PUSH_TEMPLATE_OLD = "📢 {name} 发布了新动态\n{text}\n\n🔗 {url}"
_PUSH_TEMPLATE_NEW = "📢 {name} 发布了新动态\n{text}"


class PluginSection(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "satellite"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用自动推送监控")
    config_version: str = Field(default="1.0.0", description="配置版本")


class BiliSection(PluginConfigBase):
    """B 站访问凭证。"""

    __ui_label__ = "B站凭证"
    __ui_icon__ = "key"
    __ui_order__ = 1

    cookie: str = Field(
        default="",
        description="B 站 Cookie（可选）。浏览器登录 B 站后复制整行 Cookie 粘贴至此，可显著降低风控概率。",
    )
    verify_ssl: bool = Field(
        default=True,
        description="是否校验 SSL 证书。仅在代理环境导致证书校验失败时关闭。",
    )


class SettingsSection(PluginConfigBase):
    """运行参数。"""

    __ui_label__ = "运行设置"
    __ui_icon__ = "settings"
    __ui_order__ = 2

    poll_interval: int = Field(
        default=180,
        description="轮询基准间隔（秒），建议不低于 120。",
    )
    poll_jitter: int = Field(
        default=30,
        description="轮询抖动（秒），实际间隔 = 基准 ± 抖动，防风控。",
    )
    max_images: int = Field(
        default=9,
        description="单条动态最多推送的图片数量（B 站上限 9）。v1.2.0 起多图合并为一条消息推送，无刷屏顾虑，默认放开到上限。",
    )
    hybrid_merge_threshold: int = Field(
        default=2,
        description=(
            "图片合并阈值：动态图片数超过该值时，文字+图片合并为一条"
            "合并转发卡片发送（优先 send.forward，协议端不支持时自动降级"
            "混合消息，再降级逐条发送）；不超过时保持文字一条、图片逐张发送。"
            "设为 0 表示始终合并；设为 999 表示从不合并。"
        ),
    )
    max_dynamic_age: int = Field(
        default=3600,
        description="动态最大有效时长（秒），超过不推送（防止重启后旧动态刷屏）。",
    )
    skip_forward: bool = Field(
        default=False,
        description="是否过滤转发类型动态（不推送）。",
    )
    ignore_lottery: bool = Field(
        default=True,
        description="是否丢弃开奖类动态。",
    )
    push_text_template: str = Field(
        default=_PUSH_TEMPLATE_NEW,
        description=(
            "推送文本模板，可用变量：{name} {text} {url} {time}。"
            "默认不带动态链接（图文直接推图片和文字）；"
            "视频投稿动态会自动在末尾附上链接。想恢复链接可在模板里加 {url}。"
        ),
    )


class AdminSection(PluginConfigBase):
    """权限控制。"""

    __ui_label__ = "权限"
    __ui_icon__ = "shield"
    __ui_order__ = 3

    admin_qqs: list[str] = Field(
        default_factory=list,
        description="管理员 QQ 号列表（可使用 /dyn 系列命令）。",
    )
    allow_subscribe_in_private: bool = Field(
        default=False,
        description="是否允许在私聊中使用订阅命令（订阅目标是发起者所在群时仍需群号）。",
    )


class SubscriptionsSection(PluginConfigBase):
    """订阅列表。"""

    __ui_label__ = "订阅"
    __ui_icon__ = "list"
    __ui_order__ = 4

    users: list[str] = Field(
        default_factory=list,
        description='每行一条："UID => 群号1, 群号2"。也可进群用 /dyn add <UID> 添加。',
    )


class BiliPushConfig(PluginConfigBase):
    """B 站动态推送插件配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    bili: BiliSection = Field(default_factory=BiliSection)
    settings: SettingsSection = Field(default_factory=SettingsSection)
    admin: AdminSection = Field(default_factory=AdminSection)
    subscriptions: SubscriptionsSection = Field(default_factory=SubscriptionsSection)


class BiliPushPlugin(MaiBotPlugin):
    """B 站动态自动推送。"""

    config_model = BiliPushConfig

    # RPC 帧大小上限（msgpack 序列化，真机报错实测 16777216 = 16MB）。
    # 合并消息的 segments（含全部图片 base64）必须小于该值才能走 send.hybrid；
    # 安全线取 11MB 留足元数据与协议开销余量。超限直接逐条发送，不浪费尝试。
    HYBRID_FRAME_SAFE_BYTES = 11 * 1024 * 1024

    def __init__(self):
        super().__init__()
        self._client: BiliClient | None = None
        self._subs: SubscriptionStore | None = None
        self._hist: PushHistory | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_poll_ts = 0.0
        self._group_stream_cache: dict[str, str] = {}
        # 连续被风控的轮次，驱动指数退避（见 _monitor_loop）
        self._consecutive_failures = 0

    # ---------- 生命周期 ----------

    def _migrate_push_template(self) -> None:
        """旧版默认模板（带链接）自动迁移为新默认（图文直推）。

        只动"未改过的旧默认值"；用户自定义过的模板（哪怕只差一个字符）
        一律保留不动。真机日志实测：config.toml 里写死的旧模板会在代码
        升级后继续生效，导致格式改进永远到不了已部署机器。
        """
        tmpl = self.config.settings.push_text_template
        if tmpl == _PUSH_TEMPLATE_OLD:
            self.config.settings.push_text_template = _PUSH_TEMPLATE_NEW
            self.ctx.logger.info(
                "检测到旧版默认推送模板（带链接），已自动切换为图文直推格式；"
                "如需恢复链接，可在配置 push_text_template 中加回 {url}"
            )

    async def on_load(self) -> None:
        cfg = self.config
        self._migrate_push_template()
        data_dir = str(self.ctx.paths.data_dir)
        self._subs = SubscriptionStore(data_dir)
        self._hist = PushHistory(data_dir)
        self._subs.sync_from_config(cfg.subscriptions.users)
        await self._subs.save()
        self._client = BiliClient(
            cookie=cfg.bili.cookie,
            verify=cfg.bili.verify_ssl,
        )
        if cfg.plugin.enabled:
            self._start_loop()
        self.ctx.logger.info(
            "B站动态推送插件已加载，当前订阅 %d 个 UP 主", len(self._subs.uid_list())
        )

    async def on_unload(self) -> None:
        self._stop_loop()
        if self._client is not None:
            await self._client.close()
        self.ctx.logger.info("B站动态推送插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del scope, config_data, version
        cfg = self.config
        self._migrate_push_template()
        if self._subs is not None:
            self._subs.sync_from_config(cfg.subscriptions.users)
            await self._subs.save()
        if self._client is not None:
            self._client.set_cookie(cfg.bili.cookie)
        if cfg.plugin.enabled:
            self._start_loop()
        else:
            self._stop_loop()

    # ---------- 监控循环 ----------

    def _start_loop(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())

    def _stop_loop(self) -> None:
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _monitor_loop(self) -> None:
        # 启动先等一小会儿，让 Host 完成初始化
        await asyncio.sleep(10)
        while self._running:
            failures = 0
            try:
                failures = await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ctx.logger.error("轮询异常: %s", exc, exc_info=True)
                failures = 1

            if failures:
                self._consecutive_failures += 1
            elif self._consecutive_failures:
                self.ctx.logger.info("接口已恢复，退出风控退避")
                self._consecutive_failures = 0

            cfg = self.config.settings
            interval = max(60, cfg.poll_interval)
            jitter = max(0, cfg.poll_jitter)
            delay = interval + random.uniform(-jitter, jitter)

            # 风控熔断：IP 被风控后若仍按固定间隔猛撞，只会让风控持续加重
            # （表现为 412 一直不消）。指数退避给 B 站侧计数器留出衰减时间。
            if self._consecutive_failures > 0:
                extra = min(
                    _MAX_BACKOFF,
                    interval * (2 ** (self._consecutive_failures - 1)),
                )
                delay += extra
                self.ctx.logger.warning(
                    "接口连续失败 %d 次，下次轮询延后 %.0f 秒。"
                    "持续被风控建议：填写 bili.cookie 或调大 poll_interval",
                    self._consecutive_failures,
                    delay,
                )
            await asyncio.sleep(delay)

    async def _poll_once(self) -> int:
        """轮询一轮，返回被风控命中的 UID 数（供外层退避判断）。"""
        assert self._subs is not None and self._client is not None
        self._last_poll_ts = time.time()
        blocked = 0
        for uid in self._subs.uid_list():
            if not self._running:
                return blocked
            try:
                await self._check_uid(uid)
            except BiliApiError as exc:
                self.ctx.logger.warning("UID %s 接口错误: %s", uid, exc)
                # 只有风控类错误才计入退避；单个 UID 的业务错误不该拖累全局。
                # 注意 -412(request was banned) 不在 WAF_STATUS 里，必须单独算进来，
                # 否则被禁时仍按原间隔轮询，永远好不了。
                if exc.code in WAF_STATUS or exc.code in RISK_CODES:
                    blocked += 1
            except Exception as exc:
                self.ctx.logger.error("UID %s 检查失败: %s", uid, exc, exc_info=True)
            # 每个 UID 之间留随机间隔，降低风控风险
            await asyncio.sleep(random.uniform(2.0, 5.0))
        return blocked

    async def _check_uid(self, uid: str) -> None:
        assert self._subs is not None and self._client is not None and self._hist is not None
        cfg = self.config.settings
        items = await self._client.fetch_dynamics(int(uid))
        if not items:
            return

        # 分类：置顶单独记，直播推荐跳过，转发按配置跳过
        top_item = None
        normal: list[dict[str, Any]] = []
        for it in items:
            if is_live_rcmd(it):
                continue
            if cfg.skip_forward and is_forward_dynamic(it):
                continue
            if is_top_dynamic(it) and top_item is None:
                top_item = it
            else:
                normal.append(it)

        hist = self._hist.get(uid)
        last_id = str(hist.get("dyn_id") or "")
        last_top = str(hist.get("top_dyn_id") or "")

        # 首次见到该 UID：只记基准，不推送
        if not last_id:
            if normal:
                new_base = str(max(int(it["id_str"]) for it in normal))
            else:
                new_base = str(items[0].get("id_str") or "")
            if top_item:
                await self._hist.set_last(uid, new_base, str(top_item.get("id_str") or ""))
            else:
                await self._hist.set_last(uid, new_base)
            self.ctx.logger.info("UID %s 首次初始化，基准动态 %s", uid, new_base)
            return

        # 找比基准新的普通动态
        new_items = [it for it in normal if int(it.get("id_str") or 0) > int(last_id)]
        if top_item:
            top_id_str = str(top_item.get("id_str") or "")
            if top_id_str != last_top and int(top_id_str or 0) > int(last_id):
                new_items.append(top_item)
                self.ctx.logger.info("UID %s 检测到新置顶动态 %s", uid, top_id_str)

        # 更新置顶记录
        if top_item:
            await self._hist.set_last(
                uid, str(hist.get("dyn_id") or last_id), str(top_item.get("id_str") or "")
            )

        if not new_items:
            return

        newest = max(new_items, key=lambda it: int(it.get("id_str") or 0))

        # 过期过滤：太旧的不推
        pub_ts = 0
        try:
            pub_ts = int((newest.get("modules", {}).get("module_author") or {}).get("pub_ts") or 0)
        except (ValueError, TypeError):
            pub_ts = 0
        if pub_ts > 0 and (time.time() - pub_ts) > cfg.max_dynamic_age:
            self.ctx.logger.info(
                "UID %s 新动态 %s 已过期（%d 秒前），只更新基准不推送",
                uid, newest.get("id_str"), int(time.time() - pub_ts),
            )
        else:
            groups = self._subs.groups_of(uid)
            if groups:
                await self._push_dynamic(uid, newest, groups)

        # 推进基准（置顶不推进普通基准）
        normal_new = [it for it in new_items if not is_top_dynamic(it)]
        if normal_new:
            max_new = str(max(int(it.get("id_str") or 0) for it in normal_new))
            if int(max_new) > int(last_id):
                await self._hist.set_last(
                    uid, max_new,
                    str(top_item.get("id_str") or "") if top_item else "",
                )

    # ---------- 推送 ----------

    async def _get_stream_id(self, group_id: int) -> str:
        """群号 -> stream_id（带缓存）。"""
        key = str(group_id)
        cached = self._group_stream_cache.get(key)
        if cached:
            return cached
        stream = await self.ctx.chat.open_session(
            platform="qq", chat_type="group", group_id=key
        )
        stream_id = ""
        if isinstance(stream, dict):
            stream_id = str(stream.get("stream_id") or stream.get("id") or "")
        elif stream is not None:
            stream_id = str(stream)
        if stream_id:
            self._group_stream_cache[key] = stream_id
        return stream_id

    async def _send_group_text(self, group_id: int, text: str) -> bool:
        try:
            stream_id = await self._get_stream_id(group_id)
            if not stream_id:
                self.ctx.logger.error("群 %s 无法获取 stream_id", group_id)
                return False
            return bool(await self.ctx.send.text(text, stream_id))
        except Exception as exc:
            self.ctx.logger.error("群 %s 发送文本失败: %s", group_id, exc, exc_info=True)
            return False

    async def _download_image_b64(self, url: str) -> str:
        assert self._client is not None
        client = await self._client._ensure_client()
        resp = await client.get(url)
        resp.raise_for_status()
        return base64.b64encode(resp.content).decode("ascii")

    # 合并前的图片压缩参数：B 站动态图多为长图/高清图（单张 base64 可达 4MB），
    # 9 张必超 RPC 帧 16MB 上限。QQ 聊天窗口显示宽度有限，压到 1080px 长边
    # + JPEG q85 在手机上几乎无损观感，体积却能缩一个数量级。
    COMPRESS_MAX_EDGE = 1080
    COMPRESS_JPEG_QUALITY = 85

    async def _compress_images_for_frame(
        self, image_b64s: list[str], *, where: str
    ) -> list[str] | None:
        """把图片列表压缩到合并安全线内；失败返回 None（调用方退回逐条）。

        策略：Pillow 重编码为 JPEG（长边限 COMPRESS_MAX_EDGE、质量
        COMPRESS_JPEG_QUALITY）。PNG 透明图会丢 alpha（白底合成）——
        对动态配图场景可接受。压缩在普通线程池跑，不阻塞事件循环。
        """
        try:
            import io

            from PIL import Image
        except ImportError:
            self.ctx.logger.warning(
                "%s 未安装 Pillow（pip install Pillow），无法压缩图片，"
                "超过安全线的多图将逐条发送", where,
            )
            return None

        def _encode_one(raw_b64: str) -> str:
            img = Image.open(io.BytesIO(base64.b64decode(raw_b64)))
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGBA")
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            w, h = img.size
            edge = max(w, h)
            if edge > self.COMPRESS_MAX_EDGE:
                scale = self.COMPRESS_MAX_EDGE / edge
                img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=self.COMPRESS_JPEG_QUALITY, optimize=True)
            return base64.b64encode(buf.getvalue()).decode("ascii")

        def _compress_all() -> list[str]:
            return [_encode_one(b) for b in image_b64s]

        try:
            compressed = await asyncio.to_thread(_compress_all)
        except Exception as exc:
            self.ctx.logger.warning("%s 图片压缩失败，放弃合并: %s", where, exc)
            return None
        new_total = sum(len(b) for b in compressed)
        if new_total > self.HYBRID_FRAME_SAFE_BYTES:
            self.ctx.logger.info(
                "%s 压缩后仍 %dMB（原图 %.1fMB），继续用压缩图尝试合并",
                where,
                new_total // 1024 // 1024,
                sum(len(b) for b in image_b64s) / 1024 / 1024,
            )
        self.ctx.logger.info(
            "%s 已压缩 %d 张图：%.1fMB -> %.1fMB",
            where,
            len(compressed),
            sum(len(b) for b in image_b64s) / 1024 / 1024,
            new_total / 1024 / 1024,
        )
        return compressed

    async def _send_dynamic_content(
        self,
        stream_id: str,
        text: str,
        image_b64s: list[str],
        *,
        where: str,
        sender_name: str = "",
    ) -> None:
        """把一条动态的文字+图片发到指定消息流。

        图片数超过 hybrid_merge_threshold 时，合并为一条消息发送，降级链：
        1. send.forward —— QQ 合并转发卡片（"群聊的聊天记录"样式），
           文字一条节点 + 每张图一条节点，观感最接近手动合并转发。
        2. send.hybrid —— 单条图文混合消息（协议端不支持转发卡片时）。
        3. 逐条发送 —— 文字一条，图片逐张（间隔 0.5s）。

        帧大小防线：RPC 帧上限 16MB（msgpack，真机实测 E_UNKNOWN 帧超限），
        合并载荷（含全部图片 base64）超过 11MB 安全线时先尝试压缩图片
        （Pillow → JPEG 重编码，v1.3.1），压进安全线就继续走合并；
        压不进（图太多或压缩不可用）才逐条发送。
        """
        if not text and not image_b64s:
            return
        threshold = self.config.settings.hybrid_merge_threshold
        total_b64 = sum(len(b) for b in image_b64s) + len(text)
        should_merge = threshold >= 0 and len(image_b64s) > threshold
        if should_merge and total_b64 > self.HYBRID_FRAME_SAFE_BYTES:
            # 超安全线：先压缩再合并（真机实测 hanser 9 图 35MB 逐条刷屏，
            # 压缩到安全线内即可保住合并转发卡片的体验）
            compressed = await self._compress_images_for_frame(image_b64s, where=where)
            if compressed is not None:
                image_b64s = compressed
                total_b64 = sum(len(b) for b in image_b64s) + len(text)
        should_merge = should_merge and total_b64 <= self.HYBRID_FRAME_SAFE_BYTES
        if should_merge:
            # --- 第一优先：send.forward 合并转发卡片 ---
            # Host 端（_cap_send_forward）节点格式：
            #   {"nickname"/"user_nickname": str, "user_id": str,
            #    "message_id": str(缺省自动生成), "segments": [{"type","content"}]}
            # text 段取 content/data，image 段的 content 自动进 binary_data_base64
            forward_nodes: list[dict[str, Any]] = []
            if text:
                forward_nodes.append({
                    "user_id": "",
                    "nickname": sender_name or "B站动态",
                    "segments": [{"type": "text", "content": text}],
                })
            for b64 in image_b64s:
                forward_nodes.append({
                    "user_id": "",
                    "nickname": sender_name or "B站动态",
                    "segments": [{"type": "image", "content": b64}],
                })
            try:
                await self.ctx.send.forward(forward_nodes, stream_id)
                return
            except Exception as exc:
                self.ctx.logger.warning(
                    "%s 合并转发卡片发送失败，尝试混合消息: %s", where, exc
                )
            # --- 第二优先：send.hybrid 单条图文混合 ---
            segments: list[dict[str, str]] = []
            if text:
                segments.append({"type": "text", "content": text})
            for b64 in image_b64s:
                segments.append({"type": "image", "content": b64})
            try:
                await self.ctx.send.hybrid(segments, stream_id)
                return
            except Exception as exc:
                self.ctx.logger.warning(
                    "%s 合并消息发送失败，降级为逐条发送: %s", where, exc
                )
        elif threshold >= 0 and len(image_b64s) > threshold:
            self.ctx.logger.warning(
                "%s 图片压缩后仍超过合并安全线（%d 张图），逐条发送",
                where,
                len(image_b64s),
            )
        # 逐条发送（未达合并阈值、体积超限，或合并降级到底）
        if text:
            await self.ctx.send.text(text, stream_id)
        for b64 in image_b64s:
            try:
                await self.ctx.send.image(b64, stream_id)
                await asyncio.sleep(0.5)
            except Exception as exc:
                self.ctx.logger.error("%s 发送图片失败: %s", where, exc)

    def _render_push_text(self, parsed: dict[str, Any], name: str) -> str:
        """按模板渲染推送文本。

        默认模板不带动态链接（图文/转发直接推文字+图片）。
        视频投稿例外：QQ 里没法直接播视频，只有封面图，
        自动在末尾附上链接（模板里已写 {url} 时不重复加）。
        无文字但带图片的动态（纯配图）不显示"（无文字内容）"占位——
        图片本身就是内容，占位符只会徒增噪音。
        """
        cfg = self.config.settings
        time_str = ""
        if parsed["pub_ts"]:
            time_str = datetime.fromtimestamp(parsed["pub_ts"]).strftime("%Y-%m-%d %H:%M")
        tag = "（置顶）" if parsed["top"] else ""
        if parsed["text"]:
            body = parsed["text"]
        elif parsed["images"]:
            body = ""
        else:
            body = "（无文字内容）"
        text = cfg.push_text_template.format(
            name=f"{name}{tag}",
            text=body,
            url=parsed["url"],
            time=time_str,
        )
        if parsed["video"] and "{url}" not in cfg.push_text_template:
            text += f"\n🔗 {parsed['url']}"
        return text

    async def _push_dynamic(self, uid: str, item: dict[str, Any], groups: list[int]) -> None:
        assert self._subs is not None
        parsed = parse_dynamic(item)
        if parsed is None:
            self.ctx.logger.info("UID %s 动态 %s 解析为空或被过滤", uid, item.get("id_str"))
            return

        # UP 主显示名（订阅里缓存的名字优先）
        name = self._subs.get_name(uid) or parsed["author"]
        if not self._subs.get_name(uid):
            await self._subs.set_name(uid, parsed["author"])

        text = self._render_push_text(parsed, name)

        image_b64s: list[str] = []
        for img_url in parsed["images"][: self.config.settings.max_images]:
            try:
                image_b64s.append(await self._download_image_b64(img_url))
            except Exception as exc:
                self.ctx.logger.warning("图片下载失败 %s: %s", img_url, exc)

        for gid in groups:
            stream_id = await self._get_stream_id(gid)
            if not stream_id:
                self.ctx.logger.error("群 %s 无法获取 stream_id", gid)
                continue
            try:
                await self._send_dynamic_content(
                    stream_id, text, image_b64s,
                    where=f"群 {gid}", sender_name=name,
                )
            except Exception as exc:
                self.ctx.logger.error("群 %s 发送文本失败: %s", gid, exc, exc_info=True)
            await asyncio.sleep(1.0)

        self.ctx.logger.info(
            "已推送 UID %s 动态 %s 到 %d 个群", uid, parsed["id"], len(groups)
        )

    # ---------- 命令 ----------

    def _extract_stream_and_group(self, kwargs: dict[str, Any]) -> tuple[str, str]:
        """从命令载荷提取 (stream_id, group_id)。group_id 为空表示私聊。"""
        stream_id = ""
        for key in ("stream_id", "chat_id", "session_id", "stream"):
            if kwargs.get(key):
                stream_id = str(kwargs[key])
                break
        if not stream_id and isinstance(kwargs.get("message"), dict):
            stream_id = str(kwargs["message"].get("stream_id") or "")

        group_id = ""
        base_info = kwargs.get("message_base_info") or {}
        if isinstance(base_info, dict) and base_info.get("group_id"):
            group_id = str(base_info["group_id"])
        if not group_id and kwargs.get("group_id"):
            group_id = str(kwargs["group_id"])
        if not group_id:
            raw = kwargs.get("raw_event")
            if isinstance(raw, dict) and raw.get("group_id"):
                group_id = str(raw["group_id"])
        # 群号也可出现在 stream_id 形如 "group_<id>" 的形式里
        if not group_id and stream_id.startswith("group_"):
            group_id = stream_id[len("group_"):]
        return stream_id, group_id

    def _is_admin(self, kwargs: dict[str, Any]) -> bool:
        cfg = self.config.admin
        admins = [str(a) for a in cfg.admin_qqs]
        if not admins:
            return True  # 未配置管理员时放行（个人部署便利）
        base_info = kwargs.get("message_base_info") or {}
        user = kwargs.get("user_id") or (base_info.get("user_info") or {}).get("user_id")
        return str(user or "") in admins

    async def _reply(self, stream_id: str, text: str, sent_hint: bool = False) -> tuple[bool, str, int]:
        sent = False
        if stream_id:
            try:
                sent = bool(await self.ctx.send.text(text, stream_id))
            except Exception as exc:
                self.ctx.logger.error("命令回复发送失败: %s", exc, exc_info=True)
        return True, text, 2 if sent else 0

    @Command(
        "dyn",
        description="B站动态订阅管理（add/remove/list/status/test/help）",
        # 别名直接写进正则，不依赖 Host 对 aliases 的处理方式（是否参与匹配未知）。
        # 支持全角/半角斜杠：/dyn /动态 /订阅
        pattern=(
            r"^\s*[/／]\s*(?:dyn|动态|订阅)"
            r"(?:\s+(?P<action>add|remove|list|status|test|help))?"
            r"(?:\s+(?P<arg>\S+))?\s*$"
        ),
        aliases=["动态", "订阅"],
    )
    async def cmd_dyn(
        self, matched_groups: dict | None = None, **kwargs: Any
    ) -> tuple[bool, str, int]:
        action = (matched_groups or {}).get("action") or "help"
        arg = (matched_groups or {}).get("arg") or ""
        stream_id, group_id = self._extract_stream_and_group(kwargs)

        if not self._is_admin(kwargs):
            return True, "仅管理员可使用本命令", 0

        assert self._subs is not None and self._client is not None

        # ---- add ----
        if action == "add":
            if not group_id:
                return await self._reply(stream_id, "请在群聊中使用 /dyn add <UID>（需提供订阅目标群）")
            if not arg.isdigit():
                return await self._reply(stream_id, "用法：/dyn add <UID>（UID 为纯数字）")

            name = await self._client.resolve_up_name(int(arg))
            await self._subs.add(arg, int(group_id), name=name)

            # 判据是"拉不拉得到动态"，不是 UID 位数——
            # 实测存在合法的 16 位 UID（如 3690973091596928 / 共鸣电台_FMInfinity），
            # 单看位数会误伤。只有拿不到昵称（= 一条动态都没拉到）才需要提示核对。
            warn = ""
            if name.startswith("UID:"):
                warn = "\n⚠️ 未拉到该 UP 主的任何动态，无法确认昵称，请核对 UID。"
                if len(arg) > 10:
                    warn += (
                        f"\n你填的是 {len(arg)} 位数（B 站 UID 多为 1~10 位），"
                        "请确认这不是动态链接里的 ID（t.bilibili.com/xxx）。"
                        "\nUID 取 UP 主主页链接中的数字：space.bilibili.com/<数字>"
                    )
                else:
                    warn += "若确认 UID 无误，多半是接口抖动，稍后会自动重试。"

            display = name if name.startswith("UID:") else f"{name}（UID:{arg}）"
            return await self._reply(
                stream_id,
                f"已订阅 {display} 的动态，新动态将自动推送到本群。{warn}",
            )

        # ---- remove ----
        if action == "remove":
            if not group_id:
                return await self._reply(stream_id, "请在群聊中使用 /dyn remove <UID>")
            if not arg.isdigit():
                return await self._reply(stream_id, "用法：/dyn remove <UID>（UID 为纯数字）")
            if self._subs.is_fixed(arg):
                return await self._reply(stream_id, "该订阅来自配置文件（固定订阅），请在配置中移除。")
            ok = await self._subs.remove(arg, int(group_id))
            if ok:
                return await self._reply(stream_id, f"已移除 UID:{arg} 在本群的订阅。")
            return await self._reply(stream_id, "本群没有订阅过该 UID。")

        # ---- list ----
        if action == "list":
            if not group_id:
                return await self._reply(stream_id, "请在群聊中使用 /dyn list")
            gid = int(group_id)
            lines = []
            for uid in self._subs.uid_list():
                groups = self._subs.groups_of(uid)
                if gid in groups:
                    name = self._subs.get_name(uid) or "未知UP主"
                    fixed = " [配置]" if self._subs.is_fixed(uid) else ""
                    lines.append(f"- {name} (UID:{uid}){fixed}")
            if not lines:
                return await self._reply(stream_id, "本群暂无 B 站动态订阅。用 /dyn add <UID> 添加。")
            return await self._reply(stream_id, "📋 本群订阅：\n" + "\n".join(lines))

        # ---- status ----
        if action == "status":
            state = "运行中" if self._running else "已停止"
            last = (
                datetime.fromtimestamp(self._last_poll_ts).strftime("%H:%M:%S")
                if self._last_poll_ts
                else "尚未轮询"
            )
            n_subs = len(self._subs.uid_list())
            # 只显示状态，绝不回显 Cookie 内容
            ck = (self.config.bili.cookie or "").strip()
            if not ck:
                ck_state = "未配置（无登录态，动态可能偶发为空）"
            elif "SESSDATA" in BiliClient.parse_cookie(ck):
                ck_state = "已配置（含 SESSDATA）"
            else:
                ck_state = "已配置但缺少 SESSDATA，登录态不生效"
            return await self._reply(
                stream_id,
                f"📊 B站动态推送：{state}\n"
                f"订阅 UP 主：{n_subs} 个\n"
                f"Cookie：{ck_state}\n"
                f"上次轮询：{last}",
            )

        # ---- test ----
        if action == "test":
            if not arg.isdigit():
                return await self._reply(stream_id, "用法：/dyn test <UID>（立即推送该 UP 主最新一条动态）")
            try:
                items = await self._client.fetch_dynamics(int(arg))
            except Exception as exc:
                return await self._reply(stream_id, f"拉取动态失败：{exc}")
            target = None
            for it in items:
                if is_live_rcmd(it) or is_top_dynamic(it):
                    continue
                if self.config.settings.skip_forward and is_forward_dynamic(it):
                    continue
                target = it
                break
            if target is None:
                return await self._reply(stream_id, "该 UP 主暂无可推送的普通动态。")
            if group_id:
                await self._push_dynamic(arg, target, [int(group_id)])
                return await self._reply(stream_id, "已推送测试动态到本群。")
            if stream_id:
                # 私聊测试：推文本 + 图片到当前会话（与群聊推送格式一致，
                # 否则用户在私聊里永远看不到图片效果）
                parsed = parse_dynamic(target)
                if parsed:
                    text = self._render_push_text(parsed, parsed["author"])
                    image_b64s: list[str] = []
                    for img_url in parsed["images"][: self.config.settings.max_images]:
                        try:
                            image_b64s.append(await self._download_image_b64(img_url))
                        except Exception as exc:
                            self.ctx.logger.warning("图片下载失败 %s: %s", img_url, exc)
                    await self._send_dynamic_content(
                        stream_id, text, image_b64s,
                        where="私聊", sender_name=parsed["author"],
                    )
                    return True, text, 2
            return await self._reply(stream_id, "无法确定推送目标。")

        # ---- help ----
        help_text = (
            "🛠️ B站动态订阅命令\n"
            "──────\n"
            "➕ /dyn add <UID>  订阅 UP 主（群聊）\n"
            "➖ /dyn remove <UID>  取消订阅（群聊）\n"
            "📋 /dyn list  本群订阅列表\n"
            "📊 /dyn status  运行状态\n"
            "🧪 /dyn test <UID>  立即推送一条最新动态\n"
            "❓ /dyn help  本帮助"
        )
        return await self._reply(stream_id, help_text)


def create_plugin() -> BiliPushPlugin:
    """创建插件实例。"""
    return BiliPushPlugin()
