# Telegram Calorie Tracker Bot

[한국어](README.md) | **English**

A personal-first Telegram bot for tracking calories and macros in packaged foods. Send a
barcode to reuse a saved product. For products the bot has not seen before, it uses an OpenAI
vision model to read photos of the product front and nutrition label. AI-extracted data is only
recorded and cached after the user reviews it.

## Features

- Scan a barcode from a photo or enter its digits manually
- Look up products in the local database, then Open Food Facts, then from photos with AI
- Track calories, carbohydrates, protein, and fat
- Choose the whole package, half, one serving, or the nutrition-label basis, or enter a custom
  amount
- Convert `g`, `ml`, item, package, and `%` units, and reuse the last amount consumed
- View today's totals and recent entries, undo the latest entry, and set daily goals
- Add entries manually
- Lock the bot to one owner or optionally allow public sign-up
- Run locally with SQLite or in Docker with PostgreSQL
- Use Telegram long polling, with no domain, TLS certificate, or public web port required
- Check the database and application status through `GET /healthz`

## How it works

```text
Telegram photo
  └─ Decode barcode
      ├─ Found in local DB ─────────────> Choose amount → Save entry
      ├─ Found in Open Food Facts ──────> Cache in DB → Choose amount → Save entry
      └─ Unknown product
          └─ Product front + nutrition label
              └─ Structured extraction with OpenAI
                  └─ User review → Private DB cache → Choose amount → Save entry
```

Nutrition values are copied into each intake entry as a snapshot. Editing a product later does
not change historical totals. Products registered through AI remain private to the user who
created them, even when public sign-up is enabled, and are not automatically exposed to other
users.

## Requirements

1. A bot token created by sending `/newbot` to `@BotFather` on Telegram
2. An [OpenAI API key](https://platform.openai.com/api-keys)
3. Docker Engine and the Docker Compose plugin for deployment to a personal server
4. VS Code and Python 3.11 or later for local development

The bot can start, accept manual entries, and query the database without an OpenAI key. Only
photo recognition for unknown products is disabled.

## Quick local setup

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m app.main
```

For the first run, leave `OWNER_TELEGRAM_ID=0` in `.env` and send `/whoami` to the bot. Put the
numeric ID from its reply into `.env`, then restart the bot. Until an owner is configured, the
bot blocks data-changing operations other than identifying the owner.

The required `.env` values look like this:

```dotenv
TELEGRAM_BOT_TOKEN=123456:telegram-token
OWNER_TELEGRAM_ID=123456789
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-5.6-terra
DATABASE_URL=sqlite+aiosqlite:///./data/calorie_bot.db
APP_TIMEZONE=Asia/Seoul
DATA_DIR=./data
```

In VS Code, open the repository, select `.venv/bin/python` as the Python interpreter, and run
`Calorie Telegram Bot` from Run and Debug. You can also open the included Dev Container if you
prefer not to install Python locally. Docker Desktop or OrbStack must already be installed for
the Dev Container option.

## Deploying to a personal server

For personal use, 1 vCPU and 1 GB of RAM is workable; 2 GB provides more headroom. No GPU,
static IP, or domain is required. Typical memory usage, including PostgreSQL, is a few hundred
megabytes. CPU usage briefly rises while resizing photos and decoding barcodes. AI inference
runs on OpenAI's servers.

```bash
cp .env.example .env
# Set the tokens, owner ID, OpenAI key, and POSTGRES_PASSWORD in .env
docker compose up -d --build
docker compose logs -f bot
curl http://127.0.0.1:8080/healthz
```

The provided `docker-compose.yml` deliberately applies these restrictions:

- The bot uses long polling instead of a webhook
- PostgreSQL is not exposed through a host port
- The health-check port is bound only to `127.0.0.1`
- Database data and temporary images are kept in Docker volumes
- The application container runs as a dedicated non-root user

Back up PostgreSQL before updating:

```bash
docker compose exec -T db pg_dump -U calorie calorie > calorie-backup.sql
docker compose up -d --build
```

## Usage

| Command | Example or description |
|---|---|
| `/ping` | Show bot and database status, internal latency, and process uptime |
| `/today` | Show today's calories, macros, and goals |
| `/recent` | Show the eight most recent entries |
| `/undo` | Mark the latest entry as undone without deleting it |
| `/goal` | `/goal 2000 250 130 60` (kcal, carbohydrates, protein, fat) |
| `/barcode` | `/barcode 8801234567890` |
| `/manual` | `/manual Chicken breast \| 165 \| 0 \| 31 \| 3.6` |
| `/cancel` | Cancel an in-progress photo recognition or amount entry |
| `/whoami` | Show your numeric Telegram user ID |

Send photos in this order:

1. Photograph the barcode so that it fills most of the frame.
2. If it is a new product, photograph the front with the product name and total quantity visible.
3. Photograph the nutrition label with calories, carbohydrates, protein, fat, and the declared
   measurement basis visible.
4. Review the basis, total quantity, and values extracted by AI, then choose the save button.
5. Choose the whole package, half, or one serving, or enter the actual amount consumed.

Custom input supports forms such as `45g`, `250ml`, `2개` (two items), `0.5봉` (half a package),
`70%`, and `절반` (half). For example, if a product lists nutrition per `30 g` and contains
`90 g` in total, selecting half makes the bot use `45 g` and a multiplier of `1.5`. Item-based
input is available only when the bot has read the total item count from the packaging. The bot
does not perform unsupported conversions between units based on an assumed density.

If units conflict—for example, Open Food Facts expresses a drink's nutrition per `g` while the
package volume is in `ml`—the bot does not convert automatically. Instead, it asks the user to
confirm the package-label basis. A confirmed unit correction is stored as private product data
and reused for later entries.

## OpenAI API usage and cost controls

The implementation uses image input and strict JSON Schema output through the OpenAI Responses
API.

- Product-front photos use `detail=low` because they are used mainly to identify the product name
- Nutrition-label photos use `detail=original` to preserve small text
- Images are resized to a maximum long edge of 1,800 px, with EXIF data and metadata removed
- Responses are sent with `store=false`
- The model returns calories, macros, and the measurement basis as constrained JSON rather than
  free-form text
- The bot warns about numerical inconsistencies or low confidence and always requires user review
- Confirmed results are stored in the database, so the same barcode does not trigger another API
  request

API pricing changes by model and over time. Check the
[OpenAI pricing page](https://openai.com/api/pricing/) for current rates. You can select a model
with `OPENAI_MODEL` in `.env`. The implementation follows the
[Responses API reference](https://developers.openai.com/api/reference/resources/responses/methods/create)
and the [image input guide](https://developers.openai.com/api/docs/guides/images-vision).

## Tests and quality checks

The test suite covers calculations, image resizing, AI JSON validation, public catalog
conversion, database caching, user isolation, daily totals, and undo behavior without making
real Telegram or OpenAI requests.

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest -q
```

## Known limitations

- A barcode does not contain nutrition facts, so a public database or photos of the packaging are
  still required.
- Coverage of Korean products in Open Food Facts is inconsistent.
- AI OCR can be wrong and is not appropriate for medical or therapeutic nutrition management.
- For labels with multiple columns, verify that the model selected the intended `per serving` or
  `per 100 g` column.
- The bot removes photos after successful completion, cancellation, or failure, and cleans up
  temporary photos older than 24 hours at startup. Excluding the photo directory from disk
  backups is still recommended.
- The MVP creates tables automatically at startup and adds current fields through compatibility
  migrations. Before opening the service to the public, migrate to versioned schema migrations
  with Alembic.

## Before opening the service to the public

Setting `PUBLIC_SIGNUP=true` is enough for a limited public test, but the following should be in
place before serving unrestricted users:

- Per-user daily AI quotas and Telegram request rate limits
- Terms of service, a privacy policy, and account and photo deletion policies
- Administrative review and version history for promoting user-created products to a shared
  catalog
- Automated PostgreSQL backups, error tracking, and API usage and cost monitoring
- Scheduled temporary-image expiration and, if needed, S3-compatible object storage
- An adapter for Korean Ministry of Food and Drug Safety or public-data APIs, with a review of
  their licensing terms

## License and third-party rights

The original code and documentation in this repository are distributed under the
[Apache License 2.0](LICENSE). They may be used, modified, and redistributed for personal or
commercial purposes. The license grants copyright and patent permissions and includes warranty
and liability limitations, subject to its conditions. Redistributions must preserve the license
and copyright notices and identify modified files.

This license does not relicense Open Food Facts data or product images, Telegram or OpenAI
services, product names or trademarks, or user-supplied photos. Open Food Facts data is subject
to separate terms, including the ODbL. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for
the detailed separation of rights and the licenses of direct dependencies.

The code keeps `catalog.py` separate from `ai_recognition.py`, so additional public-data sources
can be added with minimal changes to the Telegram commands and database recording logic.
