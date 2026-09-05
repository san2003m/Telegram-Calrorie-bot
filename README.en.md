# Telegram Calorie Tracker Bot

[한국어](README.md) | **English**

A personal-first Telegram bot for tracking calories and macros in packaged and general foods. Send a
barcode to reuse a saved product. For products the bot has not seen before, it reuses the first
barcode photo to identify the product and nutrition facts with an OpenAI vision model, requesting
only the missing package view when necessary. AI-extracted data is recorded and cached only after
the user reviews it.

## Features

- Scan a barcode from a photo or enter its digits manually
- Look up products in the local database, then Open Food Facts, then from photos with AI
- Search saved products by name, brand, barcode, or Korean/Japanese related tags
- Search general foods through MFDS with commands such as `/food 삶은 달걀` and cache results
- Search official restaurant-menu nutrition with `/menu brand menu size`
- Calculate recipe totals and per-serving calories and macros from ingredients with `/recipe`
- Track calories, carbohydrates, protein, and fat
- Detect Korean and Japanese nutrition-label formats and preserve the original basis text
- Distinguish Korean sodium from Japanese salt equivalent and show derived conversions
- Choose the whole package, half, one serving, or the nutrition-label basis, or enter a custom
  amount
- Convert `g`, `ml`, item, package, and `%` units, and reuse the last amount consumed
- View today's totals, reopen recent products with buttons, undo the latest entry, and set daily goals
- View today's goals, 7-day and 30-day trends, and recent entries in a private web dashboard
- Add entries manually
- Lock the bot to one owner or optionally allow public sign-up
- Run locally with SQLite or in Docker with PostgreSQL
- Use Telegram long polling, with no domain, TLS certificate, or public web port required
- Check status through `GET /healthz` and use the read-only dashboard at `GET /dashboard`
- Preserve size-limited rotating logs across restarts

## How it works

```text
Telegram photo
  └─ Decode barcode
      ├─ Found in local DB ─────────────> Choose amount → Save entry
      ├─ Found in Open Food Facts ──────> Cache in DB → Choose amount → Save entry
      └─ Unknown product
          └─ Analyze first photo → request only missing views
              └─ Structured extraction from 1–3 photos with OpenAI
                  └─ User review → Private DB cache → Choose amount → Save entry

Telegram /search product name or plain text while idle
  └─ Search names, brands, and Korean/Japanese tags in own products + public local DB
      └─ Select product → Choose amount → Save entry

Telegram /food name
  └─ Local cache → MFDS Food Nutrition Database
      └─ Select food → Reference serving/piece or custom grams → Save entry

Telegram /menu brand menu size
  └─ Saved official menu → otherwise one limited OpenAI web search
      └─ Verify official source and required values → User review → Cache → Save entry

Telegram /recipe name
  └─ Line parser → OpenAI ingredient extraction only when needed
      └─ Local/MFDS matching → User review → Save as one-serving product → Save entry
```

Nutrition values are copied into each intake entry as a snapshot. Editing a product later does
not change historical totals. Products registered through AI remain private to the user who
created them, even when public sign-up is enabled, and are not automatically exposed to other
users.

## Requirements

1. A bot token created by sending `/newbot` to `@BotFather` on Telegram
2. An [OpenAI API key](https://platform.openai.com/api-keys)
3. An [MFDS Food Nutrition Database API key](https://www.data.go.kr/data/15127578/openapi.do?recommendDataYn=Y) for general-food search
4. Docker Engine and the Docker Compose plugin for deployment to a personal server
5. VS Code and Python 3.11 or later for local development

The bot can start, accept manual entries, query the database, and parse line-based recipes without
an OpenAI key. Only unknown-product photo recognition, natural-language recipe extraction, and new
`/menu` searches are disabled. Without `MFDS_API_KEY`, new `/food` and recipe-ingredient searches
are disabled, while
previously cached foods remain reusable.

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
OPENAI_RECIPE_MODEL=gpt-5.6-luna
RECIPE_AI_DAILY_LIMIT=10
RECIPE_AI_MONTHLY_LIMIT=100
RECIPE_AI_MAX_INPUT_CHARS=2000
RECIPE_AI_MAX_OUTPUT_TOKENS=800
RECIPE_MAX_INGREDIENTS=20
RECIPE_AI_COOLDOWN_SECONDS=10
MFDS_API_KEY=your-data-go-kr-service-key
MFDS_API_TIMEOUT_SECONDS=8
DATABASE_URL=sqlite+aiosqlite:///./data/calorie_bot.db
APP_TIMEZONE=Asia/Seoul
DATA_DIR=./data
LOG_LEVEL=INFO
LOG_MAX_BYTES=10485760
LOG_BACKUP_COUNT=10
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

The private dashboard runs on the same port 8080 and is bound only to `127.0.0.1` on the
server. You can access it from your Mac through an SSH tunnel without a domain or router port
forwarding:

```bash
ssh -N -L 8080:127.0.0.1:8080 calorie-server
# Open http://127.0.0.1:8080/dashboard in a browser
```

If local port 8080 is already in use, change only the local port on the left:

```bash
ssh -N -L 18080:127.0.0.1:8080 calorie-server
# Open http://127.0.0.1:18080/dashboard in a browser
```

The dashboard reads only the user configured by `OWNER_TELEGRAM_ID`. It shows today's calorie
and macro targets, 7-day and 30-day calorie trends, the latest 12 entries, and each nutrition
source. It refreshes every 60 seconds and also provides a manual refresh button.

### External access through Cloudflare Access

For authenticated external HTTPS access, use Cloudflare Access with a remotely-managed Tunnel.
Create the Access application and its allow policy before attaching the Tunnel's public hostname.
Creating the public route first can expose the dashboard without authentication until the Access
policy is applied.

1. Add `calorie.example.com` as a self-hosted Cloudflare Access application.
2. Allow one email address and choose an authentication method such as email OTP.
3. Create a remotely-managed Tunnel and set the published application's service URL to
   `http://bot:8080`.
4. Copy `.env.tunnel.example` to `.env.tunnel` and put the token in `TUNNEL_TOKEN=...`.
5. Start the Tunnel profile only after the token is ready.

```bash
docker compose --profile tunnel up -d cloudflared
docker compose --profile tunnel logs -f cloudflared
```

Only `cloudflared` reads `.env.tunnel`; the bot container does not receive it. `cloudflared` reaches
`bot:8080` over the internal Compose network and does not open another host port. Anyone with the
token can run the Tunnel, so never put it in Git, chat, or logs, and rotate it
immediately if exposed. A regular `docker compose up -d` does not start the optional `tunnel`
profile, so local and private deployments without a token are unaffected.

The provided `docker-compose.yml` deliberately applies these restrictions:

- The bot uses long polling instead of a webhook
- PostgreSQL is not exposed through a host port
- The health-check and dashboard port is bound only to `127.0.0.1`
- Database data and temporary images are kept in Docker volumes
- The application container runs as a dedicated non-root user

Back up PostgreSQL before updating:

```bash
docker compose exec -T db pg_dump -U calorie calorie > calorie-backup.sql
docker compose up -d --build
```

Runtime logs are written to both the console and `DATA_DIR/logs/calorie-bot.log`. Under Docker,
`/data/logs/calorie-bot.log` is stored in the persistent `calorie_data` volume, so it survives
container replacement and server restarts. By default, each file is limited to 10 MiB and ten
old files are retained. Docker's own standard-output logs are also limited to three 10 MiB files
per service.

```bash
# Current Docker runtime logs
docker compose logs -f bot

# Persistent file log
docker compose exec bot tail -f /data/logs/calorie-bot.log

# Rotated files and their sizes
docker compose exec bot ls -lh /data/logs
```

To copy the persistent logs to the host for a separate backup:

```bash
docker compose cp bot:/data/logs ./calorie-logs
```

## Usage

| Command | Example or description |
|---|---|
| `/ping` | Show bot and database status, internal latency, and process uptime |
| `/today` | Show today's calories, macros, and goals |
| `/recent` | Show the eight most recent entries |
| `/undo` | Mark the latest entry as undone without deleting it |
| `/goal` | `/goal 2000 250 130 60` (kcal, carbohydrates, protein, fat) |
| `/search` | Search saved products, for example `/search 닭가슴살` |
| `/food` | Search general foods, for example `/food 삶은 달걀` |
| `/menu` | Search official brand nutrition, for example `/menu Starbucks Cafe Latte Tall` |
| `/recipe` | Send `/recipe 김치볶음밥`, then ingredients and total servings |
| `/barcode` | `/barcode 8801234567890` |
| `/manual` | `/manual Chicken breast \| 165 \| 0 \| 31 \| 3.6` |
| `/cancel` | Cancel an in-progress photo recognition or amount entry |
| `/whoami` | Show your numeric Telegram user ID |

Use `/search product name` to reuse any product you previously registered or cached. When no other
input is in progress, sending only the product name performs the same search. In addition to names,
brands, and barcodes, the bot searches shared food concepts and Korean/Japanese aliases. For
example, a Japanese product named `ゆで卵` can be found with `계란`. A tag-only match shows its
reason, such as `#계란`. Results are limited to your private products and public catalog products,
and the search does not call OpenAI or an external API. Existing products receive deterministic
tags from their names and cached catalog data at startup. Product buttons under `/recent` reopen
the amount picker directly.

Photo input works as follows:

1. Keep the barcode sharp, but do not fill the entire frame with it. Include the product name or
   nutrition label in the first photo when practical.
2. For a new product, the bot checks that same photo for the name, total quantity, and nutrition
   facts instead of discarding it.
3. If the photo is sufficient, the bot immediately shows the review screen. Otherwise it asks only
   for the missing product-front or nutrition-label view.
4. You may also send up to three package views at once as a Telegram album. The nutrition photo
   must clearly show calories, carbohydrates, protein, fat, and the declared measurement basis.
5. Review the detected market, original basis text, total quantity, and extracted values. If the
   market is wrong, select `🇰🇷 한국` or `🇯🇵 일본` before saving.
6. Choose the whole package, half, or one serving, or enter the actual amount consumed.

Custom input supports forms such as `45g`, `250ml`, `2개` (two items), `0.5봉` (half a package),
`70%`, and `절반` (half), as well as Japanese forms including `2個`, `1本`, `0.5袋`, `1食分`,
and `半分`. For example, if a product lists nutrition per `30 g` and contains
`90 g` in total, selecting half makes the bot use `45 g` and a multiplier of `1.5`. Item-based
input is available only when the bot has read the total item count from the packaging. The bot
does not perform unsupported conversions between units based on an assumed density.

If units conflict—for example, Open Food Facts expresses a drink's nutrition per `g` while the
package volume is in `ml`—the bot does not convert automatically. Instead, it asks the user to
confirm the package-label basis. A confirmed unit correction is stored as private product data
and reused for later entries.

Use `/food name` for non-packaged foods. The bot checks its local cache first and otherwise shows
up to five matches from the MFDS Food Nutrition Database. Selected records are cached by their
official food code. A reference-serving or piece button is shown only when the official response
contains enough weight information for the conversion; otherwise the bot asks for grams instead
of inventing a piece weight. Cooking method, moisture, and actual size can still change the result.

For a recipe, send `/recipe name`, then lines such as `rice 420g`, `kimchi 160g`, `egg 2개`, and
`총 2인분`. This structured form does not call OpenAI. For free-form text, OpenAI extracts only
ingredient names, amounts, and units into strict JSON; food lookup and arithmetic remain in the
MFDS-backed Python code. After reviewing every ingredient match, save the recipe as a private
one-serving product so existing intake buttons, daily totals, recent entries, and the dashboard can
reuse it. Tablespoons, teaspoons, and cups are converted to volume only; the bot does not invent a
volume-to-weight density when the selected food is measured in grams.

Use `/menu brand menu size` for restaurant and cafe items. Each request allows at most one OpenAI
web-search call. A result is shown only when one first-party brand page or official PDF contains the
exact menu and size, serving basis, calories, carbohydrates, protein, and fat. Blogs, delivery apps,
and crowd-sourced nutrition databases are rejected, and missing values are never estimated. The
user reviews the official URL and numbers before saving. Saved results are reused by query hash;
unsuccessful searches are also cached for seven days by default to prevent repeated credit use.

The Japanese `食塩相当量` value is not sodium itself. The bot preserves the value printed on the
package, derives sodium using `sodium (mg) = salt equivalent (g) × 1000 ÷ 2.54`, and explicitly
marks the result as derived. If a Korean label contains only sodium, the inverse conversion is
used. Japanese `糖質`, `糖類`, and `食物繊維` remain distinct from total `炭水化物` and are not
added to carbohydrates again. Barcode prefixes are not used to determine the label market.

The implementation refers to the Korean Ministry of Food and Drug Safety's
[nutrition-labeling guide](https://www.mfds.go.kr/brd/m_1060/view.do?seq=15190) and the Japanese
Consumer Affairs Agency's
[nutrition-labeling guide](https://www.caa.go.jp/policies/policy/food_labeling/health_promotion/assets/food_labeling_cms206_20210318_01.pdf).
Regulations can change, so recheck the latest standards before operating a public service.

## OpenAI API usage and cost controls

The implementation uses image input and strict JSON Schema output through the OpenAI Responses
API.

- The first unknown-barcode photo is reused immediately instead of being discarded
- One request can combine one to three photos regardless of their order
- Every photo uses `detail=original` because any package view may contain small nutrition text
- Images are resized to a maximum long edge of 1,800 px, with EXIF data and metadata removed
- Responses are sent with `store=false`
- The model returns calories, macros, and the measurement basis as constrained JSON rather than
  free-form text
- The same request extracts the Korean or Japanese format, original basis text, sodium, and salt
  equivalent
- The same request also returns constrained food concepts and Korean/Japanese search tags, so no
  additional AI request is made
- The model may not infer health, dieting, or disease-suitability tags; nutrition claims are kept
  only when they are explicit on the package
- AI returns only values printed on the label; deterministic Python code performs salt conversion
- The bot warns about numerical inconsistencies or low confidence and always requires user review
- Confirmed results are stored in the database, so the same barcode does not trigger another API
  request
- A single photo completes recognition when both the product name and required nutrition values are
  readable; otherwise the bot requests only the missing view
- Structured recipe lines avoid OpenAI; free-form recipes make at most one model call per recipe
- Recipe input is capped at 2,000 characters, 20 ingredients, and 800 output tokens
- Per-user limits default to 10 calls per day, 100 per month, a 10-second cooldown, and one
  concurrent request
- Identical recipe input reuses a per-user hash cache, and actual input/output token usage is stored
- The recipe model receives no web, file, code-execution, or other tools
- Menu lookup uses a low-cost model, low search context, one web-search call, and 900 output tokens
- Menu limits default to 5 calls per day and 50 per month per user, plus 20 per day and 200 per month
  for the whole service
- Each user also has a 15-second cooldown and one concurrent call
- A result can be saved only when its official URL appears in the actual web-search sources and all
  required nutrition fields are present
- Successful and unsuccessful searches are cached for seven days, with token usage tracked

API pricing changes by model and over time. Check the
[OpenAI pricing page](https://openai.com/api/pricing/) for current rates. Select `OPENAI_MODEL` for
label photos, `OPENAI_RECIPE_MODEL` for natural-language recipes, and `OPENAI_MENU_MODEL` for menu
search. The implementation follows the
[Responses API reference](https://developers.openai.com/api/reference/resources/responses/methods/create)
and the [image input guide](https://developers.openai.com/api/docs/guides/images-vision).

## Tests and quality checks

The test suite covers calculations, image resizing, AI JSON validation, Korean/Japanese tag
normalization and cross-language search, recipe parsing, quotas and caching, public catalog
conversion, user isolation, daily totals, and undo behavior without making real Telegram or OpenAI
requests.

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest -q
```

## Known limitations

- A barcode alone contains no nutrition data, so another package view is still required when the
  first photo does not show a readable product name and nutrition label.
- Coverage of Korean products in Open Food Facts is inconsistent.
- Specialized food terms and new slang outside the built-in Korean/Japanese tag dictionary require
  a matching product name or an alias saved during AI recognition.
- General-food search requires an approved public-data API key in `MFDS_API_KEY`.
- Piece and serving shortcuts are offered only when the official data provides conversion evidence.
- Recipe totals add the entered amounts from the selected food records. They do not automatically
  account for discarded oil or broth, cooking loss, or moisture changes; review every match.
- AI OCR can be wrong and is not appropriate for medical or therapeutic nutrition management.
- Korean/Japanese detection is advisory and must be reviewed before saving.
- Values marked `推定値`, `目安`, or as estimates on Korean labels generate a warning but are not
  replaced with laboratory values.
- For labels with multiple columns, verify that the model selected the intended `per serving` or
  `per 100 g` column.
- The bot removes photos after successful completion, cancellation, or failure, and cleans up
  temporary photos older than 24 hours at startup. Excluding the photo directory from disk
  backups is still recommended.
- Runtime logs may contain request status, public-database URLs with barcodes, Telegram update
  IDs, AI token usage, and error traces. Treat log backups as potentially sensitive data.
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
- MFDS production-account approval and API-usage monitoring

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
