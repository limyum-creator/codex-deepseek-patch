"""Restore official update discovery for Windows's unpackaged managed copy."""
from pathlib import Path
import re
from compatibility import CompatibilityError, _balanced_end, IDENT

MARKER = 'CodexPatchManagedWindowsUpdates'


def require(value, message):
    if not value:
        raise CompatibilityError(message)


def apply_source(source):
    require(MARKER not in source, 'Windows update adapter is already present')
    anchor = 'async initializeWindowsUpdater(){'
    require(source.count(anchor) == 1, 'Missing or ambiguous Windows updater initialization')
    begin = source.index(anchor)
    end = _balanced_end(source, begin + len(anchor) - 1)
    method = source[begin:end]
    pattern = (rf'if\(await (?P<updater>{IDENT})\.initialize\(\),!(?P=updater)\.hasUpdater\(\)\)'
               rf'\{{this\.lastUnavailableReason=(?P=updater)\.getUnavailableReason\(\);return\}}')
    targets = list(re.finditer(pattern, method))
    require(len(targets) == 1, 'Unknown Windows updater failure contract')
    bindings = list(re.finditer(
        rf'(?P<interval>{IDENT})={IDENT}\(\),(?P<options>{IDENT})=\{{nativeAddon:{IDENT},offers:(?P<offers>{IDENT}),allowed:', method))
    require(len(bindings) == 1, 'Unknown Windows updater options contract')
    stores = list(re.finditer(rf'(?P<store>{IDENT})={IDENT}\.i\.getWindowsStoreConfig\(', method))
    require(len(stores) == 1, 'Unknown Windows distribution contract')
    hit, names, store = targets[0], bindings[0].groupdict(), stores[0]['store']
    updater = hit['updater']
    old = hit.group()
    new = (f'await {updater}.initialize();'
           f'if(process.platform===`win32`&&!{updater}.hasUpdater()&&'
           f'{updater}.getUnavailableReason()===`missing current windows package family`)'
           f'{{{updater}=new CodexPatchManagedWindowsUpdater({{...{names["options"]},'
           f'checkIntervalMs:{names["interval"]},storeProductId:{store}?.storeProductId,logger:this.logger}});await {updater}.initialize()}}'
           f'if(!{updater}.hasUpdater()){{this.lastUnavailableReason={updater}.getUnavailableReason();return}}')
    helper = (Path(__file__).parent/'windows_update_ui.js').read_text(encoding='utf-8')
    start, stop = begin + hit.start(), begin + hit.end()
    patched = helper + '\n' + source[:start] + new + source[stop:]
    return patched, {'begin': start, 'end': stop, 'old': old, 'new': new,
                     'helper': helper, 'contract': 'windows-managed-official-update-check-v1'}


def locate_bundle(archive):
    import patch as p
    header, base, _ = p.archive_header(archive)
    matches = []
    for name, item in p.entries(header):
        if not name.startswith('.vite/build/') or not name.endswith('.js') or item.get('unpacked'):
            continue
        data = p.read_member(archive, item, base)
        if b'async initializeWindowsUpdater(){' in data:
            p.check_member(data, item)
            matches.append((name, data.decode('utf-8')))
    require(len(matches) == 1, 'Cannot identify one Windows updater module')
    name, source = matches[0]
    patched, evidence = apply_source(source)
    return name, patched, evidence
