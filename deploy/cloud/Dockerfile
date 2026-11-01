FROM python:3.13.18-slim-bookworm@sha256:70bda1e795ded2a684ef9815955e05829b14ee5bb5c3eb2a940648e88f10fce9 AS sqlite-build
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY deploy/cloud/build_sqlite.py .
RUN python build_sqlite.py

FROM python:3.13.18-slim-bookworm@sha256:70bda1e795ded2a684ef9815955e05829b14ee5bb5c3eb2a940648e88f10fce9
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    STOCKAI_CLOUD_DATA_DIR=/var/lib/stockai/live STOCKAI_ERASURE_DIR=/var/lib/stockai-erasures \
    STOCKAI_MASTER_KEY_FILE=/run/stockai/keys.json STOCKAI_AGE_BIN=/usr/local/bin/age
COPY --from=sqlite-build /build/libsqlite3.so.0 /usr/local/lib/libsqlite3.so.0
RUN ldconfig && python -c "import sqlite3; assert sqlite3.sqlite_version=='3.54.1',sqlite3.sqlite_version"
WORKDIR /app
COPY requirements-cloud.lock .
RUN pip install --no-cache-dir -r requirements-cloud.lock
COPY deploy/cloud/install_age.py /tmp/install_age.py
RUN python /tmp/install_age.py && rm /tmp/install_age.py
COPY backend ./backend
COPY config/prompt-standard.md config/prompt-full.md ./config/
COPY frontend/dist-public ./frontend/dist-public
RUN test -f frontend/dist-public/index.html && useradd --uid 10001 --create-home stockai \
    && mkdir -p /var/lib/stockai /var/lib/stockai-erasures /run/stockai \
    && chown -R stockai:stockai /var/lib/stockai /var/lib/stockai-erasures /run/stockai
USER 10001:10001
EXPOSE 8766
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import os,urllib.request,urllib.parse; r=urllib.request.Request('http://127.0.0.1:8766/api/health',headers={'Host':urllib.parse.urlsplit(os.environ['STOCKAI_PUBLIC_URL']).netloc}); urllib.request.urlopen(r,timeout=3)"
CMD ["python","-m","uvicorn","backend.public_app:app","--host","0.0.0.0","--port","8766","--workers","1","--proxy-headers","--forwarded-allow-ips","*"]
