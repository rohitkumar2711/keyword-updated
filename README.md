# ⚡ High-Performance Keyword Search Bot

Ultra-fast Telegram bot for keyword searching inside large files & archives (.txt, .zip, .rar). Zero-space extraction + Regex Engine.

## 🚀 One-Click Deploy

[![Deploy to Heroku](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/Shreerambro/Keyword-Checker)

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Shreerambro/Keyword-Checker)

> ⚠️ Replace `yourusername` with your actual GitHub username in the URLs above after pushing!

## ✨ Features
- 🚀 **Zero-Space Extraction** — Searches inside ZIP/RAR without extracting to disk
- ⚡ **Regex Engine** — 10x-50x faster than normal Python search
- 🔐 **Password Protected Archives** — Full support
- 👑 **Owner Controls** — `/promote`, `/demote`, `/listadmins`
- 📊 **Stats Dashboard** — Full analytics with CSV/TXT export
- 💾 **Dual Database** — SQLite for VPS, MongoDB for Heroku/Render persistence
- 🛡️ **Anti-FloodWait** — Smart message throttling

## 📋 Setup Variables

| Variable | Description |
|----------|-------------|
| `API_ID` | From [my.telegram.org](https://my.telegram.org) |
| `API_HASH` | From [my.telegram.org](https://my.telegram.org) |
| `BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) |
| `OWNER_ID` | Your Telegram User ID (use `/myid`) |
| `MONGODB_URI` | **(Optional)** Required for Heroku/Render to prevent data loss on restart |
| `CHANNEL_ID`  | Channel id for getting Logs |

## 🐳 Coolify / Docker Deployment
This repo ships a `Dockerfile`. On Coolify:
1. **New Resource → Application** → your Git repo. Build pack: **Dockerfile**.
2. **Environment variables:** `API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID` (plus optional `MONGODB_URI` / `DATABASE_URL`, `LOG_CHANNEL_ID`).
3. **Persistence:** add a **Volume** mounted at `/app/data` (the bot already uses `DATA_DIR=/app/data` for the SQLite DB + session) — *or* set `MONGODB_URI`/`DATABASE_URL` and skip the volume. Without one of these, data is lost on every redeploy.
4. **It's a worker (no web port).** Either **disable the health check**, or set env `HEALTHCHECK_PORT=8080`, expose port `8080`, and point the health check at `/`.

```bash
# Local Docker test
docker build -t keyword-bot .
docker run --env-file .env -v $(pwd)/data:/app/data keyword-bot
```

## 🖥️ VPS Deployment
```bash
git clone https://github.com/yourusername/SearchBotRepo
cd SearchBotRepo
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your credentials
python bot.py
```

## 🤖 Bot Commands
| Command | Access | Description |
|---------|--------|-------------|
| `/start` | All | Welcome message |
| `/fetch` | All | Fetch files from channel |
| `/url` | All | Search a `.zip` directly from a URL (HTTP Range, no full download) |
| `/cancel` | All | Cancel current operation |
| `/myid` | All | Show your Telegram ID |
| `/stats` | Admin | View search statistics |
| `/import` | Admin | Import CSV data |
| `/promote` | Owner | Add admin |
| `/demote` | Owner | Remove admin |
| `/listadmins` | Owner | List all admins |
| `/editmode` | Admin | Toggle smart edit |
