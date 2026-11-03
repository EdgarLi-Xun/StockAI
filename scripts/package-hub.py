"""Create an independent Hub source package without research state or Qt dependencies."""
from pathlib import Path
import hashlib
import json
import sys
import zipfile
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend.cloud import VERSION


def main():
    files={}
    for folder in ('backend','deploy/hub','deploy/cloud'):
        for p in (ROOT/folder).rglob('*'):
            if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc','.sqlite3','.age') and (not p.name.startswith('.env') or p.name=='.env.example'):files[p.relative_to(ROOT).as_posix()]=p.read_bytes()
    for name in ('requirements-hub.txt','requirements-hub.lock','pyproject.toml','docs/使用说明-同步与升级.md','docs/验收记录-同步与升级.md','docs/修复记录-助手对话误拦截.md','docs/验收清单-同步与独立部署.md','docs/验收清单-版本升级与回滚.md','docs/PRD-同步与独立部署.md','docs/PRD-版本升级与回滚.md','scripts/release-sign.py'):files[name]=(ROOT/name).read_bytes()
    for p in (ROOT/'docs/evidence/sync-upgrade-2026-11-02').rglob('*'):
        if p.is_file() and not p.is_symlink() and p.name!='artifacts.json' and p.suffix in ('.json','.png'):files[p.relative_to(ROOT).as_posix()]=p.read_bytes()
    for p in (ROOT/'docs/evidence/assistant-fix-2026-11-02').rglob('*'):
        if p.is_file() and not p.is_symlink() and p.name!='artifacts.json' and p.suffix in ('.json','.png'):files[p.relative_to(ROOT).as_posix()]=p.read_bytes()
    files['MANIFEST.json']=json.dumps({'component':'hub','version':VERSION,'files':{n:hashlib.sha256(v).hexdigest() for n,v in files.items()},'personal_data_included':False},indent=2).encode()
    target=ROOT/'dist'/('StockAI-'+VERSION+'-Hub.zip');target.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
        for name,data in sorted(files.items()):z.writestr(name,data)
    sha=hashlib.sha256(target.read_bytes()).hexdigest();target.with_suffix('.zip.sha256').write_text(sha+'  '+target.name+'\n',encoding='ascii');print(str(target));print(sha)


if __name__=='__main__':main()
