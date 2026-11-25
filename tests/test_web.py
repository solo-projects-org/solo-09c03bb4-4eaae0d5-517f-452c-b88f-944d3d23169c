"""网页端到端测试：通过 HTTP 上传两期持仓与公告、查看净变动与失败提示。"""

import io

import pytest

from app import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "web.db"),
            "UPLOAD_FOLDER": str(tmp_path / "uploads"),
        }
    )
    return app.test_client()


BEGIN = "证券代码,股数\n000001,100\n000002,200\n".encode("utf-8")
END = "证券代码,股数\n000001X,300\n000002,190\n".encode("utf-8")
ANN = "生效日,旧代码,新代码,新旧股数比\n2026-01-15,000001,000001X,3\n".encode("utf-8")


def _upload(client, path, date, data):
    return client.post(
        path,
        data={
            "as_of_date": date,
            "file": (io.BytesIO(data), "x.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def test_index_empty(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "净变动".encode() in resp.data


def test_upload_holdings_and_announcements_renders_net_change(client):
    resp = _upload(client, "/holdings/begin", "2026-01-01", BEGIN)
    assert resp.status_code == 200
    resp = client.post(
        "/announcements",
        data={"file": (io.BytesIO(ANN), "ann.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "整表换报成功".encode() in resp.data
    resp = _upload(client, "/holdings/end", "2026-01-31", END)
    text = resp.data.decode()
    # 000001X：折算 300、实报 300、净变动 0；000002：200 vs 190，净变动 -10
    assert "000001X" in text
    assert "000002" in text
    assert "-10" in text


def test_bad_announcement_flashes_and_keeps_old(client):
    _upload(client, "/holdings/begin", "2026-01-01", BEGIN)
    client.post(
        "/announcements",
        data={"file": (io.BytesIO(ANN), "ann.csv")},
        content_type="multipart/form-data",
    )
    bad = (
        "生效日,旧代码,新代码,新旧股数比\n"
        "2026-01-15,000001,000001X,3\n"
        "2026-01-15,000001,000001Y,2\n"
    ).encode("utf-8")
    resp = client.post(
        "/announcements",
        data={"file": (io.BytesIO(bad), "bad.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    text = resp.data.decode()
    assert "公告换报失败" in text
    assert "failed" in text
    # 原结果仍在页面上
    assert "000001X" in text


def test_missing_file_returns_flash_not_500(client):
    resp = client.post(
        "/announcements",
        data={},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "公告换报失败" in resp.data.decode()


# ------------------------------------------------------------ 公告回查

BEGIN2 = (
    "证券代码,股数\n000001,100\n000002,200\n"
).encode("utf-8")
ANN2 = (
    "生效日,旧代码,新代码,新旧股数比\n"
    "2026-01-15,000001,000001B,3\n"
    "2026-01-20,000001B,000001C,2\n"
).encode("utf-8")


def _seed_lookup(client):
    _upload(client, "/holdings/begin", "2026-01-01", BEGIN2)
    client.post(
        "/announcements",
        data={"file": (io.BytesIO(ANN2), "ann.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    # 最新结果批次
    from app.db import get_db

    with client.application.app_context():
        rid = get_db().execute(
            "SELECT MAX(id) AS id FROM result_batch"
        ).fetchone()["id"]
    return rid


def test_lookup_page_renders_form_and_empty_state(client):
    rid = _seed_lookup(client)
    # 无查询参数：展示回查卡片与“未准备”状态，不报错
    resp = client.get(f"/results/{rid}")
    text = resp.data.decode()
    assert "公告回查" in text
    assert "未准备" in text
    # 查无匹配显示空结果
    resp = client.get(
        f"/results/{rid}?effective_date=2026-01-15&old_code=999999"
    )
    text = resp.data.decode()
    assert "查无匹配" in text


def test_lookup_first_trace_auto_prepares_and_shows_path_and_summary(client):
    rid = _seed_lookup(client)
    resp = client.get(
        f"/results/{rid}?effective_date=2026-01-15&old_code=000001"
    )
    text = resp.data.decode()
    # 首次自动准备，走索引
    assert "持久化索引查询" in text
    assert "已为该结果批次准备回查数据" in text
    # 命中来源与完整路径
    assert "000001" in text and "000001B" in text and "000001C" in text
    assert "2026-01-15" in text and "2026-01-20" in text
    # 该来源折算股数 600（100*3*2）与汇总
    assert "600" in text
    assert "相关期末证券汇总" in text

    # 第二次回查不再提示自动准备
    resp2 = client.get(
        f"/results/{rid}?effective_date=2026-01-20&old_code=000001B"
    )
    text2 = resp2.data.decode()
    assert "已为该结果批次准备回查数据" not in text2
    assert "持久化索引查询" in text2


def test_manual_prepare_endpoint_marks_ready_and_survives_render(client):
    rid = _seed_lookup(client)
    resp = client.post(f"/results/{rid}/prepare-lookup", follow_redirects=True)
    assert resp.status_code == 200
    assert "回查数据准备完成" in resp.data.decode()
    resp = client.get(f"/results/{rid}")
    assert "已就绪" in resp.data.decode()


def test_lookup_invalid_args_flashes_not_500(client):
    rid = _seed_lookup(client)
    resp = client.get(
        f"/results/{rid}?effective_date=bad-date&old_code=000001"
    )
    assert resp.status_code == 200
    assert "回查条件有误" in resp.data.decode()


def test_lookup_result_batch_isolation_across_uploads(client):
    """另一窗口再上传/换报后，原页面 URL 仍绑定原结果批次取数。"""
    rid_old = _seed_lookup(client)
    # 换报产生新结果批次
    ann_v2 = (
        "生效日,旧代码,新代码,新旧股数比\n"
        "2026-01-15,000001,000001B,4\n"
        "2026-01-20,000001B,000001C,2\n"
    ).encode("utf-8")
    client.post(
        "/announcements",
        data={"file": (io.BytesIO(ann_v2), "ann_v2.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    # 打开旧批次页面：仍按旧比例 3（600 股），不是新比例 4（800 股）
    resp = client.get(
        f"/results/{rid_old}?effective_date=2026-01-15&old_code=000001"
    )
    text = resp.data.decode()
    assert f"结果批次 #{rid_old}" in text
    assert "600" in text
    assert "800" not in text


def test_lookup_unknown_result_returns_404(client):
    _seed_lookup(client)
    assert client.get("/results/999999").status_code == 404
    assert client.post("/results/999999/prepare-lookup").status_code == 404
