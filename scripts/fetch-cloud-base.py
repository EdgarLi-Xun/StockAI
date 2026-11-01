"""Fetch a pinned official linux/amd64 base via the registry when WSL DNS is unavailable.
Build/test helper only; no account credentials are read or printed.
"""
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile
import urllib.request
import sys

ROOT=Path(__file__).resolve().parents[1]
MANIFEST="sha256:70bda1e795ded2a684ef9815955e05829b14ee5bb5c3eb2a940648e88f10fce9"
TAG="python:3.13.18-slim-bookworm"
repository="python";output_name="python-base.tar"
if "--caddy" in sys.argv:
    repository="caddy";TAG="caddy:2.11.7-alpine";output_name="caddy-base.tar"
    MANIFEST="sha256:04913b1da43339a40ad1ea87827d70a576ac5aa6cf674c6e24fdca675887533a"
out=ROOT/".tmp"/"cloud-build";out.mkdir(parents=True,exist_ok=True)
token=json.load(urllib.request.urlopen("https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/"+repository+":pull"))["token"]
def get(path,digest=None):
    request=urllib.request.Request("https://registry-1.docker.io/v2/library/"+repository+"/"+path,headers={"Authorization":"Bearer "+token,"Accept":"application/vnd.oci.image.manifest.v1+json"})
    data=urllib.request.urlopen(request,timeout=180).read()
    if digest:
        assert hashlib.sha256(data).hexdigest()==digest.split(":")[1],"镜像摘要不匹配"
    return data
manifest=json.loads(get("manifests/"+MANIFEST,MANIFEST))
config=get("blobs/"+manifest["config"]["digest"],manifest["config"]["digest"])
config_name=manifest["config"]["digest"].split(":")[1]+".json"
archive=out/output_name
with tarfile.open(archive,"w") as tar:
    def add(name,data):
        item=tarfile.TarInfo(name);item.size=len(data);tar.addfile(item,io.BytesIO(data))
    add(config_name,config);layers=[]
    for layer in manifest["layers"]:
        name=layer["digest"].split(":")[1]+"/layer.tar"
        data=gzip.decompress(get("blobs/"+layer["digest"],layer["digest"]))
        add(name,data);layers.append(name)
        print("已核验基础镜像层",layer["digest"],flush=True)
    add("manifest.json",json.dumps([{"Config":config_name,"RepoTags":[TAG],"Layers":layers}]).encode())
(out/(repository+"-metadata.json")).write_text(json.dumps({"tag":TAG,"manifest":MANIFEST,"source":"https://hub.docker.com/_/"+repository},indent=2),encoding="utf-8")
print(archive)
