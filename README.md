# Telegram 칼로리 기록 봇

**한국어** | [English](README.en.md)

혼자 쓰는 것을 우선으로 만든 포장식품 칼로리·탄단지 기록 봇입니다. 바코드를 보내면
저장된 상품을 재사용하고, 처음 보는 상품만 제품 앞면과 영양정보 표를 OpenAI 비전 모델로
읽습니다. AI 결과는 사용자가 확인한 뒤 기록 및 캐시됩니다.

## 지금 되는 것

- 바코드 사진 또는 숫자 입력
- 로컬 DB → Open Food Facts → AI 사진 인식 순서의 상품 조회
- kcal, 탄수화물, 단백질, 지방 기록
- 제품별 전부·절반·1회분·기준량 버튼과 직접 섭취량 입력
- `g`, `ml`, `개`, `봉`, `%` 단위 환산과 지난 섭취량 재사용
- 오늘 합계, 최근 기록, 마지막 기록 취소, 일일 목표
- 직접 입력
- 개인 사용자 잠금 및 공개 가입 전환 옵션
- SQLite 로컬 실행, PostgreSQL Docker 운영
- 롱폴링 방식이라 도메인·TLS·공개 웹 포트 불필요
- DB 상태를 확인하는 `GET /healthz`
- 재시작 후에도 남는 크기 제한 순환 로그

## 처리 흐름

```text
Telegram 사진
  └─ 바코드 인식
      ├─ 내 DB에 있음 ────────────────> 섭취량 선택 → 기록
      ├─ Open Food Facts에 있음 ──────> DB 캐시 → 섭취량 선택 → 기록
      └─ 처음 보는 상품
          └─ 앞면 + 영양정보 표
              └─ OpenAI 구조화 인식
                  └─ 사용자 확인 → 개인 DB 캐시 → 섭취량 선택 → 기록
```

영양값은 섭취 기록에도 스냅샷으로 복사됩니다. 나중에 상품 정보를 수정해도 과거 합계가
바뀌지 않습니다. AI로 등록한 상품은 공개 모드에서도 등록자 개인 데이터로 저장되어 다른
사용자에게 자동 노출되지 않습니다.

## 준비물

1. Telegram에서 `@BotFather`에게 `/newbot`을 보내 발급한 봇 토큰
2. [OpenAI API 키](https://platform.openai.com/api-keys)
3. 개인 서버 운영 시 Docker Engine과 Docker Compose 플러그인
4. 로컬 개발 시 VS Code와 Python 3.11 이상

OpenAI 키 없이도 봇 시작, 수동 기록, DB 조회는 됩니다. 처음 보는 상품의 사진 인식만
비활성화됩니다.

## 가장 빠른 로컬 실행

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m app.main
```

처음에는 `.env`의 `OWNER_TELEGRAM_ID=0`으로 실행한 뒤 봇에 `/whoami`를 보냅니다. 응답한
숫자를 `.env`에 넣고 봇을 재시작하세요. 그 전에는 소유자 확인 외의 데이터 변경을 막습니다.

필수 `.env` 예시는 다음과 같습니다.

```dotenv
TELEGRAM_BOT_TOKEN=123456:telegram-token
OWNER_TELEGRAM_ID=123456789
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-5.6-terra
DATABASE_URL=sqlite+aiosqlite:///./data/calorie_bot.db
APP_TIMEZONE=Asia/Seoul
DATA_DIR=./data
LOG_LEVEL=INFO
LOG_MAX_BYTES=10485760
LOG_BACKUP_COUNT=10
```

VS Code에서는 폴더를 연 뒤 Python 인터프리터로 `.venv/bin/python`을 선택하고, Run and Debug의
`Calorie Telegram Bot`을 실행하면 됩니다. Python 설치를 피하고 싶다면 제공된 Dev Container를
열어도 됩니다. 단, Docker Desktop 또는 OrbStack은 먼저 설치되어 있어야 합니다.

## 개인 서버에 배포

서버 사양은 개인용 기준 1 vCPU, RAM 1 GB도 가능하고 2 GB면 여유가 있습니다. GPU, 고정 IP,
도메인은 필요 없습니다. PostgreSQL까지 포함한 평상시 메모리는 대략 수백 MB 수준이며, CPU는
사진 리사이즈와 바코드 판독 때 잠깐 사용됩니다. AI 추론은 OpenAI 서버에서 수행됩니다.

```bash
cp .env.example .env
# .env의 토큰, 소유자 ID, OpenAI 키, POSTGRES_PASSWORD를 수정
docker compose up -d --build
docker compose logs -f bot
curl http://127.0.0.1:8080/healthz
```

`docker-compose.yml`은 다음을 의도적으로 제한합니다.

- 봇은 webhook이 아니라 long polling 사용
- PostgreSQL은 호스트 포트로 공개하지 않음
- 상태 확인 포트는 `127.0.0.1`에만 바인딩
- DB와 임시 이미지 디렉터리는 Docker volume에 보존
- 컨테이너는 root가 아닌 전용 사용자로 실행

업데이트 전에는 PostgreSQL을 백업하세요.

```bash
docker compose exec -T db pg_dump -U calorie calorie > calorie-backup.sql
docker compose up -d --build
```

실행 로그는 콘솔과 `DATA_DIR/logs/calorie-bot.log`에 동시에 기록됩니다. Docker에서는
`/data/logs/calorie-bot.log`가 `calorie_data` 볼륨에 저장되므로 컨테이너를 다시 만들거나 서버를
재시작해도 유지됩니다. 기본값은 파일당 10 MiB이며 이전 파일 10개를 보관합니다. Docker 자체의
표준 출력 로그도 서비스별 10 MiB 파일 3개로 제한됩니다.

```bash
# Docker의 현재 실행 로그
docker compose logs -f bot

# 영속 파일 로그
docker compose exec bot tail -f /data/logs/calorie-bot.log

# 순환 파일 목록과 용량
docker compose exec bot ls -lh /data/logs
```

로그 볼륨까지 별도로 백업하려면 다음처럼 호스트로 복사할 수 있습니다.

```bash
docker compose cp bot:/data/logs ./calorie-logs
```

## 사용법

| 명령 | 예시 또는 설명 |
|---|---|
| `/ping` | 봇·DB 상태, 내부 처리 시간, 프로세스 가동 시간 확인 |
| `/today` | 오늘 kcal·탄단지와 목표 표시 |
| `/recent` | 최근 8건 |
| `/undo` | 마지막 기록을 삭제하지 않고 취소 상태로 전환 |
| `/goal` | `/goal 2000 250 130 60` (kcal·탄·단·지) |
| `/barcode` | `/barcode 8801234567890` |
| `/manual` | `/manual 닭가슴살 | 165 | 0 | 31 | 3.6` |
| `/cancel` | 진행 중인 사진 인식 또는 섭취량 입력 취소 |
| `/whoami` | 내 Telegram 숫자 ID 확인 |

사진은 다음 순서로 보냅니다.

1. 바코드가 화면 대부분을 차지하도록 촬영
2. 처음 보는 상품이면 제품명과 총 내용량이 보이는 앞면 촬영
3. kcal·탄수화물·단백질·지방과 표시 기준이 모두 보이는 영양정보 표 촬영
4. AI가 표시한 기준·총 내용량·숫자를 직접 확인한 뒤 저장 버튼 선택
5. 전부·절반·1회분 버튼을 선택하거나 실제 먹은 양을 직접 입력

직접 입력은 `45g`, `250ml`, `2개`, `0.5봉`, `70%`, `절반` 형식을 지원합니다. 예를 들어
영양정보가 `30 g당`이고 총 내용량이 `90 g`인 상품에서 `절반`을 선택하면, 봇은 섭취량
`45 g`과 계산 배수 `1.5`를 자동으로 적용합니다. `개` 단위는 포장지에서 총 낱개 수를 읽은
상품에서만 사용할 수 있으며, 단위 사이의 근거 없는 밀도 환산은 하지 않습니다.
Open Food Facts의 영양 기준이 `g`인데 실제 음료 포장은 `ml`인 것처럼 단위가 충돌하면,
봇은 자동 환산하지 않고 포장지 기준을 확인받습니다. 사용자가 확인한 단위 보정은 개인 상품
정보로 저장되어 다음 기록부터 재사용됩니다.

## AI API 사용 방식과 비용 제어

구현은 OpenAI Responses API의 이미지 입력과 strict JSON schema 출력을 사용합니다.

- 앞면은 제품명 확인용이라 `detail=low`
- 작은 글자가 있는 영양정보 표는 `detail=original`
- 전송 전 긴 변을 최대 1,800 px로 줄이고 EXIF 및 메타데이터 제거
- 응답 저장은 `store=false`
- 자유 문장이 아니라 kcal·탄단지·표시 기준을 정해진 JSON으로만 수신
- 수치 불일치 및 낮은 신뢰도 경고 후 반드시 사용자 확인
- 확인한 결과를 DB에 넣어 같은 바코드는 다시 호출하지 않음

정확한 API 단가는 모델과 시점에 따라 바뀌므로 [OpenAI 가격 페이지](https://openai.com/api/pricing/)에서
확인하세요. 모델은 `.env`의 `OPENAI_MODEL`로 교체할 수 있습니다. 관련 구현은
[Responses API 문서](https://developers.openai.com/api/reference/resources/responses/methods/create)와
[이미지 입력 가이드](https://developers.openai.com/api/docs/guides/images-vision)를 기준으로 했습니다.

## 테스트와 품질 검사

실제 Telegram/OpenAI 호출 없이 계산, 이미지 축소, AI JSON 검증, 공개 카탈로그 변환, DB 캐시,
사용자 격리, 일일 합계, 취소를 검사합니다.

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest -q
```

## 알려진 한계

- 바코드 자체에는 영양성분이 없으므로 공개 DB나 포장지 사진이 필요합니다.
- Open Food Facts의 한국 상품 커버리지는 일정하지 않습니다.
- AI OCR은 틀릴 수 있어 의료·치료 목적의 정밀 영양 관리에는 적합하지 않습니다.
- 영양정보 표가 여러 열이면 `1회 제공량당`과 `100 g당` 열을 혼동하지 않았는지 확인해야 합니다.
- 정상 완료·취소·오류 시 사진을 지우고 시작할 때 24시간 지난 임시 사진도 정리하지만, 디스크
  백업에는 사진 디렉터리를 포함하지 않는 편이 안전합니다.
- 실행 로그에는 요청 상태, 바코드가 포함된 공개 DB URL, Telegram 업데이트 ID, AI 토큰 사용량,
  오류 스택이 포함될 수 있으므로 로그 백업도 개인정보에 준해 관리해야 합니다.
- 테이블은 MVP에서 시작 시 자동 생성되고 현재 추가 필드는 호환 마이그레이션으로 보완됩니다.
  공개 서비스로 확장하기 전에는 Alembic 기반 버전 마이그레이션으로 전환해야 합니다.

## 공개 서비스로 확장할 때

`PUBLIC_SIGNUP=true`만으로 테스트 공개는 가능하지만, 불특정 다수에게 열기 전에는 다음이
필요합니다.

- 사용자별 AI 일일 한도와 Telegram 요청 rate limit
- 약관·개인정보 처리방침·계정 및 사진 삭제 정책
- 공유 상품 승격을 위한 관리자 검수 및 버전 이력 UI
- PostgreSQL 백업 자동화, 오류 추적, 사용량·API 비용 모니터링
- 임시 이미지 만료 작업과 필요 시 S3 호환 object storage
- 한국 식품의약품안전처/공공데이터 API 어댑터 및 라이선스 검토

## 라이선스와 제3자 권리

이 저장소의 원본 코드와 문서는 [Apache License 2.0](LICENSE)으로 배포합니다. 누구나
개인·상업 목적으로 사용하고, 수정하고, 재배포할 수 있으며 라이선스 조건에 따라 저작권·특허
허락과 보증·책임 제한이 적용됩니다. 재배포할 때는 라이선스와 저작권 고지를 보존하고 변경한
파일을 표시해야 합니다.

이 라이선스는 Open Food Facts 데이터·상품 이미지, Telegram/OpenAI 서비스, 상품명·상표,
사용자가 제공한 사진까지 다시 허락하는 것은 아닙니다. Open Food Facts 데이터는 ODbL 등 별도
조건이 적용됩니다. 자세한 구분과 직접 의존성 라이선스는
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)를 확인하세요.

현재 구조는 `catalog.py`와 `ai_recognition.py`가 분리되어 있어 공공데이터 소스를 추가해도 Telegram
명령과 DB 기록 코드는 거의 바뀌지 않습니다.
