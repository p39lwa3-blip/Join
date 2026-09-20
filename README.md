# CT小舖交易 Bot V11｜代肝版

這是 CT小舖交易機器人的 V11 版本。

## 主要功能

- 新工單只顯示兩個服務：
  - 🪙 幣號
  - 🛠️ 代肝
- 幣號規格：
  - 50M 有33等
  - 100M 無33等
  - 100M 有33等
  - 不死號（不會被官方掃幣號封）
- 代肝額度：
  - 50M
  - 100M
  - 150M
  - 200M
  - 300M
  - 400M
  - 500M
  - 600M
  - 700M
  - 800M
  - 900M
  - 1000M
- 代肝價格可由管理員獨立設定。
- 幣號可設定：
  - 🟢 正常提供
  - 🟡 暫停提供
  - 🔴 缺貨
- 付款後進入「待確認付款」。
- 管理員可使用「💰 確認付款」確認付款。
- 代肝付款確認後會通知客人提供遊戲帳號，收到帳號後進入排單流程。
- 不死號使用「待洽談」，不直接進付款流程。
- 支援非營業時間提示。
- 工單只偵測新的 `ticket-數字` 工單，不在啟動時掃描舊工單。
- 工單會依訂單自動改名。
- 使用 SQLite 保存訂單、價格、庫存、狀態及設定。

## Railway

Railway 需要：

- `bot.py`
- `requirements.txt`
- `README.md`

`requirements.txt`：

```text
discord.py>=2.5,<3
```

需要在 Railway Variables 設定：

```text
DISCORD_TOKEN=你的 Discord Bot Token
```

不要把 Token 寫進 `bot.py` 或 GitHub。

## Discord Developer Portal

因為機器人需要在工單中讀取客人直接輸入的遊戲帳號，需要開啟：

- Message Content Intent

同時 Bot 邀請需要：

- bot
- applications.commands

## 注意

本版本的 `bot.py` 是 V11 代肝版。
不要用舊版 V10 的 `bot.py` 覆蓋本版本。
