# openrelay-tg-bot

Telegram channel AI assistant — summarize, ask, FAQ — paid per request in USDC
via [OpenRelay](https://openrelay.hggfffdfy687.workers.dev) (x402 protocol).

Two deployment tiers:

|                     | **Simple** (`$75` one-time) | **Docker** (`$25` one-time) |
|---------------------|-----------------------------|-----------------------------|
| Bot identity        | `@OpenRelayBot` (ours)      | you create via BotFather    |
| Infra               | ours                        | your `docker compose up`    |
| Wallet custody      | custodial subaccount        | your key in `.env`          |
| Data                | our Postgres                | your local Postgres         |
| Updates             | auto                        | `docker pull`               |

Two payment modes (owner chooses per command):

- **Treasury** — channel pays. Owner deposits USDC once, bot spends without
  a signature per request.
- **User-pays** — user pays via mini-app (Base / Solana / TG Stars); owner
  earns a configurable revenue-share (default 20%).

Beta status: setup fee is code-complete but flag-disabled. First round of
channels is free.

## Architecture

```
tg → bot-router → context-svc ⇄ postgres+pgvector+redis
                       ↓
                  openrelay (x402) ← billing-svc (subaccount signer)
```

- `packages/context_svc` — ingest (telethon), hierarchical digests, retrieval
- `packages/billing_svc` — subaccount derivation, deposit watcher, x402 signing
- `packages/bot` — aiogram router, commands, mini-app callbacks
- `packages/common` — config, db, models

## Local setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
docker compose -f docker/docker-compose.yml up -d
cp .env.example .env  # fill TG_API_ID / TG_API_HASH from my.telegram.org/apps
alembic upgrade head
python -m packages.context_svc.ingest backfill <YOUR_CHAT_ID>
```

## License

MIT
