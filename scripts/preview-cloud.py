# 在隔离的开发目录启动公网预览，使用开发会话策略并准备演示账号。
"""Start an isolated development preview. Never reads or migrates the local data directory."""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
# 所有预览数据、密钥与删除记录都位于临时目录，避免读取本地正式数据库。
root=ROOT/".tmp"/"cloud-preview"
os.environ.update(STOCKAI_CLOUD_DEV="1",STOCKAI_PUBLIC_URL="http://127.0.0.1:8766",STOCKAI_CLOUD_DATA_DIR=str(root/"data"),STOCKAI_ERASURE_DIR=str(root/"erasures"),STOCKAI_MASTER_KEY_FILE=str(root/"secrets"/"keys.json"),STOCKAI_CLOUD_OLLAMA_ENABLED="0")
from backend.cloud.config import CloudConfig
from backend.cloud.store import Store
from backend.cloud.crypto import Keyring,atomic_private
from backend.cloud.auth import Auth
from backend.cloud.lifecycle import ErasureJournal,ProcessLock

cfg=CloudConfig.from_env()
with ProcessLock(cfg.erasures/"service.lock"):
    store=Store(cfg).initialize()
    keys=Keyring(cfg.key_file) if cfg.key_file.exists() else Keyring.create(cfg.key_file)
    journal=ErasureJournal(store,keys)
    if not journal.path.exists():
        journal.create()
    # 只有空预览库才创建随机管理员密码，并将登录信息写入受限文件。
    if not store.one("SELECT id FROM users LIMIT 1"):
        password=secrets.token_urlsafe(20)
        uid=Auth(store,keys).create_admin("preview@stockai.test",password)
        atomic_private(root/"preview-login.txt",("隔离开发预览；不是公网正式账号\n邮箱：preview@stockai.test\n密码："+password+"\n").encode())
    # 隔离预览不预装模型凭据、不投递邮件，也不迁移正式个人数据。
print("Development preview: http://127.0.0.1:8766 ; credentials saved privately to .tmp/cloud-preview/preview-login.txt",flush=True)
if __name__=="__main__":
    import uvicorn
    uvicorn.run("backend.public_app:app",host="127.0.0.1",port=8766,workers=1,log_level="warning")
