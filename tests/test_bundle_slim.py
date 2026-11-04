from pathlib import Path
from scripts.bundle_slim import keep_bundled, partition


def test_webengine_runtime_stays_and_unused_qt_files_go():
    kept = [
        r'PySide6\Qt6WebEngineCore.dll', r'PySide6\Qt6Quick.dll', r'PySide6\Qt6Qml.dll', r'PySide6\Qt6QmlModels.dll',
        r'PySide6\Qt6QuickWidgets.dll', r'PySide6\Qt6Positioning.dll', r'PySide6\Qt6OpenGL.dll', 'PySide6/QtQuick.pyd',
        'PySide6/QtQml.pyd', 'PySide6/resources/qtwebengine_devtools_resources.pak', 'PySide6/resources/icudtl.dat',
        'PySide6/resources/v8_context_snapshot.bin', 'PySide6/translations/qtwebengine_locales/zh-CN.pak',
        'PySide6/translations/qtwebengine_locales/en-US.pak', 'PySide6/translations/qt_zh_CN.qm', 'PySide6/qml/QtQuick/qmldir',
        'PySide6/qml/QtWebEngine/qmldir', 'PySide6/plugins/platforms/qwindows.dll',
    ]
    dropped = [
        'PySide6/opengl32sw.dll', 'PySide6/resources/qtwebengine_devtools_resources.debug.pak',
        'PySide6/resources/qtwebengine_resources.debug.pak', 'PySide6/resources/v8_context_snapshot.debug.bin',
        'PySide6/translations/qtwebengine_locales/de.pak', 'PySide6/translations/qt_de.qm', 'PySide6/Qt6Pdf.dll',
        'PySide6/Qt6Quick3D.dll', 'PySide6/Qt6QuickControls2Material.dll', 'PySide6/Qt6Charts.dll', 'PySide6/Qt6Designer.dll',
        'PySide6/Qt3DRender.pyd', 'PySide6/QtPdf.pyd', 'PySide6/qml/QtQuick3D/qmldir', 'PySide6/qml/QtCharts/qmldir',
        'PySide6/plugins/qmltooling/qmldbg_native.dll',
    ]
    assert all(keep_bundled(name) for name in kept)
    assert not any(keep_bundled(name) for name in dropped)


def test_a_resource_directory_keeps_release_files_only(tmp_path):
    folder = tmp_path / 'resources'
    folder.mkdir()
    (folder / 'qtwebengine_resources.pak').write_bytes(b'release')
    (folder / 'qtwebengine_resources.debug.pak').write_bytes(b'debug')
    kept, dropped = partition([('PySide6/resources', str(folder), 'DATA')])
    assert [item[0] for item in kept] == ['PySide6/resources/qtwebengine_resources.pak']
    assert [item[0] for item in dropped] == ['PySide6/resources/qtwebengine_resources.debug.pak']
    assert Path(kept[0][1]).name == 'qtwebengine_resources.pak'
