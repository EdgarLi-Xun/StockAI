import argparse
import getpass
from .app import Config,Store
from backend.cloud.lifecycle import ProcessLock


def main():
    parser=argparse.ArgumentParser(description='StockAI 独立同步 Hub')
    parser.add_argument('action',choices=['create-user','disable-user','serve','status']);parser.add_argument('--email');parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=8770)
    args=parser.parse_args();cfg=Config.environment()
    if args.action=='serve':
        import uvicorn
        uvicorn.run('backend.hub.app:app',host=args.host,port=args.port,workers=1,proxy_headers=False)
    else:
        with ProcessLock(cfg.control/'service.lock'):
            store=Store(cfg)
            if args.action=='create-user':
                if not args.email:parser.error('需要 --email')
                password=getpass.getpass('Hub 账号密码（至少12位）: ')
                if password!=getpass.getpass('再次输入密码: '):raise SystemExit('密码不一致')
                print('已创建 Hub 用户：'+store.create_user(args.email,password))
            elif args.action=='disable-user':
                if not args.email:parser.error('需要 --email')
                print(store.disable_user(args.email))
            else:print({'component':'hub','users':len(store.query('SELECT id FROM users')),'spaces':len(store.query('SELECT id FROM spaces WHERE deleted=0')),'protocol':1})


if __name__=='__main__':main()
