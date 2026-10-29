"""Untrusted WorkspaceEdit proposals; current snapshots, strict UTF-16, no effects."""
import re

from .edit_batch import path_key
from .lsp_client import LspError


def replacement(text, edits, check):
    if not isinstance(edits, list) or len(edits) > 512:
        raise LspError('LSP rename edits must be an array of at most 512 text edits')
    starts, ends, previous = [0], [], 0
    for match in re.finditer(r'\r\n|\r|\n', text):
        check()
        ends.append(match.start());starts.append(match.end())
    ends.append(len(text))
    def offset(point):
        if (not isinstance(point, dict) or set(point) != {'line', 'character'}
                or type(point['line']) is not int or type(point['character']) is not int
                or not 0 <= point['line'] < len(starts) or point['character'] < 0):
            raise LspError('invalid LSP rename position')
        line = text[starts[point['line']]:ends[point['line']]].encode('utf-16-le')
        if point['character'] * 2 > len(line):
            raise LspError('LSP rename position exceeds the current line; no clamping')
        try:
            return starts[point['line']] + len(line[:point['character'] * 2].decode('utf-16-le'))
        except UnicodeError:
            raise LspError('LSP rename range splits a UTF-16 surrogate pair') from None
    spans = []
    for index, edit in enumerate(edits):
        check()
        if (not isinstance(edit, dict) or set(edit) != {'range', 'newText'} or not isinstance(edit['newText'], str)
                or not isinstance(edit['range'], dict) or set(edit['range']) != {'start', 'end'}):
            raise LspError('LSP rename accepts plain text edits only; annotations and commands are unsupported')
        start, stop = offset(edit['range']['start']), offset(edit['range']['end'])
        if stop < start:
            raise LspError('LSP rename range is reversed')
        spans.append((start, stop, index, edit['newText']))
    spans.sort(key=lambda row:(row[0], row[1], row[2]))
    chunks = []
    for start, stop, _, new in spans:
        check()
        if start < previous:
            raise LspError('LSP rename text edits overlap')
        chunks.extend((text[previous:start],new));previous=stop
    chunks.append(text[previous:])
    after=''.join(chunks)
    if len(after.encode('utf-8')) > 512*1024 or '\0' in after:
        raise LspError('LSP rename result exceeds 512 KiB or contains binary text')
    if text.startswith('\ufeff') != after.startswith('\ufeff'):
        raise LspError('LSP rename must preserve the UTF-8 BOM')
    return after


def prepare(raw, path_from_uri, document, synchronized, check):
    if raw is None:
        return []
    if not isinstance(raw, dict) or set(raw)-{'changes','documentChanges'}:
        raise LspError('LSP rename requires a plain WorkspaceEdit; resource operations/annotations are unsupported')
    if 'changes' in raw and not isinstance(raw['changes'], dict):
        raise LspError('invalid LSP rename changes map')
    if 'documentChanges' in raw:
        rows=raw['documentChanges']
        if not isinstance(rows,list):
            raise LspError('invalid LSP rename documentChanges')
        entries=[]
        for row in rows:
            if (not isinstance(row,dict) or set(row)!={'textDocument','edits'}
                    or not isinstance(row['textDocument'],dict) or set(row['textDocument'])!={'uri','version'}):
                raise LspError('LSP rename accepts existing text documents only; no resource operations')
            entries.append((row['textDocument']['uri'],row['textDocument']['version'],row['edits']))
    else:
        entries=[(uri,None,edits) for uri,edits in raw.get('changes',{}).items()]
    if len(entries)>16:
        raise LspError('LSP rename exceeds the existing 16-file edit batch limit')
    plans, seen, total=[],set(),0
    for uri,version,edits in entries:
        check()
        path=path_from_uri(uri)
        current=document(path)
        key=path_key(tuple(current['path'].split('/')))
        if key in seen:
            raise LspError('LSP rename contains duplicate/aliased document targets')
        seen.add(key)
        if version is not None:
            known=next((row for row in synchronized.values() if path_key(tuple(row['path'].split('/')))==key),None)
            if (type(version) is not int or not -2147483648<=version<=2147483647 or known is None
                    or version!=known['version'] or current['sha256']!=known['sha256']):
                raise LspError('LSP rename document version is not current; no edits prepared')
        after=replacement(current['text'],edits,check)
        total+=len(edits)
        if total>512:
            raise LspError('LSP rename exceeds 512 total text edits')
        plans.append({'document':current,'after':after,'edits':len(edits),'version_confirmed':version is not None})
    for plan in plans:
        check()
        if document(plan['document']['path'])['sha256']!=plan['document']['sha256']:
            raise LspError('LSP rename target changed while preparing the batch')
    return plans
