"""Remote-client updater: its state/checkpoint never opens the remote or local research DB."""
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from backend.cloud.config import CloudConfig
from backend.cloud.crypto import Keyring,atomic_private
from backend.config import ROOT
from backend.processes import hidden_process
from backend.sync.protocol import canonical,digest
from backend.upgrades.manager import Manager
from backend.upgrades.checkpoint import file_hash
from backend.upgrades.trust import UpgradeError


class Checkpoints:
    def __init__(self,cfg):self.cfg=cfg;self.root=cfg.erasures/'upgrades/checkpoints';self.root.mkdir(parents=True,exist_ok=True);self.identity=cfg.key_file.parent/'backup-identity.txt'
    def materials(self):
        if not self.identity.is_file() or not self.cfg.age_recipient or not Path(self.cfg.age_bin).is_file():raise UpgradeError('RECOVERY')
    def make(self,ident):
        self.materials();destination=self.root/(ident+'.age');data=(self.cfg.data/'client.json').read_bytes()
        result=subprocess.run([self.cfg.age_bin,'--encrypt','--recipient',self.cfg.age_recipient,'--output',str(destination)],input=data,capture_output=True,timeout=30,**hidden_process())
        if result.returncode:raise UpgradeError('RECOVERY')
        self.decrypt(destination,digest(data));return {'id':ident,'sha256':file_hash(destination),'payload_sha256':digest(data),'counts':{},'available':True,'client_only':True}
    def decrypt(self,path,expected):
        result=subprocess.run([self.cfg.age_bin,'--decrypt','--identity',str(self.identity),str(path)],capture_output=True,timeout=30,**hidden_process())
        if result.returncode or digest(result.stdout)!=expected:raise UpgradeError('RECOVERY')
        value=json.loads(result.stdout)
        if set(value)-{'mode','url','account'}:raise UpgradeError('RECOVERY')
        return result.stdout
    def restore(self,point,after_restore=None):
        if not point.get('available'):raise UpgradeError('RECOVERY','客户端恢复点已过期或不可用')
        if not point['id'].isalnum():raise UpgradeError('RECOVERY')
        path=self.root/(point['id']+'.age')
        if file_hash(path)!=point['sha256']:raise UpgradeError('RECOVERY')
        atomic_private(self.cfg.data/'client.json',self.decrypt(path,point['payload_sha256']))


def manager(home,*,initialize=False):
    import os,sys
    from backend.cloud import VERSION
    base=home/'client-state';key_file=base/'secrets/keys.json';identity=key_file.parent/'backup-identity.txt';tools=ROOT/'tools'
    if not tools.exists():tools=ROOT/'.tmp/tools/age'
    if initialize:
        if not key_file.exists():Keyring.create(key_file)
        if not identity.exists():
            raw=subprocess.run([str(tools/'age-keygen.exe')],capture_output=True,check=True,timeout=10,**hidden_process()).stdout;atomic_private(identity,raw)
        mode=home/'desktop-mode.json';atomic_private(base/'config/client.json',mode.read_bytes() if mode.is_file() else canonical({'mode':'remote'}))
    recipient=next((v.split(':',1)[1].strip() for v in identity.read_text().splitlines() if v.startswith('# public key:')),'') if identity.is_file() else ''
    cfg=CloudConfig(data=base/'config',erasures=base/'control',key_file=key_file,background=False,age_bin=str(tools/'age.exe'),age_recipient=recipient)
    m=Manager(cfg,Keyring(key_file),component='desktop',version=VERSION,form='exe' if getattr(sys,'frozen',False) else 'source',root_file=os.environ.get('STOCKAI_RELEASE_ROOT',str(ROOT/'config/release-root.json')),source=os.environ.get('STOCKAI_RELEASE_SOURCE',''))
    m.client_only=True;m.checkpoints=Checkpoints(cfg);return m


def background_checks(home,stop):
    """Client metadata only; never constructs a research Host or opens its database."""
    import threading
    def work():
        m=manager(home,initialize=True)
        while not stop.is_set():
            try:
                m.cleanup(execute=True,automatic=True);check=m.check(automatic=True)
                if check.get('status')=='available' and m.state.get('settings')['automatic_download']:
                    tx=m.prepare()
                    if tx['status']=='planned':m.download(tx['id'])
            except Exception:pass
            stop.wait(30)
    thread=threading.Thread(target=work,name='remote-client-update-check',daemon=True);thread.start();return thread


def show(window,home,args,on_exit):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QDialog,QVBoxLayout,QLabel,QPushButton,QFileDialog,QPlainTextEdit,QMessageBox
    import threading
    m=manager(home,initialize=True);dialog=QDialog(window);dialog.setWindowTitle('StockAI 远程客户端 · 关于与升级');dialog.resize(650,460)
    layout=QVBoxLayout(dialog);layout.addWidget(QLabel('仅更新本机客户端。远程研究服务、资料和后台任务由远程站点管理。'))
    output=QPlainTextEdit(dialog);output.setReadOnly(True);layout.addWidget(output);work={'busy':False,'error':''}
    def act(fn):
        if work['busy']:return
        work.update(busy=True,error='')
        def run():
            try:fn()
            except Exception as exc:work['error']=str(exc)
            finally:work['busy']=False
        threading.Thread(target=run,daemon=True).start()
    def import_manifest():
        name,_=QFileDialog.getOpenFileName(dialog,'选择签名清单','','JSON (*.json)')
        if name:act(lambda:m.prepare(json.loads(Path(name).read_bytes())))
    def import_package():
        tx=m.current()
        if not tx:return
        name,_=QFileDialog.getOpenFileName(dialog,'选择对应的已签名安装包')
        if not name:return
        def upload():
            with Path(name).open('rb') as source:
                offset=0
                while chunk:=source.read(3*1024*1024):m.upload(tx['id'],offset,chunk);offset+=len(chunk)
        act(upload)
    def install():
        tx=m.current()
        if not tx:return
        try:
            checked=m.preflight(tx['id'],executor=True)
            if checked['status']!='waiting_confirmation':return
            answer=QMessageBox.question(dialog,'确认本机客户端升级',f"{tx['from_version']} → {tx['target_version']}\n已验证本机客户端恢复点材料。确认退出客户端后安装并重新打开远程站点？")
            if answer!=QMessageBox.StandardButton.Yes:return
            dialog.close();on_exit(tx)
        except Exception as exc:work['error']=str(exc)
    for label,fn in [('检查签名更新源',lambda:act(m.check)),('导入离线签名清单',import_manifest),('准备已检查的升级计划',lambda:act(m.prepare)),('下载并验证安装包',lambda:act(lambda:m.download(m.current()['id']))),('导入离线安装包',import_package),('预检、确认并重启客户端',install)]:
        button=QPushButton(label,dialog);button.clicked.connect(fn);layout.addWidget(button)
    timer=QTimer(dialog)
    def render():
        status=m.status();tx=status['transaction'];output.setPlainText('当前客户端版本：'+m.version+'\n检查状态：'+str(status['check'].get('status'))+'\n'+('升级目标：'+tx['target_version']+'\n当前阶段：'+tx['status']+'\n进度：'+str(tx.get('downloaded',0))+' / '+str(tx['release']['package']['size'])+'\n'+'\n'.join(tx.get('blockers',[])) if tx else '尚无升级计划')+'\n'+work['error'])
    timer.timeout.connect(render);timer.start(1000);render();dialog.exec()
