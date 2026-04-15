# Telegram Claude Bridge

通过 Telegram 群组 Topic 远程控制本地 Claude Code 的桥接工具。

---

## 安装步骤

### 1. 检查 Python 版本

```bash
python3 --version   # 需要 3.10+
```

### 2. 进入项目目录，创建虚拟环境

```bash
cd ~/telegram_claude_bridge
python3 -m venv .venv
source .venv/bin/activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 配置 .env

```bash
cp .env.example .env
```

用编辑器打开 `.env`，填写以下必填项：

```
TELEGRAM_BOT_TOKEN=你的 bot token
ALLOWED_CHAT_IDS=-1001234567890   # 你的群组 chat_id（负数）
ALLOWED_USER_IDS=123456789        # 你的 Telegram user_id
```

---

## 如何获取 chat_id 和 user_id

**user_id**：向 [@userinfobot](https://t.me/userinfobot) 发送任意消息，它会回复你的 user_id。

**chat_id（群组）**：
1. 把 bot 加入群组并给管理员权限
2. 在群组里发一条消息
3. 访问：`https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
4. 找到 `"chat":{"id":...}` 字段，群组 id 是负数

---

## Telegram 群组配置

1. 创建一个群组（或使用已有群组）
2. 开启 **Topics（话题）** 功能：群组设置 → 话题 → 开启
3. 把 bot 加入群组，设为管理员
4. 通过 [@BotFather](https://t.me/BotFather) 关闭 Privacy Mode：
   ```
   /mybots → 选择你的 bot → Bot Settings → Group Privacy → Turn off
   ```

---

## 运行

```bash
# 方式一：直接运行
source .venv/bin/activate
./start.sh

# 方式二：手动设置代理后运行
export https_proxy=http://127.0.0.1:7897
export http_proxy=http://127.0.0.1:7897
export PATH="$HOME/.local/bin:$PATH"
source .venv/bin/activate
python app.py
```

---

## 使用方法

1. 打开 Telegram，进入你的群组
2. 点进某个 **Topic（话题）**
3. 发送任意消息，bot 会提示选择工作目录
4. 选择目录后 Claude 会话自动启动
5. 之后直接发消息即可与 Claude 对话

每个 Topic = 一个独立的 Claude Code 会话，可同时开多个。

---

## 命令列表

| 命令 | 说明 |
|------|------|
| `/start` | 欢迎信息 |
| `/help` | 命令列表 |
| `/status` | 当前 Topic 状态 |
| `/cwd` | 当前工作目录 |
| `/resetdir` | 重置工作目录（弹出选择菜单） |
| `/setdir <路径>` | 直接设置工作目录 |
| `/restart` | 重启 Claude 会话 |
| `/topics` | 列出所有 Topic |
| `/ping` | Bot 在线检测 |
| `/schedule add <分> <时> <日> <月> <周> <提示词>` | 添加定时任务 |
| `/schedule list` | 查看本 Topic 定时任务 |
| `/schedule del <id>` | 删除定时任务 |
| `/schedule on/off <id>` | 启用/禁用定时任务 |

发送图片/截图，Claude 会自动分析图片内容。

---

## 常见故障排查

### Bot 没有回应

- 确认代理已开启（Clash 端口 7897）
- 确认 `TELEGRAM_BOT_TOKEN` 正确
- 确认 bot 在群组里有管理员权限
- 确认 Privacy Mode 已关闭
- 查看日志：`tail -f logs/app.log`

### 能发消息但 Telegram 没有输出

这是最常见问题，按以下步骤排查：

**第一步：确认 Telegram 收到了消息**
```bash
grep "user message" logs/telegram.log | tail -20
```

**第二步：确认写进了 Claude stdin**
```bash
grep "stdin" logs/claude.log | tail -20
```

**第三步：确认 Claude 有输出**
```bash
grep "stdout\|stderr" logs/claude.log | tail -20
```

**第四步：确认 buffer flush 了**
```bash
grep "flushing" logs/telegram.log | tail -20
```

**第五步：确认 Telegram send 成功**
```bash
grep "sent\|send.*failed" logs/telegram.log | tail -20
```

如果第三步没有输出，说明 Claude 进程本身没有产生输出，可能原因：
- Claude 在等待交互输入（某些版本启动时有欢迎界面）
- Claude 进程启动失败（查看 `logs/claude.log` 里的 stderr）
- 工作目录权限问题

**开启 DEBUG 模式获取更多信息：**
```bash
# 在 .env 里设置
LOG_LEVEL=DEBUG
```

### Claude 命令找不到

```bash
which claude
# 如果找不到，在 .env 里手动指定：
CLAUDE_BIN=/Users/yourname/.local/bin/claude
```

### 代理问题

确认 `.env` 里的代理配置：
```
HTTP_PROXY=http://127.0.0.1:7897
HTTPS_PROXY=http://127.0.0.1:7897
```

---

## 日志文件位置

| 文件 | 内容 |
|------|------|
| `logs/app.log` | 主日志 |
| `logs/telegram.log` | Telegram 收发日志 |
| `logs/claude.log` | Claude 进程输入输出日志 |

实时查看：
```bash
tail -f logs/app.log logs/telegram.log logs/claude.log
```
