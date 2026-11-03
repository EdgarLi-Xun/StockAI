"""Build a single Windows desktop executable with its local Web service and all runtimes."""
from __future__ import annotations
import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.cloud import VERSION


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--skip-build',action='store_true');args=parser.parse_args()
    if sys.platform!='win32':raise SystemExit('请在 Windows x64 上构建 EXE')
    if not args.skip_build:
        subprocess.run(['npm.cmd','run','build:public'],cwd=ROOT/'frontend',check=True)
    if not (ROOT/'frontend/dist-public/index.html').is_file():raise SystemExit('缺少已构建的完整工作台')
    tools=ROOT/'.tmp/pyinstaller-tools';env=os.environ.copy();env['PYTHONPATH']=str(tools)+os.pathsep+str(ROOT)
    env['TEMP']=str(ROOT/'.tmp');env['TMP']=env['TEMP'];env['PYTHONUTF8']='1'
    env['PYINSTALLER_CONFIG_DIR']=str(ROOT/'.tmp/pyinstaller-cache')
    # 不从其他桌面工具的 PATH 误收同名 ICU 等 DLL；系统组件留给 Windows 提供。
    windows=Path(os.environ.get('SystemRoot','C:/Windows'))
    env['PATH']=os.pathsep.join([str(Path(sys.base_prefix)),str(Path(sys.executable).parent),str(windows/'System32'),str(windows)])
    build=ROOT/'.tmp/exe-build';build.mkdir(parents=True,exist_ok=True)
    # 从现有矢量标识生成各尺寸 Windows 图标，不改变产品图形。
    icon=build/'StockAI.ico'
    from PySide6.QtGui import QGuiApplication,QIcon,QPixmap
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage,QPainter
    app=QGuiApplication.instance() or QGuiApplication([])
    renderer=QSvgRenderer(str(ROOT/'frontend/public/favicon.svg'));image=QImage(256,256,QImage.Format.Format_ARGB32);image.fill(0)
    painter=QPainter(image);renderer.render(painter,QRectF(0,0,256,256));painter.end()
    if not image.save(str(icon),'ICO'):raise RuntimeError('应用图标生成失败')
    name=f'StockAI-{VERSION}-Windows'
    version=build/'version.txt'
    core=tuple(int(v) for v in VERSION.split('-')[0].split('.'))+(1,)
    version.write_text("VSVersionInfo(ffi=FixedFileInfo(filevers="+str(core)+",prodvers="+str(core)+",mask=0x3f,flags=0,OS=0x40004,fileType=1,subtype=0,date=(0,0)),kids=[StringFileInfo([StringTable('080404b0',[StringStruct('CompanyName','StockAI'),StringStruct('FileDescription','StockAI 桌面与 Web 研究工作台'),StringStruct('FileVersion','"+VERSION+"'),StringStruct('ProductName','StockAI'),StringStruct('ProductVersion','"+VERSION+"')])]),VarFileInfo([VarStruct('Translation',[2052,1200])])])",encoding='utf-8')
    cmd=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onefile','--windowed','--noupx','--name',name,'--icon',str(icon),'--version-file',str(version),'--distpath',str(ROOT/'dist'),'--workpath',str(build/'work'),'--specpath',str(build),'--paths',str(ROOT)]
    for source,target in [('frontend/dist-public','frontend/dist-public'),('config/assistant-knowledge.json','config'),('config/prompt-standard.md','config'),('config/prompt-full.md','config'),('docs/Windows-EXE使用说明.md','docs')]:
        cmd+=['--add-data',str(ROOT/source)+';'+target]
    for binary in ('age.exe','age-keygen.exe'):
        source=ROOT/'.tmp/tools/age'/binary
        if not source.is_file():raise SystemExit('缺少已校验的 age 工具：'+str(source))
        cmd+=['--add-binary',str(source)+';tools']
    # Qt 的运行库比构建机 Python 自带版本更新，必须在包根部优先收集同一组。
    import PySide6
    qt=Path(PySide6.__file__).parent
    for runtime_name in ('VCRUNTIME140.dll','VCRUNTIME140_1.dll','MSVCP140.dll','MSVCP140_1.dll','MSVCP140_2.dll'):
        source=qt/runtime_name
        if not source.is_file():raise RuntimeError('缺少 Qt 配套运行库：'+runtime_name)
        cmd+=['--add-binary',str(source)+';.']
    for package in ('akshare','tzdata','certifi'):
        cmd+=['--collect-data',package]
    cmd+=['--collect-all','py_mini_racer','--collect-submodules','uvicorn','--copy-metadata','akshare']
    for module in ('backend.collector_worker','backend.cloud.collector','desktop.check'):
        cmd+=['--hidden-import',module]
    for module in ('tkinter','pytest','IPython','notebook','PyQt5','PyQt6','PySide2'):
        cmd+=['--exclude-module',module]
    cmd.append(str(ROOT/'stockai_desktop.py'))
    log=ROOT/'.tmp/exe-build.log'
    def source_hashes():return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for folder in ('backend','desktop') for p in (ROOT/folder).rglob('*.py')}
    initial_sources=source_hashes();initial_frontend=hashlib.sha256((ROOT/'frontend/dist-public/version.json').read_bytes()).hexdigest()
    print('Building '+name+'; detailed log: '+str(log),flush=True)
    with log.open('w',encoding='utf-8') as output:
        result=subprocess.run(cmd,cwd=ROOT,env=env,stdout=output,stderr=subprocess.STDOUT)
    if result.returncode:raise SystemExit('EXE 构建失败，请检查 '+str(log))
    if source_hashes()!=initial_sources or hashlib.sha256((ROOT/'frontend/dist-public/version.json').read_bytes()).hexdigest()!=initial_frontend:raise SystemExit('构建期间源码或静态资源发生变化，请重新构建以保证产物一致')
    exe=ROOT/'dist'/(name+'.exe');digest=hashlib.sha256(exe.read_bytes()).hexdigest()
    exe.with_suffix('.exe.sha256').write_text(digest+'  '+exe.name+'\n',encoding='ascii')
    sources={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for folder in ('backend','desktop') for p in (ROOT/folder).rglob('*.py')}
    manifest={'version':VERSION,'platform':'Windows x64','single_file':True,'sha256':digest,'bytes':exe.stat().st_size,'python':sys.version.split()[0],'pyside6':metadata.version('PySide6'),'entry':'stockai_desktop.py','source_sha256':hashlib.sha256(json.dumps(sources,sort_keys=True).encode()).hexdigest(),'frontend_sha256':hashlib.sha256((ROOT/'frontend/dist-public/index.html').read_bytes()).hexdigest(),'personal_data_included':False,'requires_python_installation':False,'requires_node_installation':False}
    exe.with_suffix('.exe.build.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(manifest,ensure_ascii=False),flush=True);print(exe,flush=True)


if __name__=='__main__':main()
