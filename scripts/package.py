"""Package source and the verified static build, excluding local runtime state."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT=Path(__file__).resolve().parents[1]
EXCLUDED={'.venv','.runtime','.tmp','node_modules','data','reports','cloud-data','cloud-erasures','cloud-secrets','dist','dist-public','build','.git','.codex','.agents','.aws','.gstack','__pycache__','.pytest_cache','.mypy_cache','.ruff_cache','htmlcov'}
EXTENSIONS={'.py','.ps1','.cmd','.md','.json','.txt','.toml','.html','.css','.ts','.vue','.svg','.jpg','.png','.docx','.lock','.yml','.yaml'}
INCLUDED_NAMES={'.gitignore','.dockerignore','.env.example','Dockerfile','Caddyfile'}


def main():
    if not (ROOT/'frontend/dist/index.html').is_file():
        raise SystemExit('缺少 frontend/dist 静态资源，请先构建')
    files=[]
    for current,dirs,names in os.walk(ROOT):
        dirs[:]=[d for d in dirs if d not in EXCLUDED]
        for name in names:
            path=Path(current)/name
            if path.suffix not in EXTENSIONS and name not in INCLUDED_NAMES:
                continue
            if name in {'local.json','secrets.json'} or (name.startswith('.env') and name!='.env.example') or path.is_symlink():
                continue
            files.append(path)
    files += [p for p in (ROOT/'frontend/dist').rglob('*') if p.is_file()]
    folder=ROOT/'dist'; folder.mkdir(exist_ok=True)
    output=folder/'StockAI-2.0.0-Windows.zip'
    temp=output.with_suffix('.zip.tmp')
    with ZipFile(temp,'w',ZIP_DEFLATED) as z:
        for p in sorted(set(files)):
            z.write(p,p.relative_to(ROOT).as_posix())
    with ZipFile(temp) as z:
        if z.testzip() is not None:
            raise SystemExit('ZIP 完整性检查失败')
        names=z.namelist()
        if 'frontend/dist/index.html' not in names or any(n.startswith(('data/','.venv/','.tmp/')) for n in names):
            raise SystemExit('发行内容校验失败')
    temp.replace(output)
    checksum=output.with_suffix('.zip.sha256')
    digest=hashlib.sha256(output.read_bytes()).hexdigest()
    checksum.write_text(f'{digest}  {output.name}\n',encoding='ascii')
    print(output)
    print(f'FILES={len(files)} BYTES={output.stat().st_size}')
    print('SHA256='+digest)
    print(checksum)


if __name__=='__main__':
    main()
