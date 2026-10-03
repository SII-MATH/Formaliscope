import argparse
import json
from pathlib import Path

from .engine import config, import_result, prepare, run

parser = argparse.ArgumentParser(description='Statement 审阅 Agent Workflow')
parser.add_argument('--config', type=Path)
commands = parser.add_subparsers(dest='command', required=True)
build = commands.add_parser('prepare', help='准备版本绑定的独立任务')
build.add_argument('--snapshot', type=Path, required=True)
build.add_argument('--output', type=Path, required=True)
build.add_argument('--declaration', action='append', default=[])
build.add_argument('--references', type=Path)
execute = commands.add_parser('run', help='调用 Agent 或导出独立任务')
execute.add_argument('--output', type=Path, required=True)
execute.add_argument('--limit', type=int, default=1)
execute.add_argument('--export-only', action='store_true')
ingest = commands.add_parser('import-result', help='校验并导入外部 Agent 的结果')
ingest.add_argument('--output', type=Path, required=True)
ingest.add_argument('--stage', choices=['readback', 'audit'], required=True)
ingest.add_argument('--result', type=Path, required=True)
args = parser.parse_args()
try:
    cfg = config(args.config)
    if args.command == 'prepare':
        result = prepare(args.snapshot, args.output, cfg, args.declaration, args.references)
        print(json.dumps({'prepared': len(result['tasks']), 'source_commit': result['source_commit']}, ensure_ascii=False))
    elif args.command == 'import-result':
        print(json.dumps(import_result(args.output, cfg, args.stage, args.result), ensure_ascii=False))
    else:
        print(json.dumps(run(args.output, cfg, limit=args.limit, export_only=args.export_only), ensure_ascii=False))
except (ValueError, OSError) as error:
    parser.exit(1, f'{error}\n')
