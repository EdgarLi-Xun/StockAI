"""Native remembered passwords stay scoped to Windows Credential Manager."""
import os
import subprocess
import sys
import uuid
from types import SimpleNamespace
import json

import pytest

from desktop.credentials import LoginVault


@pytest.mark.skipif(os.name!='nt',reason='Windows Credential Manager integration')
def test_windows_vault_persists_across_processes_and_can_be_forgotten():
    scope='StockAI-test-'+uuid.uuid4().hex
    vault=LoginVault(scope);other=LoginVault(scope+'-other')
    try:
        assert vault.read() is None and other.read() is None
        vault.save('Owner@Example.test','test-vault-password-123-汉字')
        assert vault.read()=={'email':'owner@example.test','password':'test-vault-password-123-汉字'}
        child=subprocess.run([sys.executable,'-c',"from desktop.credentials import LoginVault; import sys; value=LoginVault(sys.argv[1]).read(); assert value and value['email']=='owner@example.test' and value['password']=='test-vault-password-123-汉字'; print('persisted')",scope],capture_output=True,text=True,timeout=20)
        assert child.returncode==0,child.stderr
        assert child.stdout.strip()=='persisted'
        other.clear();assert vault.read() is not None
        vault.save('second@example.test','another-vault-password-456')
        assert vault.read()['email']=='second@example.test'
        vault.clear();vault.clear();assert vault.read() is None
    finally:
        vault.clear();other.clear()


def test_vault_rejects_invalid_inputs_without_writing():
    vault=LoginVault('validation-only')
    for email,password in [('', 'a-long-test-password'),('a\0@example.test','a-long-test-password'),('owner@example.test','short'),('owner@example.test',None)]:
        with pytest.raises(ValueError):vault.save(email,password)


@pytest.mark.parametrize('url',['http://attacker.example:8765','http://127.0.0.1:8766','http://localhost:8765','http://user@127.0.0.1:8765','file:///index.html'])
def test_native_autofill_never_reads_or_writes_for_another_origin(url):
    pytest.importorskip('PySide6')
    from desktop.credentials_bridge import LoginBridge
    calls=[]
    vault=SimpleNamespace(read=lambda:calls.append('read'),save=lambda *args:calls.append('save'),clear=lambda:calls.append('clear'))
    page=SimpleNamespace(url=lambda:SimpleNamespace(toString=lambda:url))
    bridge=LoginBridge(page,'test','http://127.0.0.1:8765',vault=vault)
    for result in (bridge.read(),bridge.save('owner@example.test','a-test-password'),bridge.clear()):
        assert not json.loads(result)['ok']
    assert not calls


def test_native_autofill_errors_are_redacted_and_trusted_origin_can_clear():
    pytest.importorskip('PySide6')
    from desktop.credentials_bridge import LoginBridge
    def fail():raise OSError('SECRET-MUST-NOT-APPEAR')
    calls=[]
    vault=SimpleNamespace(read=fail,clear=lambda:calls.append('clear'))
    page=SimpleNamespace(url=lambda:SimpleNamespace(toString=lambda:'http://127.0.0.1:8765/#login'))
    bridge=LoginBridge(page,'test','http://127.0.0.1:8765',vault=vault)
    assert 'SECRET-MUST-NOT-APPEAR' not in bridge.read()
    assert json.loads(bridge.clear())['ok'] and calls==['clear']
