"""Offline publisher utility. Private signing material never enters a build artifact."""
import argparse
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PrivateFormat,NoEncryption,load_pem_private_key
from backend.cloud.crypto import atomic_private
from backend.sync.protocol import canonical,digest
from backend.upgrades.trust import sign,public_key
from backend.upgrades.checkpoint import file_hash


def main():
    parser=argparse.ArgumentParser(description='StockAI 离线发布签名工具');sub=parser.add_subparsers(dest='action',required=True)
    init=sub.add_parser('init');init.add_argument('--directory',required=True)
    release=sub.add_parser('release');release.add_argument('--key',action='append',required=True);release.add_argument('--package',required=True);release.add_argument('--url',required=True);release.add_argument('--version',required=True);release.add_argument('--sequence',required=True,type=int);release.add_argument('--minimum-source',required=True);release.add_argument('--component',choices=['desktop','web','hub'],required=True);release.add_argument('--form',choices=['exe','source'],required=True);release.add_argument('--channel',choices=['stable','preview'],required=True);release.add_argument('--output',required=True)
    raw=sub.add_parser('sign-json');raw.add_argument('--key',action='append',required=True);raw.add_argument('--input',required=True);raw.add_argument('--output',required=True)
    args=parser.parse_args();now=datetime.now(timezone.utc)
    if args.action=='init':
        folder=Path(args.directory).resolve()
        if folder.exists() and any(folder.iterdir()):parser.error('签名目录非空，拒绝覆盖')
        keys=[Ed25519PrivateKey.generate() for _ in range(3)];entries={digest(public_key(key)):public_key(key) for key in keys};ids=list(entries)
        root={'_type':'stockai-root/1','version':1,'expires':(now+timedelta(days=365)).isoformat(),'keys':entries,'roles':{'root':{'keyids':ids[:2],'threshold':2},'releases':{'keyids':ids[2:],'threshold':1}}}
        for index,key in enumerate(keys):atomic_private(folder/('root-'+str(index+1)+'.pem' if index<2 else 'releases.pem'),key.private_bytes(Encoding.PEM,PrivateFormat.PKCS8,NoEncryption()))
        atomic_private(folder/'release-root.json',canonical(sign(root,keys[:2])));print('离线签名材料已生成。仅将 release-root.json 安装到受信任运行端；私钥留在离线发布目录。');return
    keys=[load_pem_private_key(Path(path).read_bytes(),password=None) for path in args.key]
    if args.action=='sign-json':value=json.loads(Path(args.input).read_bytes())
    else:
        package=Path(args.package)
        value={'_type':'stockai-release/1','component':args.component,'version':args.version,'channel':args.channel,'platform':'windows' if args.component=='desktop' else 'linux','architecture':'x64','form':args.form,'sequence':args.sequence,'published':now.isoformat(),'expires':(now+timedelta(days=7)).isoformat(),'minimum_source':args.minimum_source,'accepts':{k:{'min':1,'max':3 if k=='schema' else 1} for k in ('schema','api','sync')},'provides':{'schema':3,'api':1,'sync':1},'migration':'stockai-ordered/1','rollback':{'prewrite':True,'postwrite':False},'package':{'size':package.stat().st_size,'sha256':file_hash(package),'url':args.url}}
    atomic_private(Path(args.output),canonical(sign(value,keys)));print('已写入签名清单：'+str(Path(args.output).resolve()))


if __name__=='__main__':main()
