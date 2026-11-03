# 为本地服务提供可独立运行的备份命令，复用数据库初始化与完整性校验。
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import db, maintenance

parser=argparse.ArgumentParser()
parser.add_argument('--restore',help='备份标识')
parser.add_argument('--confirm',help='恢复需传入“恢复备份”')
args=parser.parse_args()
db.initialize()
if args.restore:
    from run import healthy
    if healthy():
        raise SystemExit('请先停止共享服务后再使用离线恢复脚本；在线恢复请使用本机设置页面。')
print(json.dumps(maintenance.restore_backup(args.restore,args.confirm) if args.restore else maintenance.create_backup(),ensure_ascii=False,indent=2))
