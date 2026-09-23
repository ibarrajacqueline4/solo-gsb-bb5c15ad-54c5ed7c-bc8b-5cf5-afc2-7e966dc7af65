"""GET /events/{event_id}/snapshot?at=... 历史快照测试。

SQLite 文件库 + 模拟时钟（与 test_extend.py 相同手法）。除猴子补丁
app.main.now_utc 外，还把各模型 created_at 列默认值的可调用对象替换为
同一时钟，使承诺/揭示/延长记录的写入时间完全确定，便于验证快照的
「该时刻前已写入」边界（含该时刻）。覆盖：

  - 创建时刻的快照：原始截止时间、commit 状态、计数为 0
  - 承诺/揭示计数只统计 at 之前（含 at）已写入的记录，边界精确到同一时刻
  - 按发生时间回放截止时间延长：延长前/延长瞬间/连续两次延长之间/之后
  - 基于回放后截止时间的 commit / reveal / closed 状态
  - at 缺失、格式非法、无时区、只有日期均 400 INVALID_SNAPSHOT_TIME
  - at 早于创建时间 400 SNAPSHOT_BEFORE_CREATION；恰为创建时间 200
  - 事件不存在 404、ID 非 UUID 422；非法 at 优先于 404 判定
  - 非 UTC 时区偏移（+08:00、Z）按同一时刻解析；快照只读
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

_tmpdir = tempfile.mkdtemp(prefix="sealed_predictions_snapshot_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/snapshot.db"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as main_module  # noqa: E402
from app import models  # noqa: E402
from app.hashing import compute_commitment  # noqa: E402

T0 = datetime(2030, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

COMMIT_DL = T0 + timedelta(hours=1)
REVEAL_DL = T0 + timedelta(hours=2)


class MutableClock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self, *args, **kwargs) -> datetime:
        # SQLAlchemy 的 ColumnDefault 会以执行上下文为参数调用默认值函数
        return self.t


clock = MutableClock(T0)

client = TestClient(main_module.app)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    clock.t = T0
    monkeypatch.setattr(main_module, "now_utc", clock)
    # 让承诺/揭示/延长记录的写入时间也由同一时钟决定：列默认值在类定义时
    # 持有 models.utcnow 函数对象。SQLAlchemy 首次 INSERT 后会在 Column 上
    # 备忘 _default_description_tuple（直接捕获旧函数），因此除替换
    # ColumnDefault.arg 外，还须清除该备忘属性，补丁才对后续写入生效。
    timestamp_columns = [
        models.Event.__table__.c.created_at,
        models.Commitment.__table__.c.created_at,
        models.Reveal.__table__.c.created_at,
        models.DeadlineExtension.__table__.c.created_at,
    ]
    for column in timestamp_columns:
        monkeypatch.setattr(column.default, "arg", clock)
        column.__dict__.pop("_default_description_tuple", None)
    with client:  # 触发 lifespan 建表（幂等）
        yield
    for column in timestamp_columns:
        column.__dict__.pop("_default_description_tuple", None)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def dt(s: str) -> datetime:
    """解析响应中的时间字符串（兼容 Z 与 +00:00），按时刻比较。"""
    return datetime.fromisoformat(s)


def create_event(commit_dl: datetime = COMMIT_DL, reveal_dl: datetime = REVEAL_DL) -> str:
    r = client.post("/events", json={
        "title": "快照测试事件",
        "description": "snapshot demo",
        "commit_deadline": iso(commit_dl),
        "reveal_deadline": iso(reveal_dl),
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def snapshot(event_id: str, at: str | None):
    return client.get(f"/events/{event_id}/snapshot", params={"at": at} if at is not None else {})


def submit_commitment(event_id: str, text: str, salt: str, at: datetime) -> str:
    clock.t = at
    h = compute_commitment(text, salt)
    r = client.post(f"/events/{event_id}/commitments", json={"commitment_hash": h})
    assert r.status_code == 201, r.text
    return h


def extend(event_id: str, new_commit: datetime, new_reveal: datetime,
           reason: str, at: datetime) -> None:
    clock.t = at
    r = client.post(f"/events/{event_id}/extend", json={
        "commit_deadline": iso(new_commit),
        "reveal_deadline": iso(new_reveal),
        "reason": reason,
    })
    assert r.status_code == 200, r.text


def submit_reveal(event_id: str, text: str, salt: str, at: datetime) -> None:
    clock.t = at
    r = client.post(f"/events/{event_id}/reveals",
                    json={"original_text": text, "salt": salt})
    assert r.status_code == 201, r.text


# --------------------------------------------------------------------------- #
# 基础视图
# --------------------------------------------------------------------------- #

def test_snapshot_at_creation_shows_original_view():
    event_id = create_event()
    r = snapshot(event_id, iso(T0))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event_id"] == event_id
    assert dt(body["snapshot_at"]) == T0
    assert body["title"] == "快照测试事件"
    assert body["description"] == "snapshot demo"
    assert dt(body["created_at"]) == T0
    assert dt(body["commit_deadline"]) == COMMIT_DL
    assert dt(body["reveal_deadline"]) == REVEAL_DL
    assert body["status"] == "commit"
    assert body["commitment_count"] == 0
    assert body["reveal_count"] == 0


def test_snapshot_counts_only_records_written_before_at():
    event_id = create_event()
    h1 = submit_commitment(event_id, "预测一", "salt-1", T0 + timedelta(minutes=10))
    submit_commitment(event_id, "预测二", "salt-2", T0 + timedelta(minutes=20))
    assert h1 != compute_commitment("预测二", "salt-2")

    # 第一笔写入前：0
    body = snapshot(event_id, iso(T0 + timedelta(minutes=9, seconds=59))).json()
    assert body["commitment_count"] == 0

    # 恰在第一笔写入时刻（含该时刻）：计入 1
    body = snapshot(event_id, iso(T0 + timedelta(minutes=10))).json()
    assert body["commitment_count"] == 1

    # 两笔之间：仍为 1
    body = snapshot(event_id, iso(T0 + timedelta(minutes=15))).json()
    assert body["commitment_count"] == 1

    # 恰在第二笔写入时刻：计入 2
    body = snapshot(event_id, iso(T0 + timedelta(minutes=20))).json()
    assert body["commitment_count"] == 2

    # 更晚的快照不遗漏，但也不会凭空增加
    body = snapshot(event_id, iso(T0 + timedelta(minutes=50))).json()
    assert body["commitment_count"] == 2


def test_snapshot_is_read_only_and_deterministic():
    event_id = create_event()
    submit_commitment(event_id, "预测一", "salt-1", T0 + timedelta(minutes=10))

    r1 = snapshot(event_id, iso(T0 + timedelta(minutes=30)))
    r2 = snapshot(event_id, iso(T0 + timedelta(minutes=30)))
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()

    # 事件当前视图计数不受快照请求影响
    r = client.get(f"/events/{event_id}")
    assert r.json()["commitment_count"] == 1
    assert r.json()["reveal_count"] == 0


# --------------------------------------------------------------------------- #
# 截止时间延长回放
# --------------------------------------------------------------------------- #

def test_snapshot_replays_single_extension():
    event_id = create_event()
    new_commit = T0 + timedelta(hours=2)
    new_reveal = T0 + timedelta(hours=3)
    extend(event_id, new_commit, new_reveal, "顺延窗口", T0 + timedelta(minutes=10))

    # 延长尚未发生：快照看到创建时的原始截止时间（尽管当前事件已被延长）
    r = client.get(f"/events/{event_id}")
    assert dt(r.json()["commit_deadline"]) == new_commit  # 当前视图确实已延长
    body = snapshot(event_id, iso(T0 + timedelta(minutes=5))).json()
    assert dt(body["commit_deadline"]) == COMMIT_DL
    assert dt(body["reveal_deadline"]) == REVEAL_DL
    assert body["status"] == "commit"

    # 恰在延长生效时刻：延长已套用（at 含该时刻）
    body = snapshot(event_id, iso(T0 + timedelta(minutes=10))).json()
    assert dt(body["commit_deadline"]) == new_commit
    assert dt(body["reveal_deadline"]) == new_reveal

    # 原 commit 截止时刻：按回放后的新截止时间仍在 commit 阶段
    body = snapshot(event_id, iso(COMMIT_DL)).json()
    assert dt(body["commit_deadline"]) == new_commit
    assert body["status"] == "commit"

    # 新截止时间上的阶段切换
    body = snapshot(event_id, iso(new_commit)).json()
    assert body["status"] == "reveal"
    body = snapshot(event_id, iso(new_reveal)).json()
    assert body["status"] == "closed"


def test_snapshot_replays_sequential_extensions():
    event_id = create_event()
    ext1_commit, ext1_reveal = T0 + timedelta(hours=2), T0 + timedelta(hours=3)
    ext2_commit, ext2_reveal = T0 + timedelta(hours=3), T0 + timedelta(hours=4)
    extend(event_id, ext1_commit, ext1_reveal, "第一次", T0 + timedelta(minutes=10))
    extend(event_id, ext2_commit, ext2_reveal, "第二次", T0 + timedelta(minutes=20))

    # 第一次延长之前：原始截止时间
    body = snapshot(event_id, iso(T0 + timedelta(minutes=5))).json()
    assert dt(body["commit_deadline"]) == COMMIT_DL
    assert dt(body["reveal_deadline"]) == REVEAL_DL

    # 两次延长之间：只套用第一次
    body = snapshot(event_id, iso(T0 + timedelta(minutes=15))).json()
    assert dt(body["commit_deadline"]) == ext1_commit
    assert dt(body["reveal_deadline"]) == ext1_reveal

    # 恰在第二次延长时刻：两次都已套用
    body = snapshot(event_id, iso(T0 + timedelta(minutes=20))).json()
    assert dt(body["commit_deadline"]) == ext2_commit
    assert dt(body["reveal_deadline"]) == ext2_reveal

    # 更晚快照保持第二次延长后的值；T0+3h30m 位于第二次延长的 reveal 窗口内
    body = snapshot(event_id, iso(T0 + timedelta(hours=3, minutes=30))).json()
    assert dt(body["commit_deadline"]) == ext2_commit
    assert body["status"] == "reveal"


# --------------------------------------------------------------------------- #
# 阶段状态与揭示计数
# --------------------------------------------------------------------------- #

def test_snapshot_status_and_reveal_count_boundaries_without_extension():
    event_id = create_event()
    text, salt = "明天下雨", "s3cr3t"
    submit_commitment(event_id, text, salt, T0 + timedelta(minutes=30))
    # commit 截止时刻揭示通道开启，写入一笔揭示
    submit_reveal(event_id, text, salt, COMMIT_DL)

    # 揭示写入前一刻：commit 阶段，无揭示
    body = snapshot(event_id, iso(COMMIT_DL - timedelta(seconds=1))).json()
    assert body["status"] == "commit"
    assert body["commitment_count"] == 1
    assert body["reveal_count"] == 0

    # 恰在揭示写入时刻（== commit_deadline）：reveal 阶段，揭示计入
    body = snapshot(event_id, iso(COMMIT_DL)).json()
    assert body["status"] == "reveal"
    assert body["commitment_count"] == 1
    assert body["reveal_count"] == 1

    # reveal 阶段中段保持
    body = snapshot(event_id, iso(REVEAL_DL - timedelta(seconds=1))).json()
    assert body["status"] == "reveal"
    assert body["reveal_count"] == 1

    # 到达 reveal 截止时刻：closed
    body = snapshot(event_id, iso(REVEAL_DL)).json()
    assert body["status"] == "closed"
    assert body["commitment_count"] == 1
    assert body["reveal_count"] == 1


def test_snapshot_future_at_returns_current_effective_state():
    event_id = create_event()
    text, salt = "未来预测", "salt-x"
    submit_commitment(event_id, text, salt, T0 + timedelta(minutes=5))
    extend(event_id, T0 + timedelta(hours=2), T0 + timedelta(hours=3),
           "延长", T0 + timedelta(minutes=10))
    submit_reveal(event_id, text, salt, T0 + timedelta(hours=2))

    # at 允许是未来时刻：回放全部已生效记录
    body = snapshot(event_id, iso(T0 + timedelta(days=365))).json()
    assert dt(body["commit_deadline"]) == T0 + timedelta(hours=2)
    assert dt(body["reveal_deadline"]) == T0 + timedelta(hours=3)
    assert body["status"] == "closed"
    assert body["commitment_count"] == 1
    assert body["reveal_count"] == 1


# --------------------------------------------------------------------------- #
# 时区与参数解析
# --------------------------------------------------------------------------- #

def test_snapshot_accepts_z_and_non_utc_offsets():
    event_id = create_event()

    # Z 后缀
    r = snapshot(event_id, "2030-06-01T00:30:00Z")
    assert r.status_code == 200, r.text
    assert dt(r.json()["snapshot_at"]) == T0 + timedelta(minutes=30)

    # +08:00 表示的同一时刻，按 UTC 归一化回显
    r = snapshot(event_id, "2030-06-01T08:30:00+08:00")
    assert r.status_code == 200, r.text
    assert dt(r.json()["snapshot_at"]) == T0 + timedelta(minutes=30)

    # 负偏移
    r = snapshot(event_id, "2030-05-31T19:30:00-05:00")
    assert r.status_code == 200, r.text
    assert dt(r.json()["snapshot_at"]) == T0 + timedelta(minutes=30)


@pytest.mark.parametrize("bad_at", [
    None,                    # 缺失
    "",                      # 空串
    "not-a-time",            # 完全无法解析
    "2030-06-01T00:30:00",   # 无时区偏移
    "2030-06-01",            # 仅日期
    "2030-13-01T00:00:00Z",  # 非法日期
])
def test_snapshot_invalid_at_returns_400(bad_at):
    event_id = create_event()
    r = snapshot(event_id, bad_at)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "INVALID_SNAPSHOT_TIME"


def test_snapshot_before_creation_returns_400():
    event_id = create_event()

    r = snapshot(event_id, iso(T0 - timedelta(seconds=1)))
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "SNAPSHOT_BEFORE_CREATION"

    r = snapshot(event_id, iso(T0 - timedelta(days=10)))
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "SNAPSHOT_BEFORE_CREATION"

    # 恰为创建时刻：合法
    r = snapshot(event_id, iso(T0))
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# 404 / 422
# --------------------------------------------------------------------------- #

def test_snapshot_unknown_event_returns_404():
    missing = "00000000-0000-0000-0000-000000000000"
    r = snapshot(missing, iso(T0 + timedelta(minutes=1)))
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "EVENT_NOT_FOUND"


def test_snapshot_non_uuid_returns_422():
    r = client.get("/events/not-a-uuid/snapshot", params={"at": iso(T0)})
    assert r.status_code == 422


def test_snapshot_invalid_at_takes_precedence_over_unknown_event():
    # 参数先于事件存在性解析：即使事件不存在，非法 at 仍返回 400
    missing = "00000000-0000-0000-0000-000000000000"
    r = snapshot(missing, "2030-06-01T00:30:00")  # 无时区
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_SNAPSHOT_TIME"

    r = client.get(f"/events/{missing}/snapshot")  # 缺少 at
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_SNAPSHOT_TIME"
