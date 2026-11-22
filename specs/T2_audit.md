# T2 实现：哈希链审计日志 aipay_kit/audit.py

## 目标
追加式（append-only）审计日志，每条记录带前一条的哈希，可导出并独立校验，用于「交易过程留痕 / 可信存证」演示。存储用标准库 sqlite3（文件路径或 ":memory:"）。

## 新建文件
- `aipay_kit/audit.py`
- `tests/test_audit_smoke.py`（3–5 条冒烟）

## 接口（必须一致）
```python
GENESIS = "0" * 64

@dataclass(frozen=True)
class AuditRecord:
    seq: int          # 从 1 开始连续
    ts: int           # unix 毫秒（由调用方 now_ms 传入或 time.time_ns()//1_000_000）
    actor: str        # 如 "agent:kid123" / "user:u1" / "gateway" / "merchant"
    event: str        # 如 "mandate.created" / "order.precreated" / "notify.verified" / "fulfilled" / "blocked"
    ref: str          # 关联 id（out_trade_no / mandate_id），可空字符串
    data: dict        # JSON 可序列化
    prev_hash: str
    hash: str

def canonical_json(obj) -> str:  # json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
def record_hash(seq, ts, actor, event, ref, data, prev_hash) -> str:
    # sha256 hex of canonical_json({"seq":..,"ts":..,"actor":..,"event":..,"ref":..,"data":..,"prev_hash":..})

class AuditLog:
    def __init__(self, path: str = ":memory:") -> None: ...
    def append(self, actor: str, event: str, ref: str = "", data: dict | None = None, *, now_ms: int | None = None) -> AuditRecord: ...
    def records(self, ref: str | None = None) -> list[AuditRecord]: ...   # 按 seq 升序；ref 过滤
    def export_jsonl(self, ref: str | None = None) -> str: ...            # 每行一个 canonical_json(asdict(record))，末尾换行
    def export_csv(self, ref: str | None = None) -> str: ...              # 表头 seq,ts,actor,event,ref,data,prev_hash,hash；data 列为 canonical_json
    def close(self) -> None: ...

@dataclass(frozen=True)
class ChainCheck:
    ok: bool
    count: int
    first_bad_seq: int | None
    reason: str          # "" 或 "hash_mismatch" / "prev_mismatch" / "seq_gap" / "parse_error"

def verify_jsonl(text: str) -> ChainCheck: ...
```
- verify_jsonl 只校验**完整导出**（ref=None）：seq 从 1 连续、第一条 prev_hash == GENESIS、每条 prev_hash == 上一条 hash、hash 重算一致。空文本 → ok=True, count=0。
- append 在一个 sqlite 事务里读最后一条再写，线程安全（用 threading.Lock）。
- data 默认 {}。不允许 update/delete 方法。

## 验收
- `.venv/bin/python -m pytest -q` 全部通过（含已有测试）。
