# 提供公网服务的离线初始化、管理员管理、迁移、密钥和备份维护命令。
from __future__ import annotations

import argparse
import getpass
import json

from .config import CloudConfig
from .crypto import Keyring
from .store import Store
from .auth import Auth
from .lifecycle import ErasureJournal,ProcessLock,reconcile,recover_swap,clean_staging
from . import backups


# 解析维护子命令，持有必要服务锁并调用对应的离线操作。
def main(argv=None):
    parser=argparse.ArgumentParser(description="StockAI 公网版离线初始化与维护（先停止服务）")
    sub=parser.add_subparsers(dest="command",required=True)
    init=sub.add_parser("init");init.add_argument("--email",required=True)
    create_admin=sub.add_parser("create-admin");create_admin.add_argument("--email",required=True)
    migrate=sub.add_parser("migrate");migrate.add_argument("--source",required=True);migrate.add_argument("--admin-id",required=True)
    sub.add_parser("backup");sub.add_parser("status");sub.add_parser("rotate-key")
    restore=sub.add_parser("restore");restore.add_argument("--id",required=True);restore.add_argument("--identity",required=True)
    args=parser.parse_args(argv);cfg=CloudConfig.from_env()
    with ProcessLock(cfg.erasures/"service.lock"):
        if cfg.key_file.exists():
            recover_swap(cfg,Keyring(cfg.key_file))
        store=Store(cfg).initialize()
        if args.command in {"init","create-admin"}:
            if args.command=="init" and store.one("SELECT id FROM users LIMIT 1"):
                raise SystemExit("已有账号，不能再次初始化")
            if args.command=="create-admin" and not cfg.key_file.exists():
                raise SystemExit("尚未初始化站点，请先使用 init")
            password=getpass.getpass("管理员密码（至少12字符）: ")
            if password!=getpass.getpass("再次输入: "):
                raise SystemExit("两次密码不一致")
            keys=Keyring(cfg.key_file) if cfg.key_file.exists() else Keyring.create(cfg.key_file)
            journal=ErasureJournal(store,keys)
            if not journal.path.exists() and args.command=="init":
                journal.create()
            else:
                journal.verify()
            uid=Auth(store,keys).create_admin(args.email,password)
            print(json.dumps({"admin_id":uid,"site_id":store.site_id,"public_url":cfg.public_url},ensure_ascii=False));return
        keys=Keyring(cfg.key_file);journal=ErasureJournal(store,keys);reconcile(store,journal);clean_staging(store)
        if args.command=="status":
            result={"site_id":store.site_id,"users":store.one("SELECT COUNT(*) n FROM users")["n"],"erasures":len(journal.verify()),"sqlite":__import__("sqlite3").sqlite_version,"key_version":keys.active}
        elif args.command=="backup":
            result=backups.create(store,keys,journal)
        elif args.command=="restore":
            result=backups.restore(store,keys,journal,args.id,args.identity)
        elif args.command=="rotate-key":
            # 保留验证现存备份与账本签名所需的旧版密钥。
            version=keys.rotate()
            with store.tx(immediate=True) as c:
                for row in c.execute("SELECT * FROM model_credentials WHERE envelope_json IS NOT NULL").fetchall():
                    value=keys.open(row["envelope_json"],site=store.site_id,user=row["user_id"],resource=row["id"],purpose="model-key")
                    sealed=keys.seal(value,site=store.site_id,user=row["user_id"],resource=row["id"],purpose="model-key")
                    c.execute("UPDATE model_credentials SET envelope_json=? WHERE id=?",(sealed,row["id"]))
            result={"active_key_version":version,"retained_versions":sorted(keys.keys)}
        elif args.command=="migrate":
            from .migrate import migrate
            result=migrate(store,keys,journal,args.source,args.admin_id)
        print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
