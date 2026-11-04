"""Exercise each packager's checksum output at the desktop-updater boundary."""
import ast
import hashlib
from pathlib import Path

import pytest

from backend.upgrades.github import parse_checksum


@pytest.mark.parametrize(('script', 'name'), [
    ('package-exe.py', 'StockAI-3.2.4-Setup.exe'),
    ('package-cloud.py', 'StockAI-3.2.4-Public.zip'),
    ('package-hub.py', 'StockAI-3.2.4-Hub.zip'),
])
def test_packager_checksum_is_accepted_by_installed_updater(tmp_path, monkeypatch, script, name):
    # Run only the real checksum emission, without invoking Qt/PyInstaller or building an archive.
    source = Path(__file__).resolve().parents[1] / 'scripts' / script
    tree = ast.parse(source.read_text(encoding='utf-8'))
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == 'write_text'
             and any(isinstance(value, ast.Constant) and isinstance(value.value, str)
                     and value.value.endswith('.sha256') for value in ast.walk(node))]
    assert len(calls) == 1
    package = tmp_path / name
    package.write_bytes(b'isolated release fixture')
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    write_text = Path.write_text

    def windows_write_text(path, data, encoding=None, errors=None, newline=None):
        # Reproduce Windows' default translation even when this regression runs on Linux.
        return write_text(path, data, encoding=encoding, errors=errors,
                          newline='\r\n' if newline is None else newline)

    monkeypatch.setattr(Path, 'write_text', windows_write_text)
    expression = ast.Expression(body=calls[0])
    scope = dict(output=package, target=package, setup=package,
                 checksum=digest, sha=digest, digest=digest)
    eval(compile(expression, str(source), 'eval'), scope)
    raw = package.with_suffix(package.suffix + '.sha256').read_bytes()
    assert parse_checksum(raw, name) == digest
    assert raw == (digest + '  ' + name + '\n').encode('ascii')
