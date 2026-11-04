"""Drop unused Qt pieces from the desktop bundle before it is packed."""
from __future__ import annotations
from pathlib import Path

# Filename prefixes that WebEngine does not load. Qt6Quick.dll and Qt6Qml.dll stay.
DROP_PREFIXES = (
    'qt6pdf', 'qtpdf', 'qt6shadertools', 'qt6quick3d', 'qtquick3d', 'qt6quickcontrols', 'qtquickcontrols',
    'qt6quickdialogs', 'qt6quickparticles', 'qt6quickeffects', 'qt6quickshapes', 'qt6quicktemplates',
    'qt6quicknativestyle', 'qt6quicklayouts', 'qt6quicktest', 'qt6quickvector', 'qt6labs', 'qt63d', 'qt3d',
    'qt6graphs', 'qtgraphs', 'qt6charts', 'qtcharts', 'qt6location', 'qtlocation', 'qt6multimedia', 'qtmultimedia',
    'qt6datavisualization', 'qtdatavisualization', 'qt6designer', 'qtdesigner', 'qt6qmlcompiler', 'qt6bluetooth',
    'qtbluetooth', 'qt6remoteobjects', 'qtremoteobjects', 'qt6spatialaudio', 'qt6dbus', 'qt6canvaspainter',
    'qt6uitools', 'qtuitools', 'qt6webenginequick', 'qtwebenginequick', 'qt6help', 'qthelp', 'qt6scxml', 'qtscxml',
    'qt6serial', 'qtserial', 'qt6sensors', 'qtsensors', 'qt6virtualkeyboard', 'qt6lottie', 'qt6sql', 'qtsql',
    'qt6test', 'qt6statemachine', 'qt6httpserver', 'qt6nfc', 'qt6texttospeech', 'qt6webview', 'qt6ax', 'qtaxcontainer',
)
QML_KEEP = {'qtquick', 'qtqml', 'qtwebengine', 'qtwebchannel'}
LOCALES = {'zh-cn.pak', 'en-us.pak'}


def keep_bundled(name):
    """Return whether one collected file belongs in the desktop install."""
    low = name.replace('\\', '/').lower()
    base = low.rsplit('/', 1)[-1]
    if base == 'opengl32sw.dll' or base.endswith('.pdb') or base.endswith('.debug.pak') or base.endswith('.debug.bin'):
        return False
    if '/qtwebengine_locales/' in low:
        return base in LOCALES
    if '/translations/' in low and base.endswith('.qm'):
        return 'zh_cn' in base
    if '/qml/' in low:
        return low.split('/qml/', 1)[1].split('/', 1)[0] in QML_KEEP
    if '/plugins/qmltooling/' in low:
        return False
    return not any(base.startswith(prefix) for prefix in DROP_PREFIXES)


def partition(entries):
    """Split PyInstaller TOC entries, expanding directories so debug files can be removed."""
    kept, dropped = [], []
    for item in entries:
        name, src, kind = item[0], item[1], item[2]
        source = Path(src)
        if source.is_dir():
            for file in source.rglob('*'):
                if not file.is_file():
                    continue
                dest = name.replace('\\', '/').rstrip('/') + '/' + file.relative_to(source).as_posix()
                target = (dest, str(file), kind)
                (kept if keep_bundled(dest) else dropped).append(target)
            continue
        (kept if keep_bundled(name) else dropped).append(item)
    return kept, dropped
