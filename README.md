# Telegram Music Bot

An async Telegram bot that searches the Apple Music catalog and sends song previews
back as playable audio files. Built with aiogram 3, SQLite and aiohttp — no
third-party API keys required.

## Features

- 🔎 Song search by title or artist (any plain message or `/search`)
- 📄 Paginated inline results and per-track detail cards
- ⬇️ 30-second audio previews uploaded with title, artist and cover art
- 🕘 Per-user download history with one-tap re-send
- 🛅 Disk cache for previews, so re-sending a track doesn't download it again
- 👮 Admin panel with live statistics, broadcasts, and ban/unban
- 🐳 Docker image and SQLite storage (no external services)

## Requirements

- Python 3.11+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your numeric Telegram user ID (for example from [@userinfobot](https://t.me/userinfobot)) if you want admin access

## Setup

```bash
cd telegram_music_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: set BOT_TOKEN and (optionally) ADMIN_IDS

python main.py
```

The database and audio cache are created automatically under `data/`.

### Docker

```bash
docker build -t telegram-music-bot .
docker run -d --name music-bot --env-file .env \
  -v music-bot-data:/app/data \
  telegram-music-bot
```

### Render (free web service)

The bot can serve a tiny HTTP health endpoint, enabled only when `PORT` is set.
Render provides that variable automatically; local runs stay polling-only.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/asilbek-web/Music-bot)

1. Push this project to a GitHub repository (private works too).
2. In the Render Dashboard choose **New → Blueprint**, select the repository and
   apply the included `render.yaml`. Render creates a **Free** Python web service
   with `/health` as its health check path.
   - Or create a **Web Service** manually: build
     `pip install -r requirements.txt`, start `python main.py`, health check
     `/health`, plan **Free**.
3. In the service's **Environment** tab set `BOT_TOKEN`. The Blueprint marks it as
   a secret (`sync: false`), so it is never read from Git. `ADMIN_IDS` is already
   set in `render.yaml`.
4. Deploy and open `https://<service>.onrender.com/health`; it should return
   `{"status": "ok", ...}`.
5. Keep it awake: add a free **HTTP(s)** monitor in UptimeRobot for that `/health`
   URL with a 5-minute interval. Render spins a Free web service down after 15
   minutes without inbound traffic.

Free-plan caveats: the filesystem is ephemeral, so the SQLite database (users,
history, bans, stats) is lost on every deploy, restart or spin-down, and cached
audio is re-downloaded. Persistent disks require a paid instance. Each workspace
has 750 Free instance hours per month, and Render may restart Free services at
any time, so this setup is fine for a hobby bot but not for guaranteed uptime.

## Configuration

All settings are read from environment variables or the `.env` file.

| Variable | Default | Description |
| --- | --- | --- |
| `BOT_TOKEN` | — | **Required.** Bot token from @BotFather |
| `ADMIN_IDS` | empty | Comma-separated user IDs allowed to use admin commands |
| `DB_PATH` | `data/music_bot.db` | SQLite database location |
| `AUDIO_DIR` | `data/audio` | Cache directory for downloaded previews |
| `SEARCH_LIMIT` | `10` | Number of results per search (1–25) |
| `ITUNES_COUNTRY` | `US` | Storefront used for search, e.g. `US`, `GB`, `DE` |
| `REQUEST_TIMEOUT` | `20` | Seconds before a catalog request times out |
| `AUDIO_CACHE_TTL` | `86400` | Seconds a cached preview is reused (0 = keep forever) |
| `MAX_AUDIO_SIZE` | `20971520` | Maximum download size in bytes |
| `PORT` | unset | Port for the HTTP health server; set automatically by Render |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |

## Commands

### Everyone

| Command | Description |
| --- | --- |
| `/start` | Main menu |
| `/search [query]` | Search for a song |
| `/history` | Recent downloads, tap one to get the audio again |
| `/help` | Usage instructions |
| `/cancel` | Abort the current prompt |

Any text message that isn't a command is treated as a search query.

### Admins (`ADMIN_IDS`)

| Command | Description |
| --- | --- |
| `/admin`, `/stats` | User, ban and download statistics |
| `/broadcast` | Copy one message to every non-banned user, with confirmation |
| `/ban <id>` / reply with `/ban` | Block a user |
| `/unban <id>` / reply with `/unban` | Unblock a user |

Banned users are stopped by the middleware before any handler runs.

## Project structure

```
telegram_music_bot/
├── main.py              # Entry point: config, wiring, polling, health server
├── config.py            # Environment configuration (pydantic-settings)
├── requirements.txt
├── .env.example
├── Dockerfile
├── render.yaml
├── README.md
├── handlers/
│   ├── __init__.py
│   ├── user.py          # Menu, search, pagination, downloads, history, middleware
│   └── admin.py         # Stats, broadcast, ban/unban
├── services/
│   ├── __init__.py
│   ├── search.py        # iTunes Search API client and Track model
│   └── audio.py         # Preview downloading and on-disk cache
├── database/
│   ├── __init__.py
│   └── db.py            # aiosqlite layer: users, bans, downloads, stats
└── keyboards/
    ├── __init__.py
    └── inline.py        # Inline keyboards
```

## How it works

1. A text message reaches `handlers/user.py`, which calls `MusicSearch.search`.
2. `MusicSearch` queries `https://itunes.apple.com/search` (song entity) and keeps
   results that include a `previewUrl`.
3. The results and a short search ID are stored in aiogram's FSM storage. Inline
   buttons carry only the search ID and index, so callback data stays under
   Telegram's 64-byte limit.
4. Tapping **Get audio** downloads the preview through `AudioService` (size and
   timeout guarded, cached by track ID), then uploads it with `send_audio`,
   including the artwork as the thumbnail when Telegram accepts it.
5. The download is stored per user for `/history`.

## Notes and limitations

- **Previews are about 30 seconds long.** They come from Apple's public preview
  catalog, which is keyless and permitted for this use. Full tracks are not
  distributed by this bot; swap the provider in `services/search.py` and
  `services/audio.py` if you have a licensed source.
- Search results are held in memory (`MemoryStorage`). After a restart, old
  result buttons reply that the search expired; downloading from `/history`
  still works because the preview URL is stored in SQLite.
- Catalog requests are subject to Apple's rate limits. The bot makes one request
  per search, and cached audio is reused for repeated downloads.
- `data/` contains the database and cached audio, so keep it on a volume in
  Docker and out of version control. A named volume is used above because Docker
  copies the image's `bot` ownership into it; a bind mount may require
  `chown 1000:1000` on the host directory.
- On Render's Free plan there is no persistent disk: the SQLite file and audio
  cache are recreated after each deploy, restart or idle spin-down. History and
  statistics are therefore temporary unless you upgrade the instance or move the
  data to a managed database.
