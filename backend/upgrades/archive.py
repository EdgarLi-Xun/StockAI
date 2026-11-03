"""Portable archive names, including Windows alias and traversal rules."""
from pathlib import PurePosixPath
from .trust import UpgradeError


def validate_names(items):
    seen=set();reserved={'con','prn','aux','nul',*(f'com{i}' for i in range(1,10)),*(f'lpt{i}' for i in range(1,10))}
    for item in items:
        path=PurePosixPath(item.filename);name='/'.join(path.parts).casefold()
        if not path.parts or path.is_absolute() or '..' in path.parts or '\\' in item.filename or ':' in item.filename or '\x00' in item.filename or name in seen or ((item.external_attr>>16)&0o170000)==0o120000:raise UpgradeError('PACKAGE','归档路径或文件类型不安全')
        if any(part.endswith((' ','.')) or part.split('.',1)[0].casefold() in reserved for part in path.parts):raise UpgradeError('PACKAGE','归档路径在 Windows 上存在别名或设备名称')
        seen.add(name)
