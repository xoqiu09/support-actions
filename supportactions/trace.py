import json
import math
from pathlib import Path
import re
import sqlite3
from contextlib import closing
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


def latest(db: sqlite3.Connection, run_id: str) -> dict[int, str]:
    rows = db.execute("SELECT payload FROM events WHERE run_id = ? ORDER BY seq", (run_id,))
    events = (json.loads(row[0]) for row in rows)
    return {event["step"]: event["status"] for event in events}


def publish(directory: Path, db: sqlite3.Connection, run_id: str) -> Path:
    validate_run_id(run_id)
    # Serialize projection with writers so a concurrent process cannot overwrite a newer trace.
    with db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            "SELECT payload FROM events WHERE run_id = ? ORDER BY seq", (run_id,),
        ).fetchall()
        if not rows:
            raise ValueError("运行号不存在")
        path = directory / "traces" / f"{run_id}.jsonl"
        path.parent.mkdir(exist_ok=True)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.writelines(row[0] + "\n" for row in rows)
                handle.flush()
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
    return path


def replay(directory: Path, run_id: str) -> dict:
    database = directory / "state.sqlite3"
    if not database.is_file():
        raise ValueError("运行号不存在")
    # Recover the JSON Lines projection from committed audit events after an interrupted write.
    with closing(sqlite3.connect(database)) as db:
        path = publish(directory, db, run_id)
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return snapshot(run_id, events)


def snapshot(run_id: str, events: list[dict]) -> dict:
    steps = {event["step"]: event for event in events}
    statuses = {event["status"] for event in steps.values()}
    priority = (REVIEW, "待审", FAILED, "已过期", "已拒绝", UNEXECUTED, SUCCESS)
    status = next((value for value in priority if value in statuses), UNEXECUTED)
    return {"run_id": run_id, "status": status, "steps": list(steps.values()), "events": events}
