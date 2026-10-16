# syntax=docker/dockerfile:1
# Mevzuat Takip — Docker imajı (Debian 12 tabanlı, hedef sunucuyla aynı aile).
#
# Üç aşama:
#   taban     : Python + Tesseract (Türkçe) + uv + bağımlılıklar + kod
#   test      : geliştirme bağımlılıkları + BÜTÜN testler Linux'ta çalışır (gerçek Tesseract ile OCR testleri dahil)
#               docker build --target test .        → testlerden biri bile kalırsa derleme durur
#   uygulama  : çalışan imaj (root olmayan kullanıcı); docker compose bunu kullanır
#
# Arayüz (React) burada DERLENMEZ: frontend/dist depoda hazır gelir (imajda Node yok).
# Playwright ve Chromium KURULMAZ: tarayıcılı kaynak tipi (tarayici) şu an kullanılmıyor (isteğe bağlı ek).
#   Kullanılırsa: iki `uv sync` satırına `--extra tarayici` eklenir ve ardından
#   RUN playwright install --with-deps chromium   (imaj ~400 MB büyür).
# uv'nin indirme önbelleği derleme sırasında geçici bağlanır (--mount=type=cache): imaja girmez (~290 MB tasarruf),
#   ama sonraki derlemelerde paketler yeniden indirilmez.

FROM python:3.12-slim-bookworm AS taban

# PYTHONUNBUFFERED: loglar beklemeden `docker logs`'a düşsün.
# UV_*: bağımlılıkları sistem Python'una (imajdaki 3.12) kur, uv kendi Python'unu indirmesin.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Istanbul \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Sistem paketleri: OCR için Tesseract + Türkçe dil dosyası; saat dilimi verisi.
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-tur tzdata \
    && rm -rf /var/lib/apt/lists/*

# uv'nin resmi imajından sadece çalıştırılabilir dosya alınır (sürüm sabit: her derlemede aynı araç).
COPY --from=ghcr.io/astral-sh/uv:0.11.2 /uv /usr/local/bin/uv

WORKDIR /app

# Önce sadece bağımlılık listesi: kod değişince bu katman önbellekten gelir, paketler yeniden kurulmaz.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY src ./src
COPY config ./config
COPY frontend/dist ./frontend/dist
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"


FROM taban AS test
COPY tests ./tests
# Arayüz eskime testi kaynak dosyalarla dist'i karşılaştırır: frontend kaynağı da gerekir (.dockerignore node_modules'u dışlar).
COPY frontend ./frontend
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen
RUN python -m pytest -q


FROM taban AS uygulama
# Root olmayan kullanıcı: uygulamada bir açık olsa bile container içinde sistem dosyalarına yazamaz.
RUN useradd --system --uid 10001 --no-create-home --home-dir /app mevzuat \
    && mkdir -p /app/giden_mailler \
    && chown mevzuat:mevzuat /app/giden_mailler
USER mevzuat
# Dosya kilidi (aynı anda tek günlük iş) yazılabilir bir yerde olmalı.
ENV MEVZUAT_KILIT=/tmp/mevzuat.lock
EXPOSE 8000
# Container içinde 0.0.0.0 şart (dışarıdan port yönlendirmesi gelir). Dışarıya açılması compose'daki
# "127.0.0.1:8000:8000" ile sınırlanır: sadece bu makineden erişilir.
CMD ["python", "-m", "mevzuat.cli", "panel", "--host", "0.0.0.0", "--port", "8000"]
