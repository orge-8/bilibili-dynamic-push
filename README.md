# B站动态自动推送（bilibili-dynamic-push）

MaiBot 插件：定时轮询订阅的 B 站 UP 主，发现新动态（图文 / 视频 / 转发）自动推送到群聊。

参考 [beiyu504/beiyu_bilibili-dynamic-push](https://github.com/beiyu504/beiyu_bilibili-dynamic-push) 的
订阅 / 去重 / 置顶识别 / 防风控设计重构：

- **不依赖** `bilibili-api-python`，直接调用 B 站 web 端动态接口（`polymer/web-dynamic`），
  仅需 `httpx`，并自行实现 buvid 指纹与 WBI 签名（详见文末「风控与接口实测结论」）。
- **不依赖** napcat adapter 专属 API：推送走 MaiBot SDK 能力代理
  （`chat.open_session` + `send.text` / `send.image`），任何平台适配器均可用。

## 功能

- 多群订阅同一 UP 主；`配置文件固定订阅` 与 `命令动态订阅` 合并管理
- 新动态识别：图文 / 视频 / 专栏 / 转发（可配置过滤）
- 置顶动态识别（新置顶才推，不会重复推旧置顶）
- 旧动态过期过滤（防止插件重启后把历史动态刷屏推一遍）
- 开奖类动态自动丢弃
- 轮询抖动 + UID 间随机间隔，降低风控风险
- 可选填 Cookie 进一步降低风控

## 命令

主命令是 `/dyn`，另外 `/动态`、`/订阅` 三个前缀**等价**，任选其一。
（例如 `/dyn add 123` = `/动态 add 123` = `/订阅 add 123`）

| 命令 | 说明 |
|---|---|
| `/dyn add <UID>` | 在当前群订阅 UP 主 |
| `/dyn remove <UID>` | 取消本群订阅（配置文件的固定订阅不可移除） |
| `/dyn list` | 列出本群订阅 |
| `/dyn status` | 查看运行状态、Cookie 状态与上次轮询时间 |
| `/dyn test <UID>` | 立即推送该 UP 主最新一条动态 |
| `/dyn help` | 帮助 |

不带子命令直接发 `/dyn` 等同于 `/dyn help`。

未配置 `admin_qqs` 时所有人可用；配置后仅管理员可用。

> **UID 怎么取**：打开 UP 主主页，地址栏 `space.bilibili.com/<数字>` 里的那串数字。
> 不要填动态链接 `t.bilibili.com/<数字>` 里的 ID，那是动态 ID 不是 UID。
> UID 位数不固定（多数 1~10 位，但实测存在合法的 16 位 UID），
> 所以插件不以位数判断合法性，而是看能否拉到动态——拉不到时会提示你核对。

## 配置（config.toml，首次运行自动生成）

```toml
[plugin]
enabled = true

[bili]
cookie = ""        # 可选。浏览器登录 B 站后整行复制，防风控
verify_ssl = true  # 有 MITM 代理导致证书报错时可关

[settings]
poll_interval = 180      # 轮询基准（秒），建议 >= 120
poll_jitter = 30         # 抖动（秒）
max_images = 3           # 单条动态最多推几张图
max_dynamic_age = 3600   # 超过该秒数的"新"动态不推
skip_forward = false     # 过滤转发动态
ignore_lottery = true    # 丢弃开奖动态
push_text_template = "📢 {name} 发布了新动态\n{text}"

[admin]
admin_qqs = []           # 管理员 QQ；留空 = 不鉴权

[subscriptions]
users = [                # 固定订阅：UID => 群号1, 群号2
  "114514 => 1919810",
]
```

### 推送格式说明

- **图文 / 转发动态**：直接推**正文文字 + 图片**，不带动态链接。
  新版 OPUS 图文（APP 上带加粗标题的那种）会完整解析**标题 + 正文**——
  插件请求 feed 接口时已带 `features=itemOpusStyle`；
  不带该参数时 B 站会把 OPUS 图文降级为只含图片的 DRAW 格式，文字整体丢失。
- **纯配图动态**（只有图、无文字）：不显示"（无文字内容）"占位符，图片本身就是内容。
- **视频投稿**：文字末尾自动附 `🔗 链接`（QQ 里没法直接播视频，只有封面图时需要入口）。
- 想恢复"所有动态都带链接"，把模板改成：
  `push_text_template = "📢 {name} 发布了新动态\n{text}\n\n🔗 {url}"`
  （模板里已含 `{url}` 时，视频链接不会重复附加。）
- 可用变量：`{name}`（UP 主名）、`{text}`（正文）、`{url}`（动态链接）、`{time}`（发布时间）。
- **模板自动迁移**：老版本部署的 `config.toml` 会把旧模板（带链接）写死在配置里，插件升级后代码默认值变了、配置文件不会跟着变。v1.1.1 起插件会在加载时自动识别"未改过的旧默认模板"并迁移为图文直推格式（自定义过的模板原样保留）。若想彻底回到默认，直接删掉 `config.toml` 里的 `push_text_template` 行让插件重新生成即可。
- **私聊 `/dyn test`** 也会推送图片，方便你在不打扰群的情况下预览完整效果。

## 如何获取 Cookie（可选，但强烈建议）

填 Cookie 主要解决两件事：**动态接口偶发返回空**（表现为昵称解析不出来、
只能显示 `UID:xxx`，或动态偶尔延迟推送），以及**降低风控概率**。不填也能用。

### 步骤（Chrome / Edge，30 秒）

1. 浏览器打开 <https://www.bilibili.com> 并**登录**
2. 按 `F12` 打开开发者工具 → 切到 **Network（网络）** 标签
3. 按 `F5` 刷新页面，在请求列表里点**任意一条**（选第一个就行）
4. 右侧面板 → **Headers** → 往下找到 **Request Headers**
5. 复制 `cookie:` 那一行的**完整值**（很长，一整行，注意别漏）

   <details>
   <summary>另一种取法（Application 面板）</summary>

   F12 → Application → 左侧 Storage → Cookies → `https://www.bilibili.com`，
   逐个复制需要的字段，手动拼成 `key1=value1; key2=value2` 格式。
   适合只想给最少字段的情况。
   </details>

6. 粘贴到 `config.toml` 的 `bili.cookie`，用**英文双引号**包起来：

   ```toml
   [bili]
   cookie = "SESSDATA=xxxxx; bili_jct=xxxxx; DedeUserID=xxxxx"
   ```

### 各字段的作用

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `SESSDATA` | ✅ 关键 | 登录态主体。本插件只读动态，有它就够 |
| `DedeUserID` | ⭕ | 你的 UID，带上更像真实用户 |
| `buvid3` / `buvid4` | ⭕ | 浏览器指纹。带了就用你的，插件不再另申请 |
| `bili_jct` | ❌ | CSRF token，只在点赞/转发等写操作时需要，本插件用不上 |
| `DedeUserID__ckMd5` / `sid` | ❌ | 同上，可不带 |

> 想少贴点隐私的话，只填 `SESSDATA` 一个字段就能工作。

### 注意事项

- **SESSDATA 等价于账号密码**。别发群里、别提交 Git、别贴到日志里。
  `config.toml` 已被 `.gitignore` 排除，但请确认你没有把它复制去别处。
- **会过期**，通常一个月左右（登录时勾选"记住我"会更长）。过期后插件不会报错，
  只会**悄悄退化为无登录态**——表现就是动态又开始偶发为空。所以建议日历里设个提醒。
- **换设备/改密码会让已发出的 Cookie 失效**，需要重新取。
- 插件重启或配置热更新会立即生效，不需要重启 MaiBot 主程序。

### 怎么确认生效

配置填好后，重启插件（或改一下配置触发热更新），MaiBot 日志里看插件初始化是否正常；
然后在群里发 `/dyn test <UID>`。

- 昵称正常显示（如"罗翔说刑法"）而不是 `UID:517327498` → Cookie 生效
- 仍显示 `UID:xxx` → Cookie 未生效或已过期，检查复制时是否漏字符、引号是否闭合

## 安装

1. 把本目录放入 MaiBot 的 `plugins/` 下
2. 重启 MaiBot（新增 capabilities 需重启生效）
3. WebUI 插件管理中启用本插件，编辑配置填入订阅
4. 群里发 `/dyn add <UID>` 快速订阅

## 数据文件

- `data/plugins/org.mai-mai.bilibili-dynamic-push/subscriptions.json` - 订阅关系
- `data/plugins/org.mai-mai.bilibili-dynamic-push/push_history.json` - 已推送动态 ID（去重基准）

两者自动维护，请勿手改（损坏会自动备份为 `.broken` 并重建）。

## 风控与接口实测结论（重要）

本插件自行实现了 B 站 web 接口的两道门槛，均为实测验证：

1. **buvid3/buvid4 指纹**：首次请求前调 `x/frontend/finger/spi` 获取，写入 Cookie。
2. **WBI 签名**：`x/web-interface/nav` 取 `wbi_img` → mixin key → 请求参数加 `wts` + `w_rid`。
   （`nav` 未登录会返回 `-101`，但 `wbi_img` 字段仍有效，取它即可。）

实测踩到的坑，已针对性处理：

- **`acc/info`（用户信息）风控极严**：未登录时恒返回 `-352`；更麻烦的是，
  一旦请求过它，**同一会话随后的动态接口会被连累返回 0 条**。
  → 因此 `/dyn add` 不再调用它，昵称直接取自动态流里的 `module_author.name`，
  取不到就降级显示 `UID:xxx`（首次成功推送时会自动补上昵称）。
- **动态接口偶发返回空**：无 Cookie 时 `feed/space` 会间歇性返回 `items: []`
  （无错误码，纯抖动）。→ 空结果会重试一次；**且拿到空列表时绝不推进去重基准**，
  所以最多是延迟推送，不会漏推。
- `-352` 属风控而非签名问题，**不做重试**（重试只会加剧）；仅 `-403`/`412` 会重建签名重试。

### 遇到 `HTTP 412`（接口返回非 JSON）怎么办

日志长这样：

```
UID 517327498 接口错误: B 站风控拦截（HTTP 412）。可尝试：① 填写 bili.cookie ...
```

**这里的 412 是 HTTP 状态码，不是 B 站 JSON 里的 `code` 字段**，两者都可能出现 412，别混淆。
它表示请求被 B 站 WAF 拦下，返回的是 HTML 风控页，所以解析 JSON 失败。

### 三种"412"要分清（真机踩坑实录）

| 现象 | 本质 | 对策 |
|---|---|---|
| HTTP 状态码 412 + **HTML 响应**（`接口返回非 JSON`） | WAF 传输层拦截 | 补请求头 / 填 Cookie / 换 IP |
| HTTP 200/412 + **JSON `code:-412 "request was banned"`** | **业务层风控：buvid 是"死指纹"** | 见下文指纹激活（v1.1 已自动处理） |
| JSON `code:412` | WBI 签名失效 | 自动重建签名重试 |

**关键机制——buvid 指纹激活（ExClimbWuzhi）**：
通过 `finger/spi` 拿到的 buvid3/buvid4 是"未激活"状态。B 站风控要求指纹
必须与一台"真实设备"绑定过——即向 `x/internal/gaia-gateway/ExClimbWuzhi`
POST 一份浏览器环境指纹 payload（屏幕分辨率/时区/字体/WebGL 参数等，
payload 里的 UA 要与请求头一致），服务端校验通过后 buvid 才被标记为可信。
**未激活的"死指纹"访问 `feed/space` 会被业务层以 `-412 request was banned` 拒收**，
且这个拒绝与 HTTP 头是否完整无关（真机实测：补全 13 个头仍被拒）。

v1.1 起，插件在每次申请新指纹后自动执行激活（上报设备指纹，payload 为
Windows/Chrome 风格并与 UA 匹配）；被 `-412` 拒时会自动丢弃旧指纹、
重新申请并激活后再试一次。用户 Cookie 自带 buvid3 时视为已激活，不上报。

### 遇到 412 怎么办

排查顺序：

0. **先跑诊断脚本** —— 一步定位，不用猜：

   ```
   <MaiBot 的 python> diagnose_bili.py <UID> "SESSDATA=xxx; ..."
   ```

   不需要启动 MaiBot，只依赖 httpx。输出包含：环境/代理/SSL 检查、
   **走代理 vs 直连的对比**（判断是否 TLS 指纹拦截）、spi + 指纹激活 +
   WBI + feed 完整握手、Cookie 字段检查（是否缺 SESSDATA / 是否过期）、
   响应体预览与动态条数。把整段输出发来即可定位。

1. **填 `bili.cookie`** —— 最有效，见上文《如何获取 Cookie》。
   登录态能放宽 WAF 对 TLS 指纹和访问频率的检查，是被风控后唯一稳定的解法。
2. **调大 `poll_interval`** —— 建议 ≥ 300 秒。插件已内置指数退避
   （连续失败时轮询间隔翻倍，上限 30 分钟），但如果基础间隔太短仍会持续触发。
3. **若诊断显示"走代理正常、直连被拦"** —— 这是 **TLS 指纹（JA3/JA4）拦截**：
   Python httpx 的 TLS 握手特征与真实 Chrome 差异很大，WAF 在传输层就能识别，
   **再怎么改 HTTP 请求头都没用**。走代理时握手对象变成代理，指纹被"洗"掉所以能过。
   对策：填 Cookie > 让本机也走代理 > 换网络。
4. **换网络** —— 服务器/机房 IP 段是 B 站风控重点关照对象，家庭宽带宽松得多。
   同一份代码在开发环境正常、部署到服务器就 412，基本就是这个原因。
5. 检查是否在同一 IP 上跑了多个 B 站相关插件，它们共享风控计数。

插件已做的防护：完整且自洽的浏览器请求头（UA + `sec-ch-ua` + `sec-fetch-*`，
版本号互相匹配）、**buvid 指纹自动激活（ExClimbWuzhi）**、WAF 状态码识别、
重试前退避（3s/8s）、连续失败指数熔断、`-412` 后自动换新指纹重激活。

使用建议：

- `poll_interval` 保持 ≥ 120 秒
- 在 `bili.cookie` 填入登录后的 Cookie 可显著提升稳定性、降低风控概率
- `/dyn test` 会即时请求接口，不要频繁使用

## 已知边界

- 视频/专栏内容以文字 + 封面图推送，不下载视频本体（保持轻量；如需视频可自行扩展）
- 直播开播通知走直播状态接口的批量查询，当前版本未启用（预留）

## License

MIT
