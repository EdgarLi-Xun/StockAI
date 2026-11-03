FROM python:3.13.18-slim-bookworm@sha256:70bda1e795ded2a684ef9815955e05829b14ee5bb5c3eb2a940648e88f10fce9 AS sqlite-build
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY deploy/cloud/build_sqlite.py .
RUN python build_sqlite.py
FROM python:3.13.18-slim-bookworm@sha256:70bda1e795ded2a684ef9815955e05829b14ee5bb5c3eb2a940648e88f10fce9
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 STOCKAI_HUB_DATA_DIR=/var/lib/stockai-hub/live STOCKAI_HUB_CONTROL_DIR=/var/lib/stockai-hub-control STOCKAI_AGE_BIN=/usr/local/bin/age
COPY --from=sqlite-build /build/libsqlite3.so.0 /usr/local/lib/libsqlite3.so.0
RUN ldconfig
WORKDIR /app
COPY requirements-hub.lock .
RUN pip install --no-cache-dir -r requirements-hub.lock
COPY deploy/cloud/install_age.py /tmp/install_age.py
RUN python /tmp/install_age.py && rm /tmp/install_age.py
COPY backend ./backend
RUN useradd --uid 10001 --create-home stockai && mkdir -p /var/lib/stockai-hub/live /var/lib/stockai-hub-control && chown -R stockai:stockai /var/lib/stockai-hub /var/lib/stockai-hub-control
USER 10001:10001
EXPOSE 8770
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8770/v1/health',timeout=3)"
CMD ["python","-m","backend.upgrades.cli","serve","--component","hub","--port","8770"]
