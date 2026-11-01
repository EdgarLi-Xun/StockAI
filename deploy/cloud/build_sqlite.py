"""Build the upstream WAL-reset-fixed SQLite with its published SHA3-256 digest."""
import hashlib
from pathlib import Path
import subprocess
import tarfile
import urllib.request

URL="https://sqlite.org/2026/sqlite-autoconf-3540100.tar.gz"
SHA3="b6ced585c750916b1052667239a12c25a7ff34e7f06e1e6e01ecb0427657d9c4"
root=Path.cwd();archive=root/"sqlite.tar.gz"
if not archive.is_file():
    archive.write_bytes(urllib.request.urlopen(URL,timeout=90).read())
assert hashlib.sha3_256(archive.read_bytes()).hexdigest()==SHA3,"SQLite 下载校验失败"
with tarfile.open(archive) as t:
    member=t.getmember("sqlite-autoconf-3540100/sqlite3.c")
    (root/"sqlite3.c").write_bytes(t.extractfile(member).read())
subprocess.run(["gcc","-O2","-fPIC","-shared","-DSQLITE_THREADSAFE=1","-DSQLITE_ENABLE_FTS5","-DSQLITE_ENABLE_JSON1","-DSQLITE_ENABLE_RTREE","-DSQLITE_ENABLE_COLUMN_METADATA","-Wl,-soname,libsqlite3.so.0","sqlite3.c","-o","libsqlite3.so.0","-lm","-ldl","-lpthread"],check=True)
