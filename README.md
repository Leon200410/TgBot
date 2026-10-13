# Telegram 消息语音播报 + 保持在线 + 查岗自动回复(Docker 版)

用你自己的 Telegram 账号保持登录/在线,收到**私聊消息**或**群里被@(艾特)**时,
Windows 本机用中文语音自动朗读发送人和内容。

> 这是"用户端"程序(用你本人账号登录),不是 BotFather 机器人——
> 机器人看不到你的私聊和群消息,所以必须用这种方式。

## 架构

Docker 容器(Linux)发不出声音,所以分成两部分:

```
┌───────────────────────────┐  HTTP POST /speak   ┌──────────────────────────┐
│ Docker 容器 tgbot          │ ──────────────────► │ Windows 本机 tts_server   │
│ 保持登录/在线、监听消息      │  端口 8929           │ 中文语音朗读              │
└───────────────────────────┘                     └──────────────────────────┘
  登录 session 存在命名卷 tgdata,重启不丢
```

## 一、准备(只需一次)

1. 浏览器打开 <https://my.telegram.org>,用 Telegram 手机号登录
   → 点 **API development tools** → 应用名随便填,创建
   → 记下 **App api_id** 和 **App api_hash**
2. 把 `.env.example` 复制一份改名为 `.env`,填入 `API_ID` 和 `API_HASH`
   (`ONLINE_HOURS`、`TZ`、设备显示名按需改,见下文)

## 二、首次登录(只需一次)

在本目录打开终端(PowerShell),执行:

```powershell
docker compose build
docker compose run --rm tgbot
```

按提示输入手机号(带国际区号,如 `+8613800138000`)→ 输入 Telegram 发来的验证码
→(如有两步验证)输入密码。看到"已登录"后按 `Ctrl+C` 退出,然后转成后台常驻:

```powershell
docker compose up -d
```

## 三、启动语音服务(Windows 本机)

双击 `tts_server.bat`,保持窗口运行(可最小化)。容器发来的播报文本由它朗读。

- 依赖(换机器时装一次,本机已装好):`pip install pywin32 edge-tts`
- 音色:默认用微软在线自然语音"晓晓"(edge-tts),断网时自动回退系统语音。
  想换音色改 `tts_server.py` 顶部的 `VOICE`(文件里列了常用中文音色),重启本服务生效;
  完整列表:`python -m edge_tts --list-voices`

> 语音服务没开时,容器照常保持在线和记录日志,只是没有声音,开了就恢复。

## 四、日常使用

- 容器 `restart: unless-stopped`,Docker Desktop 启动后自动拉起,无需手动管理
- 看日志:`docker compose logs -f`(每条播报都有记录)
- 停止:`docker compose down`(登录 session 保留在卷里,下次 `up -d` 直接用)
- 播报效果(只念来源,不念内容):
  - 私聊 →「收到 某某 的消息」
  - 群里被@或被回复 →「收到群聊 某群 的艾特」
  - 消息内容不朗读,但完整记录在容器日志里,`docker compose logs` 可回看

## 五、保持在线(ONLINE_HOURS)

`.env` 里的 `ONLINE_HOURS` 或按周几拆开的 `WEEKDAY_ONLINE_HOURS` / `SATURDAY_ONLINE_HOURS` /
`SUNDAY_ONLINE_HOURS` 控制账号对外显示"在线"的时段(按 `TZ` 时区),
容器在时段内每 45 秒上报一次在线状态:

| 填法 | 效果 |
|---|---|
| `always` | 全天 24 小时显示在线(默认) |
| `9:00-23:00` | 每天 9 点到 23 点 |
| `23:00-7:00` | 支持跨午夜 |
| `9:00-12:00,14:00-18:00` | 多个时段,英文逗号隔开 |
| 留空 | 不保持在线,只做消息播报 |

如果你想按周几分别配置,推荐这样写:

```env
ONLINE_HOURS=
WEEKDAY_ONLINE_HOURS=10-13,15-19,20-22
SATURDAY_ONLINE_HOURS=10-13,15-19
SUNDAY_ONLINE_HOURS=
```

上面这组配置表示:
- 周一到周五:10:00-13:00,15:00-19:00,20:00-22:00
- 周六:10:00-13:00,15:00-19:00
- 周日:不上线

当设置了 `WEEKDAY_ONLINE_HOURS` / `SATURDAY_ONLINE_HOURS` / `SUNDAY_ONLINE_HOURS` 时,
它们会优先于 `ONLINE_HOURS`。

改了 `.env` 之后执行 `docker compose up -d` 重建生效。
时段结束后,状态恢复为跟随你手机等设备的真实状态。
别人能否看到你"在线",还取决于 Telegram 里
「设置 → 隐私与安全 → 最后上线时间与在线状态」允许谁查看。

为了让“保持在线”更稳定,程序现在会做两层保活:
- 每隔一段时间调用一次 `account.updateStatus`
- 再按更长的间隔,对“收藏夹”做一次无痕保活 ping(发出后立即删除)

可在 `.env` 里调整:

```env
ONLINE_PING_SECONDS=20
ONLINE_ACTIVITY_PING_SECONDS=240
```

## 六、活跃会话里的设备名称

`.env` 里的下面三项决定 Telegram「设置 → 设备 / 活跃会话」里这个登录显示成什么:

```env
DEVICE_MODEL=Z880 MB
SYSTEM_VERSION=Windows 11
APP_VERSION=1.0
```

改完后执行 `docker compose up -d` 即可;如果你已经用旧设备名登录过,通常还需要删掉旧 session 后重新登录,
新的设备名才会体现在 Telegram 的活跃会话里。

## 七、开机自启

1. **容器**:Docker Desktop → Settings → General → 勾选 *Start Docker Desktop when you sign in*,容器会随之自动启动
2. **语音服务**:`Win + R` 输入 `shell:startup` 回车,把 `tts_server.bat` 的**快捷方式**放进去

## 八、群聊记录(CHATLOG_CHATS)

点名的群,消息会追加写进本目录的 `chatlog/群名.txt`(纯文本),群里的图片存到 `chatlog/media/群名/`。
用的就是现有登录,只读取:不发消息、不标已读,对方看不出来。

- 记哪些群:`docker-compose.yml` 里 `CHATLOG_CHATS` 的默认值,或在 `.env` 里写
  `CHATLOG_CHATS=群名1,群名2`(完整群名,英文逗号隔开);写 `CHATLOG_CHATS=` 留空=不记录
- 第一次记某个群时补最近 300 条(`CHATLOG_BACKFILL`);之后每次重启只补上次记到的位置之后的消息,
  不会重复、不会漏(最多补 2000 条);补历史只下载最近 40 张图片,新消息的图片都下载(单张 10MB 以内)
- 每行格式:`[#消息号 日期 时间] 发言人: (回复 #消息号) 正文 〔图片〕 → media/群名/消息号.jpg`
- 群名对不上或对上多个时不会记,容器日志里会有一行 `[群聊记录]` 提示
- 改了群名单后执行 `docker compose up -d` 生效;`chatlog/state.json` 记着每个群记到哪,
  想让某个群重新补历史就删掉它(或整个 `chatlog/`)

## 九、查岗机器人自动回复

支持如下私聊格式：`查岗提醒（编号 50079）`，消息里有
`请在20分钟内只回复下面的关键词：`，其后单独一行关键词，再接 `回复时限说明：`。
例如关键词是“橙子”，程序只发送 `橙子`，不添加其它内容。

在 `.env` 中设置：

```env
AUTO_CHECKIN_ENABLED=true
CHECKIN_BOT_USERNAME=sy_agent003_bot
```

然后执行 `docker compose up -d --build` 更新容器。首次启用可通过
`docker compose logs -f` 查看“自动查岗:已绑定”和后续处理结果。
关闭时将 `AUTO_CHECKIN_ENABLED` 改为 `false`，再更新容器。

- 默认关闭。用户名会在启动时解析为账号 ID，默认只允许机器人账号；只处理指定账号的入站私聊，群消息、转发和其它账号不会触发。
- 只处理启用后收到的新消息，启动前收到的历史消息不补回复。
- 关键词区必须只有一个非空行，格式不匹配时跳过并打印日志。
- 按消息中的回复分钟数计算期限，过期消息不发送。网络中断或程序未运行时，无法保证及时回复。
- 查岗编号和发送状态保存在登录卷 `/data/auto_checkin_账号ID_目标ID.json`；同一编号只尝试发送一次，重启不会再次发送。
- 发送超时或异常时记录“发送结果无法确认”，不自动重试，以免重复发送；可在 Telegram 中核对该编号。
- 发送成功只记录“等待机器人登记确认”；收到对应编号的“按时响应、已记录”后，才记录登记成功。
- 自动回复独立于保持在线时段：只要程序运行且开关开启，就会处理新的查岗提醒。

### 用私人号测试

程序默认拒绝普通私人号。若用另一个私人号模拟查岗，在 `.env` 中设置：

```env
AUTO_CHECKIN_ENABLED=true
CHECKIN_BOT_USERNAME=Leon_Li451
CHECKIN_ALLOW_USER_TEST=true
```

执行 `docker compose up -d --build`，日志应显示“指定私人号测试模式”。
再由 `@Leon_Li451` 向程序当前登录的账号发送一条新的完整查岗模板，
使用未处理过的新编号（例如 `50081`）和关键词“苹果”，应收到仅包含 `苹果` 的回复。
单独发送 `1`、转发消息、旧编号或启动前已收到的消息均不会触发。
私人号没有真实机器人的登记逻辑，因此收不到“已记录”确认是正常的。

测试完切回正式机器人时，恢复 `CHECKIN_BOT_USERNAME=sy_agent003_bot`，
并设置 `CHECKIN_ALLOW_USER_TEST=false` 后更新容器。不能把目标设为程序登录的账号自身。

## 常见问题

- **my.telegram.org 创建应用一直报 ERROR**:官方页面的老毛病,多半是 IP 风控或临时限制,
  换网络、换浏览器、隔天再试。急用可临时改用 Telegram Desktop 开源客户端公开的凭证:
  `API_ID=2040`、`API_HASH=b18441a1ff607e10a989891a5462e627`。
  个人自用一般没问题,但风控概率比自己的凭证略高;之后申请到自己的建议换回
  (改 `.env` → `docker compose down -v` → 重新走一遍首次登录)
- **容器日志提示"无法连接宿主机语音服务"**:先确认 `tts_server.bat` 在运行;
  仍不行多半是防火墙拦了,用管理员 PowerShell 放行一次:
  ```powershell
  netsh advfirewall firewall add rule name="TgBot TTS 8929" dir=in action=allow protocol=TCP localport=8929
  ```
- **测试语音服务**:`Invoke-RestMethod -Uri http://localhost:8929/speak -Method Post -ContentType "application/json; charset=utf-8" -Body (ConvertTo-Json @{text="测试"})`
- **容器想部署到别的机器(如 NAS)**:把 `.env` 里 `TTS_URL` 改成这台 Windows 的局域网 IP,
  如 `http://192.168.1.100:8929/speak`,并放行防火墙
- **想重新换号登录**:`docker compose down -v`(会删掉登录 session),再走一遍首次登录
- **我已经换了 Telegram 账号,为什么还是旧号**:因为登录 session 保存在 Docker 卷 `tgdata`。
  先执行 `docker compose down -v` 删除旧 session,再执行 `docker compose run --rm tgbot` 重新登录新号
- **我改了 DEVICE_MODEL,为什么活跃会话里还是旧名字**:大概率是因为当前还是旧 session。
  先执行 `docker compose down -v`,再 `docker compose run --rm tgbot` 重新登录;
  重登前后容器日志里也会打印当前读取到的 `device_model / system_version / app_version`

## 安全提示

- `.env` 和 Docker 卷 `tgdata` 里的 session 等同于你的账号凭证,不要泄露;
  分享代码时不要带 `.env`
- `chatlog/` 里是群聊原文和图片,分享代码时同样不要带
- 语音服务监听 0.0.0.0:8929,局域网内其他设备也能让这台电脑"说话",
  介意的话在防火墙规则里只放行 Docker 网段
