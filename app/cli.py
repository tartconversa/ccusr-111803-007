"""读取本地演练来源，并查询可追溯的交接采集记录。"""
import json
import sys
from pathlib import Path
from .capture import collect
from . import records

DEFAULT_DB=Path('records.sqlite3')

def _print(value):
    print(json.dumps(value,ensure_ascii=False,indent=2))

def main(argv=None):
    argv=argv if argv is not None else sys.argv[1:]
    command=argv[0] if argv else 'collect'
    origin='http://127.0.0.1:8765'
    db=Path(argv[1]) if len(argv)>1 else DEFAULT_DB
    if command=='collect':
        edition=argv[2] if len(argv)>2 else '1'
        _print(collect(origin,db,edition))
    elif command=='overview':
        _print(records.waybill_overview(db))
    elif command=='pending':
        _print(records.list_pending(db))
    elif command=='receipts':
        _print(records.list_receipts(db))
    elif command=='progress':
        _print(records.progress_state(db))
    elif command=='backfill':
        _print(records.process_pending(db))
    elif command=='trace':
        if len(argv)<2:
            print('用法：python -m app.cli trace <handoff_key> [db_path]',file=sys.stderr);return 2
        key=argv[1];db=Path(argv[2]) if len(argv)>2 else DEFAULT_DB
        result=records.trace_handoff(db,key)
        if result is None:print(f'找不到交接 {key}',file=sys.stderr);return 1
        _print(result)
    else:
        print(f'未知命令 {command}：可用 collect|overview|pending|trace|receipts|progress|backfill',file=sys.stderr)
        return 2
    return 0

if __name__=='__main__':
    raise SystemExit(main())
