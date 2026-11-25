"""服务层测试：公告回查（按生效日 + 旧代码追溯期初来源、准备/失败回退、重启持久化、批次隔离）。"""

import pytest

from app import create_app
from app.csv_utils import CsvError
from app.db import get_db
from app.services import (
    get_lookup_prep,
    get_net_change,
    prepare_lookup,
    replace_announcements,
    trace_announcement,
    upload_holding,
)


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "lookup.db"),
            "UPLOAD_FOLDER": str(tmp_path / "uploads"),
        }
    )
    yield app


BEGIN_CSV = (
    "证券代码,股数\n"
    "000001,100\n"   # 01-15 A->B(3)，01-20 B->C(2)，最终 600 股 C
    "000002,200\n"   # 不经过任何公告
).encode("utf-8")

ANN_CSV = (
    "生效日,旧代码,新代码,新旧股数比\n"
    "2026-01-15,000001,000001B,3\n"
    "2026-01-20,000001B,000001C,2\n"
).encode("utf-8")


def _setup(app) -> int:
    with app.app_context():
        upload_holding("begin", "2026-01-01", "begin.csv", BEGIN_CSV)
        replace_announcements("ann.csv", ANN_CSV)
        result, _rows = get_net_change()
        return result["id"]


def test_first_trace_auto_prepares_and_uses_index(app):
    result_id = _setup(app)
    with app.app_context():
        # 回查前没有准备记录
        assert get_lookup_prep(result_id) is None

        out = trace_announcement(result_id, "2026-01-15", "000001")
        # 首次回查自动准备并走持久化索引
        assert out.attempted_prepare is True
        assert out.used_index is True
        assert out.prep["status"] == "ready"
        assert len(out.matches) == 1
        m = out.matches[0]
        assert m.origin_code == "000001"
        assert m.end_code == "000001C"
        assert m.adjusted_shares == 600  # 100 * 3 * 2
        # 完整路径两步都在
        assert [s["effective_date"] for s in m.steps] == [
            "2026-01-15",
            "2026-01-20",
        ]
        assert "000001 → 000001B" in m.path and "000001B → 000001C" in m.path

        # 汇总沿用整表：期末 000001C 折算 600、实报 0、净变动 -600
        assert len(out.summaries) == 1
        s = out.summaries[0]
        assert s.end_code == "000001C"
        assert (s.adjusted_shares, s.reported_shares, s.net_change) == (
            600,
            0,
            -600,
        )

        # 再次回查不再触发自动准备，直接用已就绪索引
        again = trace_announcement(result_id, "2026-01-20", "000001B")
        assert again.attempted_prepare is False
        assert again.used_index is True
        assert again.matches[0].origin_code == "000001"


def test_trace_second_hop_and_no_match_empty(app):
    result_id = _setup(app)
    with app.app_context():
        # 第二跳：旧代码是中间代码 000001B
        out = trace_announcement(result_id, "2026-01-20", "000001B")
        assert {m.origin_code for m in out.matches} == {"000001"}

        # 不相关公告：空结果，且没有对应期末汇总
        none_out = trace_announcement(result_id, "2026-01-15", "999999")
        assert none_out.matches == []
        assert none_out.summaries == []

        # 000002 没有任何路径，不会命中
        assert trace_announcement(result_id, "2026-01-15", "000002").matches == []


def test_prepare_failure_falls_back_to_scan_and_can_retry(app, monkeypatch):
    result_id = _setup(app)
    with app.app_context():
        # 人为让索引写入失败：准备应登记 failed 并抛出
        def boom(*_a, **_kw):
            raise sqlite_raise()

        monkeypatch.setattr(
            "app.services.get_db",
            lambda: _failing_conn(get_db(), boom),
        )
        with pytest.raises(Exception):
            prepare_lookup(result_id)
        monkeypatch.undo()

        # 准备状态为 failed，但原表照查仍能命中（回退扫描）
        prep = get_lookup_prep(result_id)
        assert prep["status"] == "failed"
        out = trace_announcement(result_id, "2026-01-15", "000001")
        assert out.used_index is False
        assert len(out.matches) == 1
        assert out.matches[0].end_code == "000001C"

        # 重试后恢复为索引查询
        prepare_lookup(result_id)
        assert get_lookup_prep(result_id)["status"] == "ready"
        out2 = trace_announcement(result_id, "2026-01-15", "000001")
        assert out2.used_index is True


def test_lookup_scoped_to_selected_result_batch(app):
    first_id = _setup(app)
    with app.app_context():
        # 换报公告（比例变化）产生第二个结果批次
        new_ann = (
            "生效日,旧代码,新代码,新旧股数比\n"
            "2026-01-15,000001,000001B,4\n"
            "2026-01-20,000001B,000001C,2\n"
        ).encode("utf-8")
        _msg, second_id = replace_announcements("ann_v2.csv", new_ann)
        assert second_id != first_id

        # 旧批次仍按旧比例 3 回查，新批次按新比例 4：互不串数据
        old = trace_announcement(first_id, "2026-01-15", "000001")
        assert old.matches[0].adjusted_shares == 600  # 100*3*2
        new = trace_announcement(second_id, "2026-01-15", "000001")
        assert new.matches[0].adjusted_shares == 800  # 100*4*2
        # 两个批次各自独立准备
        assert get_lookup_prep(first_id)["status"] == "ready"
        assert get_lookup_prep(second_id)["status"] == "ready"


def test_lookup_survives_restart(app):
    result_id = _setup(app)
    db_path = app.config["DATABASE"]
    with app.app_context():
        prepare_lookup(result_id)
        before = trace_announcement(result_id, "2026-01-15", "000001")

    # 模拟服务重启：用同一个数据库文件新建应用与连接
    app2 = create_app(
        {
            "TESTING": True,
            "DATABASE": db_path,
            "UPLOAD_FOLDER": app.config["UPLOAD_FOLDER"],
        }
    )
    with app2.app_context():
        prep = get_lookup_prep(result_id)
        assert prep is not None and prep["status"] == "ready"
        after = trace_announcement(result_id, "2026-01-15", "000001")
        assert after.used_index is True
        assert [m.origin_code for m in after.matches] == [
            m.origin_code for m in before.matches
        ]
        assert after.matches[0].adjusted_shares == before.matches[0].adjusted_shares


def test_invalid_arguments_raise(app):
    result_id = _setup(app)
    with app.app_context():
        with pytest.raises(CsvError):
            trace_announcement(result_id, "2026/01/15", "000001")
        with pytest.raises(CsvError):
            trace_announcement(result_id, "2026-01-15", " ")
        with pytest.raises(CsvError):
            prepare_lookup(99999)


# ------------------------------------------------------------ 测试辅助

def sqlite_raise():
    return RuntimeError("injected lookup index failure")


class _FailingConn:
    """代理 sqlite 连接，仅让 lookup_index 的写入抛错，其余行为透传。"""

    def __init__(self, real, fail):
        self._real = real
        self._fail = fail

    def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith("INSERT INTO LOOKUP_INDEX"):
            self._fail()
        return self._real.execute(sql, params)

    def executemany(self, sql, params):
        if sql.lstrip().upper().startswith("INSERT INTO LOOKUP_INDEX"):
            self._fail()
        return self._real.executemany(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _failing_conn(real, fail):
    return _FailingConn(real, fail)
