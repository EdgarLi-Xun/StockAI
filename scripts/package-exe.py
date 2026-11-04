"""Build the per-user Windows setup from a slimmed PyInstaller directory."""
from __future__ import annotations
import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.cloud import VERSION


def compiler():
    local=Path(os.environ.get('LOCALAPPDATA',''))/'Programs'/'Inno Setup 6'/'ISCC.exe'
    candidates=[Path(os.environ.get('INNO_SETUP_COMPILER','')),local,Path(r'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'),Path(r'C:\Program Files\Inno Setup 6\ISCC.exe')]
    for path in candidates:
        if path.is_file():return path
    raise SystemExit('缺少 Inno Setup 6 编译器 ISCC.exe')


def write_spec(path,entry,icon,version_file,extra_binaries,extra_datas,hidden,excludes,report):
    path.write_text(f"""# -*- mode: python ; coding: utf-8 -*-
import json, sys
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules, copy_metadata
from scripts.bundle_slim import partition

mini_datas, mini_bins, mini_hidden = collect_all('py_mini_racer')
datas = list({extra_datas!r}) + mini_datas + collect_data_files('akshare') + collect_data_files('tzdata') + collect_data_files('certifi') + copy_metadata('akshare')
binaries = list({extra_binaries!r}) + mini_bins
hidden = list({hidden!r}) + list(mini_hidden) + collect_submodules('uvicorn')
a = Analysis(
    [{str(entry)!r}],
    pathex=[{str(ROOT)!r}],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=list({excludes!r}),
    noarchive=False,
)
kept_bins, dropped_bins = partition(list(a.binaries))
kept_datas, dropped_datas = partition(list(a.datas))
a.binaries = kept_bins
a.datas = kept_datas
def _size(item):
    try: return Path(item[1]).stat().st_size
    except OSError: return 0
dropped = list(dropped_bins) + list(dropped_datas)
kept = list(kept_bins) + list(kept_datas)
Path({str(report)!r}).write_text(json.dumps({{'dropped_files': len(dropped), 'dropped_bytes': sum(_size(item) for item in dropped), 'kept_files': len(kept), 'kept_bytes': sum(_size(item) for item in kept)}}, indent=2), encoding='utf-8')
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='StockAI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon={str(icon)!r},
    version={str(version_file)!r},
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, upx_exclude=[], name='StockAI')
""",encoding='utf-8')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--skip-build',action='store_true');args=parser.parse_args()
    if sys.platform!='win32':raise SystemExit('请在 Windows x64 上构建安装包')
    if not args.skip_build:
        subprocess.run(['npm.cmd','run','build:public'],cwd=ROOT/'frontend',check=True)
    if not (ROOT/'frontend/dist-public/index.html').is_file():raise SystemExit('缺少已构建的完整工作台')
    tools=ROOT/'.tmp/pyinstaller-tools';env=os.environ.copy();env['PYTHONPATH']=str(tools)+os.pathsep+str(ROOT)
    env['TEMP']=str(ROOT/'.tmp');env['TMP']=env['TEMP'];env['PYTHONUTF8']='1'
    env['PYINSTALLER_CONFIG_DIR']=str(ROOT/'.tmp/pyinstaller-cache')
    windows=Path(os.environ.get('SystemRoot','C:/Windows'))
    env['PATH']=os.pathsep.join([str(Path(sys.base_prefix)),str(Path(sys.executable).parent),str(windows/'System32'),str(windows)])
    build=ROOT/'.tmp/exe-build';build.mkdir(parents=True,exist_ok=True)
    icon=build/'StockAI.ico'
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage,QPainter
    app=QGuiApplication.instance() or QGuiApplication([])
    renderer=QSvgRenderer(str(ROOT/'frontend/public/favicon.svg'));image=QImage(256,256,QImage.Format.Format_ARGB32);image.fill(0)
    painter=QPainter(image);renderer.render(painter,QRectF(0,0,256,256));painter.end()
    if not image.save(str(icon),'ICO'):raise RuntimeError('应用图标生成失败')
    version_file=build/'version.txt'
    core=tuple(int(v) for v in VERSION.split('-')[0].split('.'))+(0,)
    version_info='.'.join(str(part) for part in core)
    version_file.write_text("VSVersionInfo(ffi=FixedFileInfo(filevers="+str(core)+",prodvers="+str(core)+",mask=0x3f,flags=0,OS=0x40004,fileType=1,subtype=0,date=(0,0)),kids=[StringFileInfo([StringTable('080404b0',[StringStruct('CompanyName','StockAI'),StringStruct('FileDescription','StockAI 桌面与 Web 研究工作台'),StringStruct('FileVersion','"+VERSION+"'),StringStruct('ProductName','StockAI'),StringStruct('ProductVersion','"+VERSION+"')])]),VarFileInfo([VarStruct('Translation',[2052,1200])])])",encoding='utf-8')
    import PySide6
    qt=Path(PySide6.__file__).parent
    binaries=[]
    for binary in ('age.exe','age-keygen.exe'):
        source=ROOT/'.tmp/tools/age'/binary
        if not source.is_file():raise SystemExit('缺少已校验的 age 工具：'+str(source))
        binaries.append((str(source),'tools'))
    for runtime_name in ('VCRUNTIME140.dll','VCRUNTIME140_1.dll','MSVCP140.dll','MSVCP140_1.dll','MSVCP140_2.dll'):
        source=qt/runtime_name
        if not source.is_file():raise RuntimeError('缺少 Qt 配套运行库：'+runtime_name)
        binaries.append((str(source),'.'))
    datas=[(str(ROOT/source),target) for source,target in [('frontend/dist-public','frontend/dist-public'),('config/assistant-knowledge.json','config'),('config/prompt-standard.md','config'),('config/prompt-full.md','config'),('docs/Windows-EXE使用说明.md','docs')]]
    hidden=['backend.collector_worker','backend.cloud.collector','desktop.check']
    excludes=['tkinter','pytest','IPython','notebook','PyQt5','PyQt6','PySide2']
    report=build/'slim.json'
    spec=build/'stockai.spec'
    write_spec(spec,ROOT/'stockai_desktop.py',icon,version_file,binaries,datas,hidden,excludes,report)
    dist=build/'dist'
    if dist.exists():shutil.rmtree(dist)
    log=ROOT/'.tmp/exe-build.log'
    def source_hashes():return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for folder in ('backend','desktop') for p in (ROOT/folder).rglob('*.py')}
    initial_sources=source_hashes();initial_frontend=hashlib.sha256((ROOT/'frontend/dist-public/version.json').read_bytes()).hexdigest()
    print('Building StockAI directory; detailed log: '+str(log),flush=True)
    cmd=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--distpath',str(dist),'--workpath',str(build/'work'),str(spec)]
    with log.open('w',encoding='utf-8') as output:
        result=subprocess.run(cmd,cwd=ROOT,env=env,stdout=output,stderr=subprocess.STDOUT)
    if result.returncode:raise SystemExit('目录构建失败，请检查 '+str(log))
    app_dir=dist/'StockAI'
    if not (app_dir/'StockAI.exe').is_file():raise SystemExit('目录构建没有产生 StockAI.exe')
    if source_hashes()!=initial_sources or hashlib.sha256((ROOT/'frontend/dist-public/version.json').read_bytes()).hexdigest()!=initial_frontend:raise SystemExit('构建期间源码或静态资源发生变化，请重新构建以保证产物一致')
    slim=json.loads(report.read_text(encoding='utf-8'))
    print('Slimmed '+str(slim['dropped_files'])+' files, '+str(round(slim['dropped_bytes']/1024/1024,1))+' MB uncompressed',flush=True)
    setup_name=f'StockAI-{VERSION}-Setup'
    subprocess.run([str(compiler()),'/DAppVersion='+VERSION,'/DVersionInfo='+version_info,'/DSourceDir='+str(app_dir),'/DOutputDir='+str(ROOT/'dist'),'/DIconFile='+str(icon),str(ROOT/'scripts/desktop.iss')],check=True,cwd=ROOT)
    setup=ROOT/'dist'/(setup_name+'.exe')
    if not setup.is_file():raise SystemExit('安装包没有生成')
    digest=hashlib.sha256(setup.read_bytes()).hexdigest()
    setup.with_suffix('.exe.sha256').write_text(digest+'  '+setup.name+'\n',encoding='ascii')
    sources={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for folder in ('backend','desktop') for p in (ROOT/folder).rglob('*.py')}
    manifest={'version':VERSION,'platform':'Windows x64','single_file':False,'installer':True,'install_dir':'%LOCALAPPDATA%\\Programs\\StockAI','sha256':digest,'bytes':setup.stat().st_size,'slimmed':slim,'python':sys.version.split()[0],'pyside6':metadata.version('PySide6'),'entry':'stockai_desktop.py','source_sha256':hashlib.sha256(json.dumps(sources,sort_keys=True).encode()).hexdigest(),'frontend_sha256':hashlib.sha256((ROOT/'frontend/dist-public/index.html').read_bytes()).hexdigest(),'personal_data_included':False,'requires_python_installation':False,'requires_node_installation':False}
    setup.with_suffix('.exe.build.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(manifest,ensure_ascii=False),flush=True);print(setup,flush=True)


if __name__=='__main__':main()
