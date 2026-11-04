# 打包公网部署所需的源码、静态资源和部署说明，排除本机私密运行数据。
"""Build a reviewable public-edition source/static bundle without local state or keys."""
from __future__ import annotations

import hashlib
import json
import ast
import re
from pathlib import Path
from zipfile import ZipFile,ZIP_DEFLATED

ROOT=Path(__file__).resolve().parents[1]
EXCLUDED={"__pycache__","node_modules","dist",".pytest_cache",".tmp",".git",".agents",".codex",".aws"}
EXTENSIONS={".py",".md",".ts",".vue",".json",".css",".html",".js",".svg",".png",".jpg",".lock",".toml",".yml",".ps1"}


def edition_version():
    tree=ast.parse((ROOT/'backend/cloud/__init__.py').read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='VERSION' for t in node.targets):
            version=ast.literal_eval(node.value)
            if isinstance(version,str) and re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?',version):return version
    raise SystemExit('公网版本号无效')


# 校验公网前端构建，筛选发行文件并生成归档与 SHA-256 清单。
def main():
    version=edition_version()
    if not (ROOT/"frontend/dist-public/index.html").is_file():
        raise SystemExit("请先在 frontend 运行 npm run build:public")
    selected=[]
    for folder in ("backend","desktop","frontend","deploy/cloud","deploy/hub","docs","tests"):
        for path in (ROOT/folder).rglob("*"):
            if not path.is_file() or path.is_symlink() or EXCLUDED&set(path.relative_to(ROOT).parts):
                continue
            if path.relative_to(ROOT).parts[:2]==('docs','evidence') and path.name=='artifacts.json':
                continue  # 包本身的最终摘要单独交付，避免把自身摘要递归写入归档。
            if path.name.startswith(".env") and path.name!=".env.example":
                continue
            if path.suffix in EXTENSIONS or path.name in {"Dockerfile","Caddyfile",".env.example"}:
                selected.append(path)
    selected.extend(ROOT/name for name in ("CONTEXT.md","pyproject.toml","requirements.txt","requirements-cloud.txt","requirements-cloud.lock","requirements-hub.txt","requirements-hub.lock","stockai_desktop.py",".dockerignore"))
    selected.extend(ROOT/"config"/name for name in ("prompt-standard.md","prompt-full.md","assistant-knowledge.json"))
    selected.extend(ROOT/"scripts"/name for name in ("package-cloud.py","package-hub.py","release-sign.py","preview-cloud.py","build-cloud.ps1","fetch-cloud-base.py"))
    files={p.relative_to(ROOT).as_posix():p.read_bytes() for p in sorted(set(selected))}
    files["README.md"]=(f"# StockAI 公网研究工作台 {version}\n\n本包包含独立公网入口、已构建的网页及源码。\n\n"
        "请先阅读 [部署与初始化说明](deploy/cloud/README.md) 和 [验收记录](docs/验收记录-公网多用户与模型接入.md)。\n\n"
        "研究增强与助手的操作和验收见 docs/使用说明-研究工作台与AI助手.md、docs/验收记录-研究工作台与AI助手.md。预发布构建仍需完成真实模型、SMTP及目标主机联调。\n\n"
        "本轮同步、独立 Hub、签名升级和回滚见 docs/使用说明-同步与升级.md、docs/验收记录-同步与升级.md。\n\n"
        "适用于 Linux x86_64 + Docker Compose。需要本人配置域名、SMTP、备份接收公钥和模型凭据。\n\n"
        "包内没有真实账号、Key、本地数据库、会话、备份或主密钥；不会自动迁移本地版数据。\n").encode()
    forbidden=("data/","cloud-data/","cloud-erasures/","cloud-secrets/",".tmp/",".venv/",".git/")
    if any(name.startswith(forbidden) or name.endswith((".sqlite3",".age",".env","keys.json","secrets.json")) for name in files):
        raise SystemExit("发行文件安全检查未通过")
    manifest={"edition":"public","version":version,"stage":"preview" if '-' in version else 'release',"files":{name:hashlib.sha256(value).hexdigest() for name,value in files.items()}}
    files["MANIFEST.json"]=json.dumps(manifest,ensure_ascii=False,indent=2).encode()
    output=ROOT/"dist"/f"StockAI-{version}-Public.zip";output.parent.mkdir(exist_ok=True)
    pending=output.with_suffix(".zip.tmp")
    with ZipFile(pending,"w",ZIP_DEFLATED) as z:
        for name,value in sorted(files.items()):
            z.writestr(name,value)
    with ZipFile(pending) as z:
        if z.testzip():
            raise SystemExit("ZIP 完整性核验失败")
    pending.replace(output)
    checksum=hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".zip.sha256").write_text(checksum+"  "+output.name+"\n",encoding="ascii",newline="\n")
    print(output);print(f"FILES={len(files)} BYTES={output.stat().st_size} SHA256={checksum}")


if __name__=="__main__":
    main()
