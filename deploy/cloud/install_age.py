# 按固定版本和摘要安装 age 加密工具，供公网备份和恢复使用。
import hashlib
import io
from pathlib import Path
import tarfile
import urllib.request

URL="https://github.com/FiloSottile/age/releases/download/v1.3.2/age-v1.3.2-linux-amd64.tar.gz"
SHA256="cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10"
raw=urllib.request.urlopen(URL,timeout=120).read()
# 下载内容必须匹配固定摘要，核验失败时不安装可执行文件。
assert hashlib.sha256(raw).hexdigest()==SHA256,"age 下载校验失败"
with tarfile.open(fileobj=io.BytesIO(raw)) as t:
    # 只提取预期的加密与密钥生成程序，并设置可执行权限。
    for name in ("age","age-keygen"):
        target=Path("/usr/local/bin")/name
        target.write_bytes(t.extractfile("age/"+name).read());target.chmod(0o755)
