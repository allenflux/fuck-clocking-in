# Slack 考勤私聊自动回复

用你本人授权的 Slack 账号，直接在指定考勤机器人的私聊里回复随机数字。Python 3.11+，仅使用标准库，不需要浏览器、AI、数据库服务器或对外开放端口。

识别的消息格式：

```text
【考勤抽查】请在 30 分钟内（18:52 前）直接回复以下数字：1362
```

回复内容为 `1362`，保持前导零，不发在线程里。只有指定私聊、指定发送者、完整匹配模板的消息才会触发回复。

## 1. 获取你自己的 Slack 用户 Token

1. 打开 [Slack 应用管理](https://api.slack.com/apps)，选择 **Create New App → From a manifest**，选择考勤机器人所在的工作区，导入本项目的 `slack-app-manifest.json`。
2. 在 **OAuth & Permissions** 确认 **User Token Scopes** 为 `im:history`、`im:read`、`chat:write`，然后 **Install to Workspace**，使用你本人账号授权。工作区如果限制安装应用，需要管理员批准。
3. 把 **User OAuth Token**（`xoxp-` 开头）写到本地 `.env` 的 `SLACK_USER_TOKEN`。这是用户令牌，程序会验证账号身份；`xoxb-` Bot Token 不能代替。

权限依据：[用户 Token 身份](https://docs.slack.dev/authentication/tokens/)、[读取私聊](https://docs.slack.dev/reference/methods/conversations.history/)、[发送消息](https://docs.slack.dev/reference/methods/chat.postMessage/)。无需 `as_user`、修改头像或浏览器 Cookie。聊天里的 Slack 连接不会导出 Token 给 VPS。

Token 仅留在本地配置中，`.env` 已被 Git 和 Docker 构建忽略。程序日志不输出 Token。若工作区强制 Token 轮换，本版本不负责自动刷新，需要使用符合该工作区要求的 OAuth 刷新流程。

## 2. 配置并检查

当前本地 `.env` 已预填这次确认的账号和私聊 ID，只需填用户 Token。新克隆的仓库先执行：

```sh
cp .env.example .env
chmod 600 .env
```

编辑 `.env`（不要在聊天里粘贴 Token）：

| 配置 | 含义 |
| --- | --- |
| `SLACK_USER_TOKEN` | 你本人的 `xoxp-` 用户令牌 |
| `SLACK_ATTENDANCE_CHANNEL_ID` | 与考勤机器人的 `D…` 私聊 ID |
| `SLACK_ATTENDANCE_SENDER_ID` | 考勤机器人的 `U…` 用户 ID；旧版集成也支持 `B…` bot_id |
| `SLACK_EXPECTED_USER_ID` | 你的 `U…` 用户 ID，防止 Token 配错账号 |
| `SLACK_TIMEZONE` | 机器人文字里截止时间的时区，示例按 `Asia/Shanghai` 配置 |
| `POLL_INTERVAL_SECONDS` | 检查间隔，默认 60 秒，允许 60–600 秒 |
| `HTTP_TIMEOUT_SECONDS` | 单次网络超时，默认 15 秒 |
| `ATTENDANCE_STATE_PATH` | 持久化记录位置，默认 `data/state.sqlite3` |

配置文件支持字面量 `KEY=VALUE` 和整行注释，不执行 shell 命令、不做变量插值；环境变量优先。

如果不知道 ID，先只填 Token：

```sh
python3 main.py --list-dms
```

程序会显示你的用户 ID、私聊 ID 及对方用户 ID。可用机器人资料中的成员 ID 匹配。填入私聊 ID 后执行：

```sh
python3 main.py --inspect
```

这一步只读，会显示账号、私聊对方及最近的考勤消息发送者 ID。填好剩余配置后模拟检查：

```sh
python3 main.py --once --dry-run
```

模拟模式不发送、不写入回复记录。没有仍在有效期内的抽查时，不会出现“将回复”日志。正式前台运行：

```sh
python3 main.py
```

## 3. VPS 部署：Docker Compose

将本项目复制到 VPS，准备好 `.env`，在项目目录执行：

```sh
docker compose build
docker compose run --rm attendance python main.py --inspect
docker compose run --rm attendance python main.py --once --dry-run
docker compose up -d
docker compose logs -f --tail=50 attendance
```

不映射任何端口，只需要 VPS 能通过 HTTPS 访问 `slack.com`。容器以普通用户运行，设置 128 MiB 内存上限和 0.25 CPU 上限；这些是上限，实际占用取决于运行环境。回复记录保存在命名卷 `attendance-data`，重建容器会保留。

停止：

```sh
docker compose down
```

保留命名卷以保留回复记录。只运行一个实例，不要同时运行 Docker 版和另一份独立状态文件的程序。

## 4. VPS 部署：systemd（不装 Docker）

VPS 需要 Python 3.11+ 和系统时区、证书数据。以 Debian 12 / Ubuntu 24.04 为例：

```sh
sudo apt-get update
sudo apt-get install -y python3 ca-certificates tzdata
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin slack-attendance
sudo install -d -m 755 /opt/slack-attendance
sudo cp -r attendance main.py /opt/slack-attendance/
sudo install -m 600 .env /etc/slack-attendance.env
sudo install -m 644 deploy/attendance.service /etc/systemd/system/attendance.service
sudo systemctl daemon-reload
sudo systemctl enable --now attendance.service
sudo journalctl -u attendance.service -f
```

systemd 自动创建私有状态目录 `/var/lib/slack-attendance`。网络故障会退避重试；Token、权限和目标配置错误会退出并停服，修复 `/etc/slack-attendance.env` 后执行 `sudo systemctl restart attendance.service`。

## 行为和可靠性

- 常态每分钟对一个私聊调用一次历史接口；每页 15 条，只读取最近两小时。超过一页时完整翻页，每次历史请求之间至少间隔 60 秒；翻完后再刷新最新消息，核对分页期间的手动回复。若新增消息仍需分页，该轮先不发送。私聊非常活跃时，分页会增加响应延迟。
- SQLite 记录每条抽查的处理状态，重启后保留；同一状态文件使用进程锁，阻止重复实例。
- 发现你已经在该抽查之后、下次抽查之前直接回复相同数字，会跳过；不会把上次相同数字的回复算给新抽查。
- 截止时间取“消息发送时间 + 指定分钟数”和“正文 HH:MM”两者较早值，支持跨午夜；过期消息不会发送。时区须与机器人正文一致。
- HTTP 429 遵守 `Retry-After`，网络错误退避等待；每次发送前重新检查期限。
- 发送前落盘。若发送超时、服务端结果不明或发送过程中崩溃，下次先读取历史核对；没有明确成功记录时，不会自动再次发送这条抽查，会写警告，请查看私聊并按需手动回复。这样避免超时造成重复发言，但极端故障下可能漏掉一次。
- 手动回复若恰好发生在历史读取与自动发送之间，仍存在并发重复的窄窗口。Slack 接口没有这里可依赖的完整事务保障。
- 本程序只发送数字。Slack 接口返回成功表示消息已发送，考勤是否通过由对方机器人判定。

轮询和限流参考：[历史接口限制](https://docs.slack.dev/reference/methods/conversations.history/)、[429 处理](https://docs.slack.dev/apis/web-api/rate-limits/)。

## 本地验证

```sh
python3 -m unittest discover -s tests -v
```

测试使用模拟 Slack 响应，不会向真实私聊发送测试消息。
