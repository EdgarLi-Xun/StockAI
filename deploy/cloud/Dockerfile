# 第一阶段编译经过固定版本与摘要核验的 SQLite 共享库。
FROM python:3.13.18-slim-bookworm@sha256:70bda1e795ded2a684ef9815955e05829b14ee5bb5c3eb2a940648e88f10fce9 AS sqlite-build
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY deploy/cloud/build_sqlite.py .
RUN python build_sqlite.py

# 最终镜像只复制已编译的 SQLite 库与运行依赖，构建工具留在上一阶段。
FROM python:3.13.18-slim-bookworm@sha256:70bda1e795ded2a684ef9815955e05829b14ee5bb5c3eb2a940648e88f10fce9
# 运行阶段固定持久化目录与加密工具路径，禁止生成字节码缓存。
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    STOCKAI_CLOUD_DATA_DIR=/var/lib/stockai/live STOCKAI_ERASURE_DIR=/var/lib/stockai-erasures \
    STOCKAI_MASTER_KEY_FILE=/run/stockai/keys.json STOCKAI_AGE_BIN=/usr/local/bin/age
# 使用构建出的 SQLite，并在镜像内核对实际加载版本。
COPY --from=sqlite-build /build/libsqlite3.so.0 /usr/local/lib/libsqlite3.so.0
RUN ldconfig && python -c "import sqlite3; assert sqlite3.sqlite_version=='3.54.1',sqlite3.sqlite_version"
WORKDIR /app
# 先安装锁定的公网依赖，再复制应用和已构建的公网界面。
COPY requirements-cloud.lock .
RUN pip install --no-cache-dir -r requirements-cloud.lock
# 安装经过摘要核验的 age，备份只持久化加密归档。
COPY deploy/cloud/install_age.py /tmp/install_age.py
RUN python /tmp/install_age.py && rm /tmp/install_age.py
COPY backend ./backend
COPY config/prompt-standard.md config/prompt-full.md config/assistant-knowledge.json ./config/
COPY frontend/dist-public ./frontend/dist-public
# 验证前端产物并为数据卷准备专用非特权用户。
RUN test -f frontend/dist-public/index.html && useradd --uid 10001 --create-home stockai \
    && mkdir -p /var/lib/stockai /var/lib/stockai-erasures /run/stockai \
    && chown -R stockai:stockai /var/lib/stockai /var/lib/stockai-erasures /run/stockai
USER 10001:10001
EXPOSE 8766
# 健康探测携带配置中的公网 Host，遵守应用主机校验。
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import os,urllib.request,urllib.parse; r=urllib.request.Request('http://127.0.0.1:8766/api/health',headers={'Host':urllib.parse.urlsplit(os.environ['STOCKAI_PUBLIC_URL']).netloc}); urllib.request.urlopen(r,timeout=3)"
# 容器只启动单个应用进程，个人任务并发由应用内队列控制。
CMD ["python","-m","backend.upgrades.cli","serve","--component","web","--port","8766"]
