import json
import math
from pathlib import Path
import re
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from hashlib import sha256
from itertools import chain
from tempfile import NamedTemporaryFile


UNEXECUTED = "未执行"
SUCCESS = "成功"
FAILED = "执行失败"
REVIEW = "待人工核对"
INVALID_VALUE = "<无效值>"


def validate_run_id(run_id: str) -> None:
    # Leave room for the .jsonl suffix in a 255-byte filename.
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,249}", run_id):
        raise ValueError("运行号须由 1 至 249 个字母、数字、连字符或下划线组成")


def safe_tool(name: str) -> str:
    if isinstance(name, str) and re.fullmatch(r"[a-z_][a-z_0-9]{0,63}|无法识别", name):
        return name
    return "<无效工具名>"


def safe_address(value: str) -> str:
    street = re.match(r"[\u4e00-\u9fff]{1,32}?(?:路|街|道|巷|弄)", value)
    prefix = street.group() if street else ""
    if re.search(r"[〇零一二三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾佰仟两]", prefix):
        prefix = ""
    return prefix + "***"


def safe_account(value: str) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Z]", value):
        return value
    return None


def stored_params(params: dict, fields: dict) -> dict | None:
    # Preserve shape and type errors without persisting unexpected names or raw values.
    if not isinstance(params, dict):
        return None
    result = {}
    for key in sorted(fields.keys() & params.keys()):
        value = params[key]
        if key == "amount":
            numeric = type(value) is int or (type(value) is float and math.isfinite(value))
            result[key] = value if numeric else None
        elif type(value) is not str or not value.strip():
            result[key] = None
        elif key == "new_address":
            result[key] = safe_address(value)
        elif key == "account":
            result[key] = safe_account(value) or INVALID_VALUE
        else:
            result[key] = value if re.fullmatch(r"[A-Z]-[0-9]{3}", value) else INVALID_VALUE
    if params.keys() - fields.keys():
        result["<无效参数>"] = None
    return result


def safe_params(params: dict, fields: dict) -> dict:
    sanitized = stored_params(params, fields) or {}
    return {key: value if value is not None else INVALID_VALUE
            for key, value in sanitized.items() if key in fields}


def initialize(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY, run_id TEXT, step INTEGER, payload TEXT
        )
    """)


def record(db: sqlite3.Connection, event: dict) -> None:
    db.execute(
        "INSERT INTO events (run_id, step, payload) VALUES (?, ?, ?)",
        (event["run_id"], event["step"], json.dumps(event, ensure_ascii=False, sort_keys=True)),
    )


def latest(db: sqlite3.Connection, run_id: str) -> dict[int, dict]:
    rows = db.execute("SELECT payload FROM events WHERE run_id = ? ORDER BY seq", (run_id,))
    events = (json.loads(row[0]) for row in rows)
    return {event["step"]: event for event in events}


def _event_payloads(db: sqlite3.Connection, run_id: str) -> list[str]:
    return [row[0] for row in db.execute(
        "SELECT payload FROM events WHERE run_id = ? ORDER BY seq", (run_id,),
    )]


def _trace_path(traces: Path, run_id: str) -> Path:
    if run_id == run_id.lower():
        return traces / f"{run_id}.jsonl"
    digest = sha256(run_id.encode("ascii")).hexdigest()
    return traces / "case-sensitive" / f"{digest}.jsonl"


def _write_trace(path: Path, lines: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.writelines(lines)
            handle.flush()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _preserve_legacy_trace(path: Path, run_id: str) -> None:
    try:
        existing = path.open(encoding="utf-8")
    except FileNotFoundError:
        return
    with existing:
        try:
            first_line = existing.readline()
            event = json.loads(first_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        legacy_id = event.get("run_id") if isinstance(event, dict) else None
        if (isinstance(legacy_id, str) and legacy_id.isascii()
                and legacy_id != run_id and legacy_id.lower() == run_id):
            legacy_path = _trace_path(path.parent, legacy_id)
            if not legacy_path.exists():
                _write_trace(legacy_path, chain((first_line,), existing))


def publish(directory: Path, db: sqlite3.Connection, run_id: str) -> Path:
    validate_run_id(run_id)
    # Serialize projection with writers so a concurrent process cannot overwrite a newer trace.
    with db:
        db.execute("BEGIN IMMEDIATE")
        payloads = _event_payloads(db, run_id)
        if not payloads:
            raise ValueError("运行号不存在")
        path = _trace_path(directory / "traces", run_id)
        if run_id == run_id.lower():
            _preserve_legacy_trace(path, run_id)
        _write_trace(path, (payload + "\n" for payload in payloads))
    return path


def replay(directory: Path, run_id: str) -> dict:
    database = directory / "state.sqlite3"
    if not database.is_file():
        raise ValueError("运行号不存在")
    # Recover the JSON Lines projection from committed audit events after an interrupted write.
    try:
        with closing(sqlite3.connect(database, timeout=0)) as db:
            path = publish(directory, db, run_id)
    except (OSError, sqlite3.OperationalError) as error:
        if isinstance(error, sqlite3.OperationalError) and str(error) != "database is locked":
            raise
        with closing(sqlite3.connect(database)) as db:
            payloads = _event_payloads(db, run_id)
        if not payloads:
            raise ValueError("运行号不存在")
        events = [json.loads(payload) for payload in payloads]
        return {**snapshot(run_id, events), "trace_warning": "审计文件写入失败"}
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return snapshot(run_id, events)


def snapshot(run_id: str, events: list[dict]) -> dict:
    steps = {event["step"]: event for event in events}
    statuses = {event["status"] for event in steps.values()}
    priority = (REVIEW, "待审", FAILED, "已过期", "已拒绝", UNEXECUTED, SUCCESS)
    status = next((value for value in priority if value in statuses), UNEXECUTED)
    return {"run_id": run_id, "status": status, "steps": list(steps.values()), "events": events}
