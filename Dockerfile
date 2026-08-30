FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system calorie && useradd --system --gid calorie --home-dir /app calorie

COPY pyproject.toml README.md ./
COPY app ./app
RUN python -m pip install --upgrade pip && python -m pip install .

RUN mkdir -p /data && chown -R calorie:calorie /app /data
USER calorie

EXPOSE 8080
CMD ["python", "-m", "app.main"]
