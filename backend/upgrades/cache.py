"""Retention limited to journal-owned update files; never traverse a workspace or unknown folder."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import re
import shutil
from .trust import UpgradeError


class Cache:
    def cleanup(self,*,execute=False,automatic=False,now=None):
        now=now or datetime.now(timezone.utc)
        previous=self.state.get('cache-cleanup',{})
        if automatic and previous.get('checked_at') and now<datetime.fromisoformat(previous['checked_at'])+timedelta(days=1):return previous
        with self.lock():
            active=self.current()
            if active and active['status'] not in ('succeeded','rolled_back','failed','cancelled','manual_recovery'):
                return {'status':'waiting','reason':'升级事务尚未结束','files':[],'bytes':0}
            transactions=list(self.state.prefixed('transaction:').values())
            valid=[t for t in transactions if re.fullmatch('[0-9a-f]{32}',t.get('id',''))]
            retained={active['id']} if active else set()
            installation=self.state.get('active-installation')
            if installation:
                retained.add(installation['transaction'])
                previous_code=self.state.get('previous-installation:'+installation['transaction'])
                if previous_code:retained.add(previous_code['transaction'])
            expired=[t for t in valid if t['status'] in ('succeeded','rolled_back','failed','cancelled') and datetime.fromisoformat(t['updated_at'])<now-timedelta(days=30)]
            candidates=[]
            for tx in expired:
                candidates.append((self.folder/'downloads'/tx['id'],tx,None))
                if tx['id'] not in retained:
                    candidates.append((self.folder/'installations'/tx['id'],tx,None))
                # 可用旧代码可长期保留；数据检查点仍受既有 30 天隐私保留上限约束。
                point=tx.get('checkpoint')
                if point and point.get('available') and re.fullmatch('[a-zA-Z0-9]{1,64}',point['id']):candidates.append((self.folder/'checkpoints'/(point['id']+'.age'),tx,point))
            planned=[]
            for path,tx,point in candidates:
                if not path.exists():continue
                root=self.folder.resolve();resolved=path.resolve()
                if path.is_symlink() or not resolved.is_relative_to(root) or resolved==root:raise UpgradeError('RECOVERY','缓存路径不属于当前升级目录')
                files=list(path.rglob('*')) if path.is_dir() else [path]
                if any(p.is_symlink() or not p.resolve().is_relative_to(root) for p in files):raise UpgradeError('RECOVERY','缓存目录含链接，未执行清理')
                size=sum(p.stat().st_size for p in files if p.is_file());planned.append((path,tx,point,size))
            result={'status':'completed' if execute else 'preview','checked_at':now.isoformat(),'retention_days':30,'retained_transactions':sorted(retained),'files':[p.relative_to(self.folder).as_posix() for p,_,_,_ in planned],'bytes':sum(size for _,_,_,size in planned)}
            if execute:
                for path,tx,point,_ in planned:
                    if path.is_dir():shutil.rmtree(path)
                    else:path.unlink()
                    if point:
                        tx={**tx,'checkpoint':{**point,'available':False,'expired_at':now.isoformat()}}
                        self.state.put('transaction:'+tx['id'],tx)
                history=self.state.get('history',[])
                for item in history:
                    tx=self.state.get('transaction:'+item['id'])
                    if tx and tx.get('checkpoint'):item['recovery_available']=tx['checkpoint'].get('available',False)
                self.state.put('history',history);self.state.put('cache-cleanup',result)
            return result
