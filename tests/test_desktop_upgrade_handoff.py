"""Exercise the desktop timer callback without starting Qt or a real upgrade."""
import ast
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from desktop.application import keep_source_process, running_version_is_older


def timer_callback(monkeypatch, tmp_path, mode, status='succeeded', target='3.2.16'):
    from desktop import upgrade
    monkeypatch.setattr(sys, 'gettrace', lambda: object() if mode == 'trace' else None)
    for name in ('pydevd', 'debugpy'):
        monkeypatch.delitem(sys.modules, name, raising=False)
    if mode in ('pydevd', 'debugpy'):
        monkeypatch.setitem(sys.modules, mode, object())
    calls = []
    monkeypatch.setattr(upgrade, 'handoff', lambda *args: calls.append('handoff'))
    transaction = {'status': status, 'actual_version': target, 'target_version': target}
    namespace = {
        'args': SimpleNamespace(no_forward=mode == 'no-forward'), 'home': tmp_path,
        'upgrade_manager_instance': SimpleNamespace(current=lambda: transaction),
        'quitting': {'value': False}, 'VERSION': '3.2.15',
        'keep_source_process': keep_source_process, 'running_version_is_older': running_version_is_older,
        'quit_all': lambda: calls.append('quit'),
    }
    tree = ast.parse(Path('desktop/application.py').read_text(encoding='utf-8'))
    callback = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == 'upgrade_handoff')
    exec(compile(ast.Module(body=[callback], type_ignores=[]), 'desktop/application.py', 'exec'), namespace)
    return namespace['upgrade_handoff'], calls


@pytest.mark.parametrize('mode', ['no-forward', 'trace', 'pydevd', 'debugpy'])
def test_historical_success_does_not_quit_a_requested_source_session(monkeypatch, tmp_path, mode):
    callback, calls = timer_callback(monkeypatch, tmp_path, mode)
    callback()
    assert calls == []


def test_normal_old_desktop_still_exits_after_successful_upgrade(monkeypatch, tmp_path):
    callback, calls = timer_callback(monkeypatch, tmp_path, 'normal')
    callback()
    assert calls == ['quit']


@pytest.mark.parametrize('mode', ['normal', 'no-forward', 'trace', 'debugpy'])
def test_explicit_upgrade_request_still_hands_off_and_exits(monkeypatch, tmp_path, mode):
    callback, calls = timer_callback(monkeypatch, tmp_path, mode, status='exit_requested')
    callback()
    assert calls == ['handoff', 'quit']


@pytest.mark.parametrize('target', ['3.2.14', '3.2.15'])
def test_current_or_newer_desktop_does_not_exit_for_old_success(monkeypatch, tmp_path, target):
    callback, calls = timer_callback(monkeypatch, tmp_path, 'normal', target=target)
    callback()
    assert calls == []
