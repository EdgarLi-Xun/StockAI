from __future__ import annotations
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import uuid
from backend.cloud.lifecycle import ProcessLock
from backend.cloud.crypto import atomic_private
from backend.cloud import VERSION
from backend.sync.protocol import canonical,digest
from .versions import Version
from .trust import Trust,UpgradeError
from .state import State
from .download import Fetcher
from .github import ASSET_HOSTS,DEFAULT_SOURCE,asset_url,parse_checksum,repository,latest as github_latest
from .checkpoint import Checkpoints,file_hash,database_health
from .cache import Cache

TERMINAL={'succeeded','rolled_back','failed','cancelled','manual_recovery'}


class Manager(Cache):
    def __init__(self,cfg,keys,*,component='web',version=VERSION,form='source',root_file=None,source='',store=None,fetcher=None,identity=None):
        self.cfg,self.keys,self.component,self.version,self.form=cfg,keys,component,version,form;self.store=store
        self.folder=cfg.erasures/'upgrades';self.state=State(self.folder,keys.keys[min(keys.keys)])
        self.root_file=root_file;self.source=source or DEFAULT_SOURCE;self.fetcher=fetcher or Fetcher();self.checkpoints=Checkpoints(cfg,component,identity)
        self.target={'component':component,'version':version,'platform':'windows' if component=='desktop' else 'linux','architecture':'x64' if platform.machine().lower() in ('amd64','x86_64') else platform.machine().lower(),'form':form,'capabilities':{'api':1,'sync':1,'schema':3}}
        if not self.state.get('settings'):self.state.put('settings',{'channel':'preview' if Version(version).pre else 'stable','automatic_check':True,'automatic_download':False})

    def lock(self):return ProcessLock(self.folder/'transaction.lock')
    def trust(self):return Trust(self.state,self.root_file)
    def current(self):
        ident=self.state.get('active')
        return self.state.get('transaction:'+ident) if ident else None
    def status(self):
        repo=repository(self.source);kind='github' if repo else 'signed' if self.source else 'unconfigured'
        settings=self.state.get('settings');check=self.state.get('check',{'status':'never_checked'})
        if check.get('source_id')!=digest({'source':self.source,'channel':settings['channel'],'target':self.target}):check={'status':'never_checked'}
        return {'component':self.component,'current_version':self.version,'form':self.form,'source_configured':bool(self.source),'source_kind':kind,'release_page':f'https://github.com/{repo}/releases' if repo else None,'settings':settings,'check':check,'transaction':self.current(),'history':self.state.get('history',[])[-30:],'cache_cleanup':self.state.get('cache-cleanup'),'capabilities':self.target['capabilities'],'executor':self.component=='desktop'}
    def update(self,tx,status,**values):
        tx={**tx,**values,'status':status,'updated_at':datetime.now(timezone.utc).isoformat()};self.state.put('transaction:'+tx['id'],tx);return tx

    def select_release(self,trust,items,channel):
        candidates=[];sequences={}
        for item in items:
            try:
                release=trust.candidate(item,self.target)
                if release['channel']!=channel:continue
                sequence=release['sequence'];content=digest(release)
                if sequence in sequences and sequences[sequence]!=content:raise UpgradeError('REPLAY')
                sequences[sequence]=content
                candidates.append((Version(release['version']),sequence,item))
            except UpgradeError as exc:
                if exc.code not in ('TARGET','DOWNGRADE'):raise
        return max(candidates,key=lambda value:(value[0],value[1]))[2] if candidates else None

    def check(self,automatic=False):
        settings=self.state.get('settings');before=self.state.get('check',{})
        now=datetime.now(timezone.utc)
        source_id=digest({'source':self.source,'channel':settings['channel'],'target':self.target})
        if before.get('source_id')!=source_id:before={}
        if automatic:
            if not settings['automatic_check']:return before
            if before.get('status')=='check_failed':
                if before.get('next_retry') and now<datetime.fromisoformat(before['next_retry']):return before
            elif before.get('last_attempt') and now<datetime.fromisoformat(before['last_attempt'])+timedelta(hours=24):return before
        record={**before,'status':'checking','last_attempt':now.isoformat(),'source_id':source_id,'release':None};self.state.put('check',record)
        try:
            if not self.source:raise UpgradeError('SOURCE_UNCONFIGURED','尚未配置可信更新源，可导入离线签名清单')
            repo=repository(self.source)
            if repo:
                selected=github_latest(self.fetcher,repo,self.target,settings['channel'])
                record.update(status='download_available' if selected and Version(selected['version'])>Version(self.version) else 'download_current' if selected else 'download_unavailable',last_success=now.isoformat(),error=None,next_retry=None,release=selected)
                self.state.put('check',record);return record
            envelope=self.fetcher.metadata(self.source)
            if not isinstance(envelope,dict):raise UpgradeError('UNTRUSTED')
            trust=self.trust();trust.rotate(envelope.get('roots',[]))
            selected=self.select_release(trust,envelope.get('releases',[]),settings['channel'])
            if selected:
                try:trust.release(selected,self.target)
                except UpgradeError as exc:
                    if exc.code!='DOWNGRADE':raise
                    selected=None
            record.update(status='available' if selected and Version(selected['signed']['version'])>Version(self.version) else 'up_to_date',last_success=now.isoformat(),error=None,next_retry=None,release=selected['signed'] if selected else None)
            if selected:self.state.put('selected-envelope',selected)
            self.state.put('check',record);return record
        except (UpgradeError,OSError,ValueError) as exc:
            code=getattr(exc,'code','NETWORK');record.update(status='check_failed',error=code,next_retry=(now+timedelta(minutes=30)).isoformat())
            self.state.put('check',record);return record

    def prepare(self,envelope=None):
        with self.lock():
            old=self.current()
            if old and old['status'] not in TERMINAL:return old
            envelope=envelope or self.state.get('selected-envelope')
            if not envelope:raise UpgradeError('UNTRUSTED')
            trust=self.trust()
            if 'releases' in envelope:
                trust.rotate(envelope.get('roots',[]))
                envelope=self.select_release(trust,envelope['releases'],self.state.get('settings')['channel'])
                if not envelope:raise UpgradeError('TARGET')
            candidate=trust.candidate(envelope,self.target)
            if Version(candidate['version'])<=Version(self.version):raise UpgradeError('DOWNGRADE')
            release=trust.release(envelope,self.target)
            ident=uuid.uuid4().hex;tx={'id':ident,'component':self.component,'from_version':self.version,'target_version':release['version'],'release':release,'release_hash':digest(release),'status':'planned','created_at':datetime.now(timezone.utc).isoformat(),'writes_opened':False,'actor':None,'checkpoint':None,'error':None}
            tx.update(workspace=str(self.cfg.data.resolve()),control=str(self.cfg.erasures.resolve()))
            tx['confirmation']=digest({k:tx[k] for k in ('id','component','from_version','target_version','release_hash','workspace','control')})
            self.state.put('active',ident);self.state.put('transaction:'+ident,tx);self.state.put('envelope:'+ident,envelope)
            return tx

    def prepare_local(self):
        """仅本机桌面 EXE：把已检查的 GitHub 发行包绑定到发布页 SHA-256。这不是发布者签名。"""
        with self.lock():
            old=self.current()
            if old and old['status'] not in TERMINAL:return old
            if getattr(self,'client_only',False) or self.component!='desktop' or self.form!='exe':raise UpgradeError('TARGET','只有本机桌面程序可以从 GitHub 下载安装')
            repo=repository(self.source)
            if not repo or self.target['architecture']!='x64':raise UpgradeError('TARGET')
            check=self.state.get('check') or {}
            found=check.get('release') or {}
            if check.get('status')!='download_available' or found.get('signature_verified') is not False:raise UpgradeError('UNTRUSTED','当前没有可安装的 GitHub 发行包')
            version,name,size=found.get('version'),found.get('asset_name'),found.get('size')
            if found.get('component')!='desktop' or found.get('channel')!=self.state.get('settings')['channel']:raise UpgradeError('TARGET')
            if not isinstance(version,str) or name!=f'StockAI-{version}-Windows.exe' or type(size) is not int or not 0<size<=2*1024*1024*1024:raise UpgradeError('TARGET')
            try:newer=Version(version)>Version(self.version)
            except (TypeError,ValueError):raise UpgradeError('TARGET') from None
            if not newer:raise UpgradeError('DOWNGRADE')
            url=asset_url(repo,version,name)
            if found.get('download_url')!=url:raise UpgradeError('TARGET')
            checksum=parse_checksum(self.fetcher.fetch_bytes(asset_url(repo,version,name+'.sha256'),256,ASSET_HOSTS),name)
            release={'component':'desktop','version':version,'channel':found['channel'],'platform':'windows','architecture':'x64','form':'exe','asset_name':name,'provides':dict(self.target['capabilities']),'package':{'size':size,'sha256':checksum,'url':url}}
            ident=uuid.uuid4().hex
            tx={'id':ident,'component':self.component,'from_version':self.version,'target_version':version,'release':release,'release_hash':digest(release),'trust':'github-sha256','checksum':checksum,'status':'planned','created_at':datetime.now(timezone.utc).isoformat(),'writes_opened':False,'actor':None,'checkpoint':None,'error':None}
            tx.update(workspace=str(self.cfg.data.resolve()),control=str(self.cfg.erasures.resolve()))
            tx['confirmation']=digest({k:tx[k] for k in ('id','component','from_version','target_version','release_hash','workspace','control')})
            self.state.put('active',ident);self.state.put('transaction:'+ident,tx)
            return tx

    def bind_github(self,tx):
        repo=repository(self.source);release=tx.get('release') or {};package=release.get('package') or {}
        version,name=release.get('version'),release.get('asset_name')
        if getattr(self,'client_only',False) or self.component!='desktop' or self.form!='exe' or not repo:raise UpgradeError('TARGET')
        if tx.get('trust')!='github-sha256' or release.get('component')!='desktop' or release.get('form')!='exe':raise UpgradeError('UNTRUSTED')
        if not isinstance(version,str) or name!=f'StockAI-{version}-Windows.exe':raise UpgradeError('TARGET')
        try:newer=Version(version)>Version(self.version)
        except (TypeError,ValueError):raise UpgradeError('TARGET') from None
        if not newer:raise UpgradeError('DOWNGRADE')
        sha=package.get('sha256')
        if package.get('url')!=asset_url(repo,version,name) or sha!=tx.get('checksum') or not isinstance(sha,str) or len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):raise UpgradeError('PACKAGE')
        if type(package.get('size')) is not int or not 0<package['size']<=2*1024*1024*1024:raise UpgradeError('PACKAGE')

    def package_path(self,tx):return self.folder/'downloads'/tx['id']/('payload.exe' if self.form=='exe' else 'payload.zip')

    def download(self,ident,cancel=None):
        with self.lock():
            tx=self.state.get('transaction:'+ident)
            if not tx or tx['status'] not in ('planned','downloading','download_paused','download_failed'):raise UpgradeError('BUSY')
            tx=self.update(tx,'downloading',downloaded=0)
            self.state.put('cancel:'+ident,False)
            try:
                self.fetcher.download(tx['release'],self.package_path(tx),lambda count,total:self.update(tx,'downloading',downloaded=count,total=total),cancel or (lambda:self.state.get('cancel:'+ident,False)),ASSET_HOSTS if tx.get('trust')=='github-sha256' else None)
                return self.update(tx,'verified',downloaded=tx['release']['package']['size'])
            except UpgradeError as exc:self.update(tx,'download_paused' if exc.code=='CANCELLED' else 'download_failed',error=exc.code,next_retry=(datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat());raise

    def upload(self,ident,offset,data):
        with self.lock():
            tx=self.state.get('transaction:'+ident)
            if not tx or tx['status'] not in ('planned','uploading'):raise UpgradeError('BUSY')
            if len(data)>4*1024*1024:raise UpgradeError('PACKAGE')
            path=self.package_path(tx);path.parent.mkdir(parents=True,exist_ok=True);size=path.stat().st_size if path.exists() else 0
            if offset!=size or size+len(data)>tx['release']['package']['size']:raise UpgradeError('PACKAGE','上传偏移与当前目标不一致')
            with path.open('ab') as output:output.write(data);output.flush();os.fsync(output.fileno())
            done=path.stat().st_size==tx['release']['package']['size']
            if done and file_hash(path)!=tx['release']['package']['sha256']:self.update(tx,'failed',error='PACKAGE');raise UpgradeError('PACKAGE')
            return self.update(tx,'verified' if done else 'uploading',downloaded=path.stat().st_size)

    def preflight(self,ident,*,executor=False):
        tx=self.state.get('transaction:'+ident)
        if not tx:raise UpgradeError('BUSY')
        if tx['status'] in TERMINAL:return tx
        blockers=[];package=self.package_path(tx);release=tx['release']
        if tx.get('workspace')!=str(self.cfg.data.resolve()) or tx.get('control')!=str(self.cfg.erasures.resolve()):raise UpgradeError('RECOVERY','计划所属工作区与当前目录不同，拒绝执行')
        if tx.get('trust')=='github-sha256':self.bind_github(tx)
        else:self.trust().release(self.state.get('envelope:'+ident),self.target)
        if not package.is_file() or package.stat().st_size!=release['package']['size'] or file_hash(package)!=release['package']['sha256']:blockers.append('升级包尚未完整验证')
        data_bytes=sum(p.stat().st_size for p in self.cfg.data.rglob('*') if p.is_file() and not p.is_symlink())
        expanded=0
        if self.form=='source' and package.is_file():
            import zipfile
            try:
                with zipfile.ZipFile(package) as archive:expanded=sum(item.file_size for item in archive.infolist())
                if expanded>6*1024*1024*1024:blockers.append('解包规模超过允许上限')
            except zipfile.BadZipFile:blockers.append('源码安装包不是有效 ZIP')
        # 检查点会同时存在快照、压缩包、密文和真实解密验证副本。
        needed=release['package']['size']*8+expanded*2+data_bytes*6+64*1024*1024
        if shutil.disk_usage(self.folder).free<needed:blockers.append('空间不足，估算已包含程序、检查点与迁移暂存')
        if shutil.disk_usage(self.cfg.data).free<data_bytes*2+64*1024*1024:blockers.append('业务数据卷没有足够的恢复暂存空间')
        if not os.access(self.cfg.data,os.W_OK) or not os.access(self.folder,os.W_OK):blockers.append('工作区或升级目录没有写权限')
        if self.store:
            jobs=self.store.one("SELECT count(*) n FROM personal_jobs WHERE status='running'")['n']
            mail=self.store.one("SELECT count(*) n FROM alert_deliveries WHERE state='sending'")['n']
            site=self.store.one("SELECT count(*) n FROM site_jobs WHERE status='running'")['n']
            if jobs or mail or site:blockers.append('仍有模型、通知或采集在途，请等待结束或先停止任务')
            clients=getattr(self.store,'upgrade_clients',{})
            if any(v['dirty'] and v['expires']>datetime.now(timezone.utc).timestamp() for v in clients.values()):blockers.append('有未保存编辑，请先保存或导出草稿')
        try:self.checkpoints.materials()
        except UpgradeError as exc:blockers.append(str(exc))
        if self.component!='desktop' and not executor:
            return self.update(tx,'awaiting_operator',blockers=blockers,estimated_bytes=needed,operator_command=f'python -m backend.upgrades.cli execute --id {ident} --component {self.component}')
        return self.update(tx,'preflight_blocked' if blockers else 'waiting_confirmation',blockers=blockers,estimated_bytes=needed)

    def maintenance(self,enabled):
        if self.store:self.store.execute("UPDATE cloud_meta SET value=? WHERE key='maintenance'",('1' if enabled else '0',))
        else:atomic_private(self.folder/'maintenance',b'1' if enabled else b'0')

    def execute(self,ident,confirmation,runner,*,actor='operator'):
        """Runner is a fixed local executor, never a command supplied by HTTP input."""
        with self.lock():
            tx=self.state.get('transaction:'+ident)
            if not tx or confirmation!=tx['confirmation']:raise UpgradeError('CONFIRMATION')
            if tx['status'] in TERMINAL:return tx
            if self.state.get('active')!=ident or tx['status'] not in ('verified','waiting_confirmation','preflight_blocked','awaiting_operator','exit_requested'):raise UpgradeError('BUSY')
            tx=self.preflight(ident,executor=True)
            if tx['status']!='waiting_confirmation':raise UpgradeError('RECOVERY','升级预检尚有阻断项')
            self.maintenance(True);tx=self.update(tx,'backup',actor=actor)
            try:
                if self.store:
                    sync=getattr(self.store,'sync_service',None)
                    if sync:
                        for row in self.store.query('SELECT user_id FROM sync_bindings'):sync.capture(row['user_id'],100000)
                checkpoint=self.checkpoints.make(ident);tx=self.update(tx,'preparing',checkpoint=checkpoint)
                installed=runner.prepare(tx,self.package_path(tx));tx=self.update(tx,'migrating',installation=installed)
                health=runner.probe(tx,installed)
                if health.get('version')!=tx['target_version'] or health.get('component')!=self.component or health.get('capabilities')!=tx['release']['provides'] or not all(health.get(k) for k in ('database','archives','control','frontend','protocol')):raise UpgradeError('HEALTH','新版本未通过完整健康检查或实际协议能力不符')
                tx=self.update(tx,'switching',health=health);runner.activate(tx,installed)
                tx=self.update(tx,'succeeded',writes_opened=True,actual_version=health['version']);self.record(tx);self.maintenance(False);return tx
            except BaseException as exc:
                code=getattr(exc,'code','INSTALL_FAILED');tx=self.update(tx,'rolling_back',error=code)
                try:
                    runner.stop_new(tx)
                    if tx.get('checkpoint'):self.checkpoints.restore(tx['checkpoint'],self.reconcile)
                    runner.rollback(tx)
                    tx=self.update(tx,'rolled_back',actual_version=tx['from_version']);self.record(tx);self.maintenance(False)
                except BaseException:
                    tx=self.update(tx,'manual_recovery',error=code,rollback_error='RESTORE_FAILED');self.record(tx)
                return tx

    def recover(self,runner):
        with self.lock():
            tx=self.current()
            if not tx or tx['status'] in TERMINAL or tx['status'] in ('planned','downloading','uploading','verified','download_failed','download_paused','awaiting_operator','preflight_blocked','waiting_confirmation'):return tx
            self.maintenance(True)
            if tx.get('writes_opened'):return self.update(tx,'manual_recovery',error='WRITES')
            try:
                runner.stop_new(tx)
                if tx.get('checkpoint'):self.checkpoints.restore(tx['checkpoint'],self.reconcile)
                runner.rollback(tx);tx=self.update(tx,'rolled_back',error='INTERRUPTED',actual_version=tx['from_version']);self.record(tx);self.maintenance(False)
            except Exception:tx=self.update(tx,'manual_recovery',error='RESTORE_FAILED')
            return tx

    def reconcile(self,stage):
        if not self.store:return
        from dataclasses import replace
        from backend.cloud.store import Store
        from backend.cloud.lifecycle import ErasureJournal,reconcile
        from backend.cloud.research_control import control
        target=Store(replace(self.cfg,data=stage));reconcile(target,ErasureJournal(target,self.keys));control(self.store,self.keys).reconcile(target)
        from backend.sync.engine import Sync
        from backend.sync.protocol import open_sealed,keyring
        import base64
        if target.one("SELECT name FROM sqlite_master WHERE type='table' AND name='sync_bindings'"):
            sync=Sync(target,self.keys);sync.recover_local()
        with target.tx(immediate=True) as c:
            c.execute("UPDATE personal_jobs SET status='interrupted',error_code='UPGRADE_RECOVERY' WHERE status IN('queued','running')")
            c.execute("UPDATE alert_deliveries SET state='uncertain',error_code='UPGRADE_RECOVERY' WHERE state='sending'")

    def record(self,tx):
        history=self.state.get('history',[]);history.append({k:tx.get(k) for k in ('id','component','from_version','target_version','actual_version','status','release_hash','created_at','updated_at','error','writes_opened')});self.state.put('history',history)

    def rollback(self,ident,runner,confirmation):
        tx=self.state.get('transaction:'+ident)
        if not tx or confirmation!=tx['confirmation']:raise UpgradeError('CONFIRMATION')
        if tx['status'] in ('rolled_back','cancelled'):return tx
        if tx['writes_opened']:
            self.maintenance(True);self.checkpoints.make(uuid.uuid4().hex)
            self.update(tx,'manual_recovery',error='WRITES');raise UpgradeError('WRITES')
        with self.lock():
            self.maintenance(True);self.checkpoints.restore(tx['checkpoint'],self.reconcile);runner.rollback(tx)
            tx=self.update(tx,'rolled_back',actual_version=tx['from_version']);self.record(tx);self.maintenance(False);return tx
