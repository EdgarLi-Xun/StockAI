"""Versioned, non-network health probe. No Runtime, jobs, models, matching or mail."""
import argparse
import json
from pathlib import Path
from backend.cloud.crypto import Keyring,atomic_private
from backend.sync.protocol import canonical
from .checkpoint import database_health,file_hash
from .trust import UpgradeError


def inspect(value):
    from backend.cloud import VERSION
    if VERSION!=value['expected_version']:raise UpgradeError('HEALTH','程序版本与签名目标不一致')
    data=Path(value['data']);keys=Keyring(Path(value['key_file']))
    component=value['component']
    if value.get('client_only'):
        from backend.config import ROOT
        from PySide6.QtWebEngineWidgets import QWebEngineView
        build=json.loads((ROOT/'frontend/dist-public/version.json').read_bytes())
        frontend=build['version']==VERSION
        for name,wanted in build['files'].items():
            path=(ROOT/'frontend/dist-public'/name).resolve()
            if not path.is_relative_to((ROOT/'frontend/dist-public').resolve()) or file_hash(path)!=wanted:frontend=False
        return {'component':component,'version':VERSION,'database':True,'archives':True,'control':True,'frontend':frontend,'protocol':True,'capabilities':{'schema':3,'api':1,'sync':1},'client_only':True,'paid_calls':0,'orders_executed':0,'mail_sent':0}
    if component=='hub':
        from backend.hub.app import Config,Store
        store=Store(Config(data,Path(value['erasures']),dev=value.get('dev',False)))
        counts=database_health(store.db);frontend=True;archives=True
    else:
        from backend.cloud.config import CloudConfig
        from backend.cloud.store import Store
        from backend.cloud.lifecycle import ErasureJournal,reconcile
        from backend.cloud.research_control import control
        cfg=CloudConfig(data=data,erasures=Path(value['erasures']),key_file=Path(value['key_file']),public_url=value['public_url'],dev=value.get('dev',False),background=False)
        before=database_health(data/'stockai.sqlite3');store=Store(cfg).initialize();migrated=database_health(store.path)
        for table in ('users','user_watchlist','personal_reports','sim_accounts','sim_orders','sim_lots','sim_ledger','personal_backtests','research_objects','research_versions','assistant_requests'):
            if table in before and before[table]!=migrated[table]:raise UpgradeError('HEALTH','迁移改变了未声明可删除的业务记录数量')
        reconcile(store,ErasureJournal(store,keys));control(store,keys).reconcile()
        from backend.config import ROOT
        static=ROOT/'frontend/dist-public';frontend=(static/'index.html').is_file();archives=True
        try:
            build=json.loads((static/'version.json').read_bytes())
            frontend=frontend and build['version']==VERSION
            for name,expected in build['files'].items():
                path=(static/name).resolve()
                if not path.is_relative_to(static.resolve()) or not path.is_file() or file_hash(path)!=expected:frontend=False
        except (OSError,ValueError,KeyError):frontend=False
        for row in store.query('SELECT user_id,markdown_path,html_path FROM personal_reports'):
            for name in ('markdown_path','html_path'):
                path=(data/row[name]).resolve()
                if not path.is_relative_to((data/'reports'/row['user_id']).resolve()) or not path.is_file():archives=False
        from backend.sync.schema import protect
        with store.tx(immediate=True) as c:protect(c)
        counts=database_health(store.path)
    baseline=value.get('baseline',{})
    # 应用最新注销后允许减少；增加的已执行流水/任务则不能来自健康探针。
    for table in ('sim_orders','sim_ledger','model_calls','personal_jobs','alert_deliveries'):
        if table in baseline and counts.get(table,0)>baseline[table]:raise UpgradeError('HEALTH','健康检查出现了额外业务执行')
    return {'component':component,'version':VERSION,'database':True,'archives':archives,'control':True,'frontend':frontend,'protocol':True,'capabilities':{'schema':3,'api':1,'sync':1},'counts':counts,'paid_calls':0,'orders_executed':0,'mail_sent':0}


def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('config');parser.add_argument('--result',required=True);args=parser.parse_args(argv)
    value=json.loads(Path(args.config).read_bytes());result=inspect(value);atomic_private(Path(args.result),canonical(result))


if __name__=='__main__':main()
