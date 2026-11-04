"""Windows task acceptance through the real DianStar loop; no downloaded code executes."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
# Import runtime before per-case state changes: SDK ACL leases have a host lifetime.
from dianstar.app.runtime import run_turn
from dianstar.app.project_context import create_project_context
from dianstar.config.mcp import MCPSettings
from dianstar.config.paths import ENV_FILE
from dianstar.config.projects import ProjectSettings
from dianstar.config.models import load_model_settings
from dianstar.security.tool_policy import ToolPolicy
from dianstar.tools.local.project_context import ProjectContext

FILES = {'a.txt': 'foo\nneedle\n\nalpha\nneedle\n', 'sub/b.txt': 'Beta\nneedle\n',
         'space name.txt': 'hello\n', 'zero.txt': '', 'sub/C.TXT': 'gamma\n',
         'app.log': 'INFO start\nERROR broken\nINFO end\nERROR stopped\n',
         'numbers.dat': '9\n2\n15\n2\n', 'words.dat': 'pear\napple\npear\nbanana\n',
         'left.dat': 'a\nb\nc\n', 'right.dat': 'b\nc\nd\n',
         'first.csv': 'name,value\na,1\nb,2\n', 'second.csv': 'name,value\na,1\nb,3\n',
         'pictures/one.jpg': 'a'*9, 'pictures/two.jpg': 'b'*4}


def cases():
    result = []
    def add(source, question, expected=None, *, ordered=True, check=None):
        result.append({'id': f'task-{len(result)+1:02}', 'source_line': source,
            'question': question, 'expected': expected, 'ordered': ordered, 'check': check})
    sizes = {name: len(data.encode()) for name, data in FILES.items()}
    add(1611, '递归找出字节数最大的三个文件，按从大到小列出相对路径。', sorted(sizes, key=lambda n: -sizes[n])[:3])
    add(1612, '递归找出非空文件中字节数最小的三个，按从小到大列出相对路径。', sorted((n for n in sizes if sizes[n]),key=lambda n:sizes[n])[:3])
    txt = sorted(n for n in FILES if n.lower().endswith('.txt'))
    add(913, '递归列出扩展名为 txt 的文件，扩展名不区分大小写。', txt, ordered=False)
    add(551, '只列出当前目录这一层的 txt 文件，不进入子目录。', [n for n in txt if '/' not in n], ordered=False)
    add(531, '列出名称中包含空格的文件相对路径。', ['space name.txt'])
    add(531, '递归列出包含字面文本 needle 的文件相对路径，每个文件只列一次。', ['a.txt','sub/b.txt'], ordered=False)
    add(32, '从 a.txt 提取所有包含 needle 的行，保留原顺序与重复。', ['needle','needle'])
    add(905, '统计 a.txt 中非空白行的数量。', ['4'])
    add(1587, '提取 a.txt 最后两行，保留顺序。', ['alpha','needle'])
    add(1587, '提取 a.txt 最前三行，保留其中的空行。', ['foo','needle',''])
    add(551, '将 words.dat 的行去重并按字母升序排列。', ['apple','banana','pear'])
    add(2704, '将 numbers.dat 按数值升序排列，保留重复数字。', ['2','2','9','15'])
    add(1532, '将 numbers.dat 的行按原顺序倒序输出，不是数值排序。', ['2','15','2','9'])
    add(913, '统计所有子目录在内的 txt 文件数量，扩展名不区分大小写。', [str(len(txt))])
    add(334, '统计 pictures 中所有 jpg 文件的总字节数。', ['13'])
    add(261, '计算 a.txt 的 SHA256，只输出十六进制摘要。', [hashlib.sha256(FILES['a.txt'].encode()).hexdigest()])
    add(604, '输出 left.dat 与 right.dat 共有的行，按字母排序。', ['b','c'])
    add(604, '输出仅在 left.dat 出现而不在 right.dat 出现的行。', ['a'])
    add(551, '检查 words.dat 是否存在重复行，存在只输出 yes，否则 no。', ['yes'])
    add(308, '计算当前目录和子目录中所有现有文件总字节数。', [str(sum(sizes.values()))])
    add(1613, '递归列出所有零字节普通文件相对路径。', ['zero.txt'])
    add(222, '递归列出所有空目录相对路径。', ['empty'])
    add(32, '从 app.log 中提取 ERROR 开头的行，保留顺序。', ['ERROR broken','ERROR stopped'])
    add(597, '比较 first.csv 和 second.csv 的内容，一致输出 same，不一致输出 different。', ['different'])
    add(106, '为 a.txt 的每行末尾追加 | COUNTRY，保留空行，不修改原文件，将处理结果作为答案。', [v+'| COUNTRY' for v in FILES['a.txt'].splitlines()])
    add(248, '将 a.txt 原样复制为 backup.txt。', check={'backup.txt':FILES['a.txt']})
    add(456, '把 space name.txt 重命名为 space name.bak，保留内容。', check={'space name.bak':FILES['space name.txt'],'space name.txt':None})
    add(453, '把 a.txt 中 foo 精确替换成 bar，其他内容保持。', check={'a.txt':FILES['a.txt'].replace('foo','bar')})
    add(5507, '将 left.dat 与 right.dat 的全部行合并后按字母排序，保留重复。', ['a','b','b','c','c','d'])
    add(875, '将 sub 的目录结构复制到 skeleton，并为其中每个文件创建同名零字节文件，不复制原正文。', check={'skeleton/b.txt':'','skeleton/C.TXT':''})
    return result


def fixture_bytes(case):
    values={name:value.encode() for name,value in FILES.items()}
    for name,value in case.get('extra_files',{}).items():
        values[name]=value['text'].encode(value['encoding']) if isinstance(value,dict) else value.encode()
    return values


def edge_cases():
    specs=[
        ('中文目录','读取 中文 文件.txt 的全部行。',['第一行','第二行'],{'中文 文件.txt':'第一行\n第二行\n'}),
        ("quote'project","读取 quote'file.txt 全文。",['quote fixture'],{"quote'file.txt":'quote fixture\n'}),
        ('bracket[1]','按字面文件名读取 [sample].txt，不要当作通配符。',['literal brackets'],{'[sample].txt':'literal brackets\n'}),
        ('project','读取 bom.txt 的两行文字，不把 BOM 当作正文。',['中文','marker'],{'bom.txt':{'text':'中文\nmarker\n','encoding':'utf-8-sig'}}),
        ('project','读取 unicode.txt，这是带 BOM 的 UTF-16 文件。',['中文','marker'],{'unicode.txt':{'text':'中文\nmarker\n','encoding':'utf-16'}}),
        ('space project','统计 mixed.txt 的非空白行数。',['2'],{'mixed.txt':'a\r\n\r\n  \r\nb\r\n'}),
        ('project','提取 literals.dat 中包含字面文本 a.b[1]$ 的行，不使用正则解释。',['a.b[1]$'],{'literals.dat':'a.b[1]$\naxb1\na.b[2]$\n'}),
        ('deep/a/b','递归统计扩展名为 .item 的普通文件数量。',['300'],{f'items/sub{i%10}/item{i:03}.item':'fixture\n' for i in range(300)}),
        ('project','统计 long.dat 第一行的字符数，不计换行。',['8000'],{'long.dat':'a'*8000+'\n'}),
        ('amp&project','按字面文件名读取 dollar$&file.txt，不展开其中符号。',['literal dollar'],{'dollar$&file.txt':'literal dollar\n'}),
        ('project','读取 zero.txt 的全文作为答案，原文件为空，因此答案文件也应为空。',[],{}),
        ('project','先实际运行 fail.py，记录其退出码；再运行 recover.py，记录其退出码。答案只写两行退出码，顺序不得颠倒。',['7','0'],
            {'fail.py':"raise SystemExit(7)\n",'recover.py':"print('recovered')\n"}),
        ('project','用PowerShell依次查询引擎主版本号整数、PSEdition，并用System.IO.Path.GetRelativePath计算sub/b.txt相对于当前项目的路径，答案依次三行。',['7','Core','sub/b.txt'],{})]
    return [dict(id=f'edge-{i:02}',source_line=None,project_name=name,question=question,
        expected=expected,ordered=True,check=None,extra_files=extra,clarified=True,
        **({'exit_sequence':[7,0]} if i==12 else {})) for i,(name,question,expected,extra) in enumerate(specs,1)]


def seed(root,case):
    root.mkdir(parents=True)
    for name, value in fixture_bytes(case).items():
        path=root/name; path.parent.mkdir(exist_ok=True,parents=True)
        path.write_bytes(value)
    (root/'empty').mkdir()


def judge(root, case):
    errors=[]
    if case['expected'] is not None:
        path=root/'answer.txt'
        if not path.is_file():
            errors.append('missing_answer_file')
        try:
            lines=path.read_text(encoding='utf-8-sig').replace('\\','/').splitlines() if path.is_file() else []
        except UnicodeError:
            errors.append('answer_encoding_unsupported');lines=[]
        expected=case['expected']
        if not case['ordered']:
            lines,expected=sorted(lines),sorted(expected)
        ranked = case.get('original_id',case['id']) in {'task-01','task-02'}
        sizes={name:len(value.encode()) for name,value in FILES.items()}
        ranking_ok = ranked and len(lines)==len(expected) and len(set(lines))==len(lines) and all(n in sizes for n in lines) and [sizes[n] for n in lines]==[sizes[n] for n in expected]
        if lines!=expected and not ranking_ok:
            errors.append('answer_mismatch')
    checks=case.get('check') or {}
    for name,expected in checks.items():
        path=root/name
        if expected is None:
            if path.exists(): errors.append('source_not_removed:'+name)
        elif not path.is_file() or path.read_bytes()!=expected.encode():
            errors.append('wrong_file:'+name)
    # Preserve all input files except explicitly requested mutations.
    for name,expected in fixture_bytes(case).items():
        if name not in checks and (not (root/name).is_file() or (root/name).read_bytes()!=expected):
            errors.append('unexpected_input_change:'+name)
    return errors


def sequence_errors(case, results):
    if not case.get('exit_sequence'):
        return []
    needed=iter(case['exit_sequence']);wanted=next(needed,None)
    for item in results:
        value=json.loads(item['content'])
        code=value.get('exit_code')
        if code is not None and code==wanted: wanted=next(needed,None)
    return ['required_exit_sequence_missing'] if wanted is not None else []


async def evaluate(case, output, *, development=False):
    root=(output/case['id']/case.get('project_name','project')).resolve()
    if root.exists(): raise ValueError('Use a fresh output directory')
    if development:
        root.mkdir(parents=True)
        if case.get('broken'):
            (root/'main.py').write_text("print('repair-ok'\n",encoding='utf-8')
    else:
        seed(root,case)
    events=[]; approvals=[]; history=[]; start=time.monotonic(); error=None
    async def approve(request):
        # Automated consent is limited to these disposable projects. Network,
        # installs, project switches and account actions are outside this run.
        description=request.get('description','')
        try: payload=json.loads(description)
        except ValueError: payload={}
        allowed=not payload.get('允许互联网',False)
        approvals.append({'summary':request.get('summary',''), 'approved':allowed})
        return allowed
    def context(auth, directory, path, **kwargs):
        return ProjectContext(auth, ProjectSettings(allow_external_paths=False),roots=(root,)),{}
    policies={name:ToolPolicy(enabled=False) for name in ('project.open','workspace.storage',
        'search_web','read_webpage','manage_web_access','get_weather')}
    question=(f'在当前测试目录 {root} 完成：{case["question"]}\n'
        '查询题将答案写入 answer.txt，每行一项，不加标题或解释；修改题直接修改指定文件。'
        '请至少实际执行一次 Windows 命令工具（shell 或 workspace_run）完成或核对任务。'
        '不要修改其他输入文件，不联网，不安装软件，不提交 Git，不处理其他目录。')
    if development: question=case['question']+f'\n项目目录：{root}。只在此目录完成，不联网、不安装、不提交Git。'
    elif case.get('clarified'):
        question=(f'只读查询任务，项目目录为 {root}。{case["question"]}\n'
            '不得修改任何现有输入文件；只将查询结果写入 answer.txt，每行一项，不加解释。'
            '至少实际执行一次 shell 或 workspace_run。不联网，不安装，不提交Git，不处理其他目录。')
    try:
        with patch.dict(os.environ,DIANSTAR_FILE_STATE_ROOT=str(output/'state')), \
             patch('dianstar.app.runtime.load_mcp_settings',return_value=MCPSettings(git_enabled=False,github_enabled=False)), \
             patch('dianstar.app.runtime.create_project_context',side_effect=context):
            answer=await asyncio.wait_for(run_turn(question,project_directory=root,env_file=ENV_FILE,
                history=history,on_tool=events.append,on_permission=approve,tool_policies=policies,
                conversation_id='shell-eval-'+case['id']),300)
    except Exception as exc:
        error=type(exc).__name__+': '+str(exc)
        answer=''
    completed=[e for e in events if e['phase']=='completed']
    executed=[e for e in completed if e['tool'] in {'shell','workspace_run'} and e['status'] in {'success','warning'}]
    failures=judge(root,case) if not development else []
    failures+=sequence_errors(case,[item for item in history if item.get('role')=='tool'])
    if development:
        observed=[]
        exit_codes=[]
        for message in history:
            if message.get('role')=='tool':
                value=json.loads(message['content'])
                if value.get('exit_code') is not None: exit_codes.append(value['exit_code'])
                if value.get('success') and value.get('exit_code')==0:
                    observed.append(value.get('content',''))
        if not any(case['required_output'] in text for text in observed):
            failures.append('expected_program_output_not_observed')
        if not (root/case['source_file']).is_file():
            failures.append('source_file_missing')
        if case.get('broken') and not any(code!=0 for code in exit_codes):
            failures.append('initial_failure_not_observed')
    if not executed: failures.append('no_successful_command')
    if error: failures.append('runtime_error')
    row={'id':case['id'],'source_line':case.get('source_line'),'elapsed_seconds':round(time.monotonic()-start,2),
        'passed':not failures,'failures':failures,'error':error,'tool_calls':len(completed),
        'command_calls':len([e for e in completed if e['tool'] in {'shell','workspace_run'}]),
        'approval_count':len(approvals),'approvals':approvals,'events':events,'answer':answer,
        'postcheck_warnings':len([e for e in completed if e['status']=='warning']),
        'results':[{'tool':m.get('name'), 'content':m.get('content')} for m in history if m.get('role')=='tool']}
    (output/case['id']/'result.json').write_text(json.dumps(row,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:row[k] for k in ('id','passed','failures','elapsed_seconds','tool_calls','command_calls','approval_count','error')},ensure_ascii=False),flush=True)
    return row


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--ids',nargs='*');parser.add_argument('--resume',action='store_true');parser.add_argument('--development',action='store_true');parser.add_argument('--clarified',action='store_true');parser.add_argument('--edges',action='store_true');parser.add_argument('--repeat',type=int,default=1)
    args=parser.parse_args(); output=Path(args.output).resolve();output.mkdir(parents=True,exist_ok=True)
    development=[{'id':'dev-python','question':'用 Python 写一个输出 Hello Python 的 main.py，并实际运行。','required_output':'Hello Python','source_file':'main.py'},
        {'id':'dev-go','question':'用 Go 写一个输出 Hello Go 的 main.go，并实际编译运行。','required_output':'Hello Go','source_file':'main.go'},
        {'id':'dev-java','question':'用 Java 写一个输出 Hello Java 的 Hello.java，并实际编译运行。','required_output':'Hello Java','source_file':'Hello.java'},
        {'id':'dev-repair','question':'运行现有 main.py，根据实际报错修复语法错误，再次运行，输出应为 repair-ok。','required_output':'repair-ok','source_file':'main.py','broken':True}]
    if args.repeat<1 or args.repeat>5: parser.error('repeat must be between 1 and 5')
    selected=[c for c in (edge_cases() if args.edges else development if args.development else cases()) if not args.ids or c['id'] in args.ids]
    if not selected: parser.error('No matching case IDs')
    if args.edges and args.development: parser.error('Choose either edges or development')
    if args.repeat>1:
        selected=[{**c,'id':c['id']+f'-r{r}','original_id':c['id'],'repeat':r} for c in selected for r in range(1,args.repeat+1)]
    if args.clarified:
        for case in selected:
            case['clarified']=True
            if case.get('original_id',case['id'])=='task-25':
                case['question']='读取 a.txt，为每一行（包括空行）末尾直接追加 | COUNTRY，不额外插入空格。原空行在答案中变为 | COUNTRY。'
    manifest={'source':'https://github.com/TellinaTool/nl2bash','commit':'d6b9f5bdff45621d190134e31ab63b7bf7002190',
        'data_file':'data/bash/all.nl','license':'MIT (dataset only)', 'official_score':False,
        'model':load_model_settings().default_model,'prompt_protocol':2 if args.clarified or args.edges else 1, 'cases':selected}
    if args.edges: manifest.update(source='DianStar engineering edge cases',commit=None,data_file=None,license=None)
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    rows=[]
    for case in selected:
        saved=output/case['id']/'result.json'
        if args.resume and saved.exists():
            row=json.loads(saved.read_text(encoding='utf-8'))
            errors=judge(saved.parent/case.get('project_name','project'),case) if not args.development else row['failures']
            errors+=sequence_errors(case,row['results'])
            if args.development:
                errors=[e for e in errors if e!='no_successful_command']
            if not any(e['tool'] in {'shell','workspace_run'} and e['status'] in {'success','warning'} for e in row['events'] if e['phase']=='completed'):
                errors.append('no_successful_command')
            if row['error']: errors.append('runtime_error')
            if errors!=row['failures']: row['initial_grader_failures']=row['failures']
            row.update(passed=not errors,failures=errors)
            row['postcheck_warnings']=len([e for e in row['events'] if e['phase']=='completed' and e['status']=='warning'])
            saved.write_text(json.dumps(row,ensure_ascii=False,indent=2),encoding='utf-8')
            rows.append(row)
        else:
            rows.append(await evaluate(case,output,development=args.development))
        (output/'summary.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__': asyncio.run(main())
