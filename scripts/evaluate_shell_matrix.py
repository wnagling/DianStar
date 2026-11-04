"""Deterministic Windows execution matrix. No model, network or downloaded commands."""
import argparse
import asyncio
import codecs
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dianstar.core.types import ToolAction
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway
from dianstar.tools.local.project_context import ProjectContext
from dianstar.tools.local.workspace import WorkspaceTools
from dianstar.platform.shells import find_powershell


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def matrix():
    rows=[]
    def add(group, variant, entry, script, expected, **kw):
        rows.append(dict(id=f'matrix-{len(rows)+1:03}',group=group,variant=variant,
            entry=entry,script=script,expected=expected,**kw))
    for root in ('plain','space project','中文项目',"quote'project",'bracket[1]','amp&project','paren(project)','deep/a/b'):
        for entry in ('shell','workspace_run'):
            add('default_listing',root,entry,'Get-ChildItem -Recurse -File | Select-Object -ExpandProperty Name',['sentinel.txt'])
            add('explicit_listing',root,entry,"Get-ChildItem -LiteralPath . -Recurse -File | Select-Object -ExpandProperty Name",['sentinel.txt'])
            add('literal_filename',root,entry,'Get-Content -LiteralPath '+quote("带 空格[1]'x.txt")+ ' -Encoding UTF8',['中文 marker'])
            add('native_cwd',root,entry,'& '+quote(sys.executable)+" -I -S -c 'import os; print(os.getcwd())'",['{root}'])
    for encoding in ('utf8','utf8bom','utf16le','utf16be','crlf','empty'):
        for entry in ('shell','workspace_run'):
            option=' -Encoding UTF8' if encoding in {'utf8','crlf'} else ''
            add('encoding',encoding,entry,'Get-Content -LiteralPath input.txt'+option,
                [] if encoding=='empty' else ['中文 marker'],encoding=encoding)
    for entry in ('shell','workspace_run'):
        add('nonzero','plain',entry,"Write-Output 'before-failure'; exit 7",['before-failure'],exit_code=7)
        add('terminating_error','plain',entry,"throw 'fixture terminating error'",[],nonzero=True)
        add('stderr','plain',entry,"Write-Error 'fixture stderr'; exit 9",[],nonzero=True)
        add('pipeline','plain',entry,"1..100 | Where-Object { $_ % 2 -eq 0 } | Measure-Object -Sum | Select-Object -ExpandProperty Sum",['2550'])
        add('empty_pipeline','plain',entry,"@() | Measure-Object | Select-Object -ExpandProperty Count",['0'])
        add('literal_metacharacters','plain',entry,"Write-Output 'a&b|c;$x[1]中文'",['a&b|c;$x[1]中文'])
        add('large_input','plain',entry,'(Get-Content -LiteralPath large.txt -Encoding UTF8 | Measure-Object -Line).Lines',['20000'],large=True)
    return rows


async def run(spec, output):
    case=output/spec['id'];root=case/spec['variant']/'project';root.mkdir(parents=True)
    (root/'sentinel.txt').write_text('fixture sentinel',encoding='utf-8')
    (root/"带 空格[1]'x.txt").write_text('中文 marker',encoding='utf-8')
    value='中文 marker\r\n' if spec.get('encoding')=='crlf' else '中文 marker\n'
    enc=spec.get('encoding')
    data={'utf8':value.encode(),'utf8bom':codecs.BOM_UTF8+value.encode(),
        'utf16le':codecs.BOM_UTF16_LE+value.encode('utf-16le'),
        'utf16be':codecs.BOM_UTF16_BE+value.encode('utf-16be'),'crlf':value.encode(),'empty':b''}.get(enc,b'')
    (root/'input.txt').write_bytes(data)
    if spec.get('large'): (root/'large.txt').write_bytes(b'fixture\n'*20000)
    before={p.name:p.read_bytes() for p in root.iterdir() if p.is_file()}
    auth=Authorization();context=ProjectContext(auth,roots=(root,))
    workspace=WorkspaceTools(context,case/'unused.env')
    requests=[]
    async def approve(request): requests.append(request['summary']);return True
    gateway=ToolGateway(auth,workspace.registrations(),approve)
    args={'repository':str(root),'timeout_seconds':15}
    if spec['entry']=='shell': args['command']=spec['script']
    else: args['argv']=[str(find_powershell()),'-NoLogo','-NoProfile','-NonInteractive','-Command',spec['script']]
    started=time.monotonic()
    try:
        result=(await gateway.execute(ToolAction(spec['entry'],args))).result
        errors=[]
        if result.get('error'): errors.append(result['error'])
        code=result.get('exit_code')
        if spec.get('nonzero'):
            if not isinstance(code,int) or code==0: errors.append('failure_reported_as_success')
        elif code!=spec.get('exit_code',0): errors.append('wrong_exit_code')
        for expected in spec['expected']:
            expected=expected.replace('{root}',str(root))
            if expected not in result.get('content',''): errors.append('expected_output_missing')
        if result.get('timed_out'): errors.append('unexpected_timeout')
        if result.get('isolation_profile')!='lpac': errors.append('lpac_not_confirmed')
        for name,data in before.items():
            if not (root/name).is_file() or (root/name).read_bytes()!=data: errors.append('input_modified:'+name)
        row={**spec,'passed':not errors,'failures':errors,'elapsed_seconds':round(time.monotonic()-started,3),
            'approvals':len(requests),'result':result}
        (case/'result.json').write_text(json.dumps(row,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:row[k] for k in ('id','group','variant','entry','passed','failures')},ensure_ascii=False),flush=True)
        return row
    finally:
        auth.close();workspace.storage.manager.close()


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True)
    args=parser.parse_args();output=Path(args.output).resolve()
    output.mkdir(parents=True,exist_ok=False)
    rows=[]
    with patch.dict(os.environ,DIANSTAR_FILE_STATE_ROOT=str(output/'state')):
        for spec in matrix():
            rows.append(await run(spec,output))
            (output/'summary.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'total':len(rows),'passed':sum(r['passed'] for r in rows)},ensure_ascii=False))
    return int(any(not r['passed'] for r in rows))


if __name__=='__main__': raise SystemExit(asyncio.run(main()))
