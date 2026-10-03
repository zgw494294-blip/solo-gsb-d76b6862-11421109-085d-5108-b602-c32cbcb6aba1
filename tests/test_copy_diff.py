"""两个已记录版本间的批注改动比较：纯函数 / 存储层 / HTTP 接口测试。"""

import importlib
import sqlite3

import pytest

from app.copies import (
    CopyNotFound,
    CopyStore,
    RevisionNotFound,
    diff_annotations,
)

TEXT = "0123456789abcdefghij"  # 20 码点


def ann(id, start, end, label="L", holder=None):
    return {"id": id, "start": start, "end": end, "label": label,
            "holder": holder}


def snap(annotations, revision=1, status="open"):
    return {"revision": revision, "status": status,
            "annotations": annotations}


@pytest.fixture()
def store(tmp_path):
    return CopyStore(tmp_path / "copies.db")


# ---------- 纯函数 diff_annotations ----------


def test_diff_lists_only_changed_annotations_with_four_fields():
    old = snap([ann("a", 0, 3, "甲", None), ann("b", 4, 7, "乙", None)])
    new = snap([ann("a", 0, 3, "甲", None), ann("b", 4, 6, "乙改", "bob")])
    changes = diff_annotations(old, new)
    assert [c["id"] for c in changes] == ["b"]
    cb = changes[0]
    assert cb["changed_fields"] == ["end", "label", "holder"]
    assert cb["start"] == {"old": 4, "new": 4}  # 未变字段也附旧值/新值
    assert cb["end"] == {"old": 7, "new": 6}
    assert cb["label"] == {"old": "乙", "new": "乙改"}
    assert cb["holder"] == {"old": None, "new": "bob"}


def test_diff_each_field_independently():
    old = snap([ann("a", 0, 3, "甲", None), ann("b", 4, 7, "乙", None),
                ann("c", 8, 9, "丙", None), ann("d", 10, 11, "丁", None)])
    new = snap([ann("a", 1, 3, "甲", None),              # start
                ann("b", 4, 6, "乙", None),              # end
                ann("c", 8, 9, "丙改", None),            # label
                ann("d", 10, 11, "丁", "alice")])        # holder
    by_id = {c["id"]: c for c in diff_annotations(old, new)}
    assert by_id["a"]["changed_fields"] == ["start"]
    assert by_id["a"]["start"] == {"old": 0, "new": 1}
    assert by_id["b"]["changed_fields"] == ["end"]
    assert by_id["c"]["changed_fields"] == ["label"]
    assert by_id["d"]["changed_fields"] == ["holder"]


def test_diff_reverse_direction_swaps_old_and_new():
    old = snap([ann("a", 0, 3, "甲", None)])
    new = snap([ann("a", 0, 3, "甲", "alice")])
    forward = diff_annotations(old, new)[0]
    backward = diff_annotations(new, old)[0]
    assert forward["holder"] == {"old": None, "new": "alice"}
    assert backward["holder"] == {"old": "alice", "new": None}
    # 反向结果的 old/new 恰为正向对调
    for field in ("start", "end", "label", "holder"):
        assert backward[field] == {"old": forward[field]["new"],
                                   "new": forward[field]["old"]}


def test_diff_same_snapshot_is_empty():
    s = snap([ann("a", 0, 3, "甲", "alice"), ann("b", 4, 7, "乙", None)])
    assert diff_annotations(s, s) == []


def test_diff_pure_order_change_is_not_a_change():
    # 按完整 id 对齐、不按数组位置配对：仅保存顺序不同不算改动
    old = snap([ann("z", 15, 18, "丁"), ann("a", 0, 3, "甲"),
                ann("m", 8, 10, "丙")])
    new = snap([ann("a", 0, 3, "甲"), ann("m", 8, 10, "丙"),
                ann("z", 15, 18, "丁")])
    assert diff_annotations(old, new) == []


def test_diff_results_sorted_by_unicode_codepoint():
    old = snap([ann(id_, 0, 1, "L") for id_ in ("z", "a", "m")])
    new = snap([ann(id_, 0, 1, "X") for id_ in ("z", "a", "m")])
    # Python 字符串默认序即 Unicode 码点序
    assert [c["id"] for c in diff_annotations(old, new)] == sorted(
        ["z", "a", "m"])


def test_diff_special_ids_aligned_literally():
    # 空串、含 "/"、含换行的 id 原样参与，按完整字符串对齐不混同
    ids = ["", "a/b", "a\nb", "a", "a\n"]
    old = snap([ann(i, 0, 1, "L") for i in ids])
    new = snap([ann(i, 0, 2, "L") for i in reversed(ids)])
    by_id = {c["id"]: c for c in diff_annotations(old, new)}
    assert set(by_id) == set(ids)
    for c in by_id.values():
        assert c["end"] == {"old": 1, "new": 2}


# ---------- 存储层 diff_revisions ----------


def _build_history(store):
    cid = store.create(TEXT, [ann("a", 0, 4, "甲"), ann("b", 3, 7, "乙"),
                              ann("c", 12, 15, "丙")])["id"]
    store.claim(cid, "a", "alice")                          # rev 2
    store.claim(cid, "c", "bob")                            # rev 3
    store.modify(cid, "c", "bob", 13, 15, "丙改")           # rev 4
    store.release(cid, "b", "alice")                        # rev 5
    store.release(cid, "c", "bob")                          # rev 6
    store.finalize(cid, 6)                                  # rev 7
    return cid


def test_store_diff_forward_and_statuses(store):
    cid = _build_history(store)
    result = store.diff_revisions(cid, 1, 4)
    assert result["id"] == cid
    assert result["from_revision"] == 1 and result["to_revision"] == 4
    assert result["statuses"] == {"from": "open", "to": "open"}
    by_id = {c["id"]: c for c in result["changes"]}
    assert set(by_id) == {"a", "b", "c"}
    assert by_id["a"]["holder"] == {"old": None, "new": "alice"}
    assert by_id["a"]["changed_fields"] == ["holder"]
    assert by_id["c"]["start"] == {"old": 12, "new": 13}
    assert by_id["c"]["label"] == {"old": "丙", "new": "丙改"}
    assert by_id["c"]["holder"] == {"old": None, "new": "bob"}
    assert by_id["c"]["changed_fields"] == ["start", "label", "holder"]
    # 码点序：a < b < c
    assert [c["id"] for c in result["changes"]] == ["a", "b", "c"]


def test_store_diff_reverse(store):
    cid = _build_history(store)
    result = store.diff_revisions(cid, 4, 1)
    assert result["from_revision"] == 4 and result["to_revision"] == 1
    assert result["statuses"] == {"from": "open", "to": "open"}
    by_id = {c["id"]: c for c in result["changes"]}
    assert by_id["c"]["start"] == {"old": 13, "new": 12}
    assert by_id["c"]["label"] == {"old": "丙改", "new": "丙"}
    assert by_id["a"]["holder"] == {"old": "alice", "new": None}


def test_store_diff_same_revision_empty(store):
    cid = _build_history(store)
    result = store.diff_revisions(cid, 3, 3)
    assert result["changes"] == []
    assert result["statuses"] == {"from": "open", "to": "open"}


def test_store_diff_finalize_only_empty_changes_both_statuses(store):
    cid = _build_history(store)
    result = store.diff_revisions(cid, 6, 7)
    assert result["changes"] == []  # 结稿只翻 status，批注未变
    assert result["statuses"] == {"from": "open", "to": "finalized"}
    # 反向：状态方向也按入参确定
    reverse = store.diff_revisions(cid, 7, 6)
    assert reverse["statuses"] == {"from": "finalized", "to": "open"}
    assert reverse["changes"] == []


def test_store_diff_ignores_saved_order(store):
    # 创建顺序 z,a,m；修改把 z 挪到 a 与 m 之间——纯顺序变化不出现在清单
    cid = store.create(TEXT, [ann("z", 15, 18, "丁"), ann("a", 0, 3, "甲"),
                              ann("m", 8, 10, "丙")])["id"]
    store.claim(cid, "z", "alice")  # rev 2
    store.modify(cid, "z", "alice", 4, 6, "丁改")  # rev 3
    result = store.diff_revisions(cid, 1, 3)
    by_id = {c["id"]: c for c in result["changes"]}
    assert set(by_id) == {"z"}  # a、m 仅相对位置变化，不算改动
    z = by_id["z"]
    assert z["start"] == {"old": 15, "new": 4}
    assert z["end"] == {"old": 18, "new": 6}
    assert z["label"] == {"old": "丁", "new": "丁改"}
    assert z["holder"] == {"old": None, "new": "alice"}


def test_store_diff_special_ids_changed(store):
    cid = store.create(TEXT, [
        ann("", 0, 2, "空"), ann("a/b", 5, 8, "斜"),
        ann("a%2Fb", 12, 15, "百分"), ann("a\nb", 15, 18, "行")])["id"]
    store.claim(cid, "", "alice")                       # rev 2：空串 id 整组
    store.modify(cid, "", "alice", 0, 1, "空改")        # rev 3
    result = store.diff_revisions(cid, 1, 3)
    assert [c["id"] for c in result["changes"]] == [""]
    e = result["changes"][0]
    assert e["end"] == {"old": 2, "new": 1}
    assert e["label"] == {"old": "空", "new": "空改"}
    assert e["holder"] == {"old": None, "new": "alice"}
    # 未变的特殊 id（含 /、字面 %2F、换行）按完整 id 对齐后不出现
    assert len(result["changes"]) == 1


def test_store_diff_readonly(store):
    cid = _build_history(store)
    before = store.get(cid)
    store.diff_revisions(cid, 1, 7)
    store.diff_revisions(cid, 7, 1)
    assert store.get(cid) == before
    assert [r["revision"] for r in store.list_revisions(cid)] == \
        list(range(1, 8))


def test_store_diff_unknown_copy_and_revision_404(store):
    with pytest.raises(CopyNotFound):
        store.diff_revisions("deadbeef", 1, 2)
    cid = _build_history(store)
    for bad in (0, 8, 99, -1):
        with pytest.raises(RevisionNotFound):
            store.diff_revisions(cid, 1, bad)
        with pytest.raises(RevisionNotFound):
            store.diff_revisions(cid, bad, 7)


def test_store_diff_baseline_only_no_inferred_history(tmp_path):
    # 旧库仅有 revision=5 的 baseline：不推测 1..4，比较任一端缺失即 404
    db = tmp_path / "copies.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE copies (
            id TEXT PRIMARY KEY, text TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE annotations (
            copy_id TEXT NOT NULL, ann_id TEXT NOT NULL, pos INTEGER NOT NULL,
            start INTEGER NOT NULL, end INTEGER NOT NULL, label TEXT NOT NULL,
            holder TEXT, PRIMARY KEY (copy_id, ann_id));
    """)
    conn.execute("INSERT INTO copies(id, text, revision) VALUES('c1', ?, 5)",
                 (TEXT,))
    conn.execute(
        "INSERT INTO annotations VALUES('c1', 'a', 0, 0, 3, 'L', NULL)")
    conn.commit()
    conn.close()
    store = CopyStore(db)
    with pytest.raises(RevisionNotFound):
        store.diff_revisions("c1", 1, 5)  # 不编造早期历史
    with pytest.raises(RevisionNotFound):
        store.diff_revisions("c1", 5, 6)
    # baseline 与自身比较合法：空清单
    assert store.diff_revisions("c1", 5, 5)["changes"] == []


# ---------- HTTP 端到端 ----------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COPY_DB_PATH", str(tmp_path / "copies.db"))
    import app.main as main
    importlib.reload(main)
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c
    main._store = None


def _create(client, anns):
    r = client.post("/api/copies", json={"text": TEXT, "annotations": anns})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_http_diff_full_flow(client):
    cid = _create(client, [ann("a", 0, 4, "甲"), ann("b", 3, 7, "乙"),
                           ann("c", 12, 15, "丙")])
    client.post(f"/api/copies/{cid}/annotations/a/claim",
                json={"reviewer": "alice"})
    client.post(f"/api/copies/{cid}/annotations/c/claim",
                json={"reviewer": "bob"})
    client.patch(f"/api/copies/{cid}/annotations/c",
                 json={"reviewer": "bob", "start": 13, "end": 15,
                       "label": "丙改"})
    client.post(f"/api/copies/{cid}/annotations/b/release",
                json={"reviewer": "alice"})
    client.post(f"/api/copies/{cid}/annotations/c/release",
                json={"reviewer": "bob"})
    client.post(f"/api/copies/{cid}/finalize",
                json={"expected_revision": 6})

    # 正向 1 → 4
    r = client.get(f"/api/copies/{cid}/revisions/diff",
                   params={"from_revision": 1, "to_revision": 4})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == cid
    assert body["from_revision"] == 1 and body["to_revision"] == 4
    assert body["statuses"] == {"from": "open", "to": "open"}
    by_id = {c["id"]: c for c in body["changes"]}
    assert [c["id"] for c in body["changes"]] == ["a", "b", "c"]
    assert by_id["b"]["changed_fields"] == ["holder"]  # rev4 时 b 仍被持有
    assert by_id["b"]["holder"] == {"old": None, "new": "alice"}
    assert by_id["b"]["start"] == {"old": 3, "new": 3}
    assert by_id["c"]["changed_fields"] == ["start", "label", "holder"]

    # 反向 4 → 1：旧值/新值方向对调
    r = client.get(f"/api/copies/{cid}/revisions/diff",
                   params={"from_revision": 4, "to_revision": 1})
    body = r.json()
    by_id = {c["id"]: c for c in body["changes"]}
    assert by_id["c"]["start"] == {"old": 13, "new": 12}
    assert by_id["c"]["label"] == {"old": "丙改", "new": "丙"}
    assert by_id["a"]["holder"] == {"old": "alice", "new": None}

    # 相同版本：空清单
    r = client.get(f"/api/copies/{cid}/revisions/diff",
                   params={"from_revision": 2, "to_revision": 2})
    assert r.status_code == 200
    assert r.json()["changes"] == []
    assert r.json()["statuses"] == {"from": "open", "to": "open"}

    # 仅结稿变化（6 → 7）：清单为空，但两端 status 都返回
    r = client.get(f"/api/copies/{cid}/revisions/diff",
                   params={"from_revision": 6, "to_revision": 7})
    assert r.status_code == 200
    assert r.json()["changes"] == []
    assert r.json()["statuses"] == {"from": "open", "to": "finalized"}


def test_http_diff_order_only_not_a_change(client):
    cid = _create(client, [ann("z", 15, 18, "丁"), ann("a", 0, 3, "甲"),
                           ann("m", 8, 10, "丙")])
    client.post(f"/api/copies/{cid}/annotations/z/claim",
                json={"reviewer": "alice"})
    client.patch(f"/api/copies/{cid}/annotations/z",
                 json={"reviewer": "alice", "start": 4, "end": 6,
                       "label": "丁改"})
    r = client.get(f"/api/copies/{cid}/revisions/diff",
                   params={"from_revision": 1, "to_revision": 3})
    assert r.status_code == 200
    assert [c["id"] for c in r.json()["changes"]] == ["z"]


def test_http_diff_special_ids(client):
    cid = _create(client, [
        ann("", 0, 2, "空"), ann("a/b", 5, 8, "斜"),
        ann("a%2Fb", 12, 15, "百分"), ann("a\n", 15, 18, "行")])
    client.post(f"/api/copies/{cid}/annotations//claim",
                json={"reviewer": "alice"})
    client.patch(f"/api/copies/{cid}/annotations/",
                 json={"reviewer": "alice", "start": 0, "end": 1,
                       "label": "空改"})
    r = client.get(f"/api/copies/{cid}/revisions/diff",
                   params={"from_revision": 1, "to_revision": 3})
    assert r.status_code == 200
    changes = r.json()["changes"]
    assert [c["id"] for c in changes] == [""]
    assert changes[0]["end"] == {"old": 2, "new": 1}
    assert changes[0]["label"] == {"old": "空", "new": "空改"}


def test_http_diff_unknown_copy_and_revision_404(client):
    r = client.get("/api/copies/deadbeef/revisions/diff",
                   params={"from_revision": 1, "to_revision": 2})
    assert r.status_code == 404
    cid = _create(client, [ann("a", 0, 3)])
    client.post(f"/api/copies/{cid}/annotations/a/claim",
                json={"reviewer": "alice"})  # rev 2
    # 未记录的正整数版本 → 404
    r = client.get(f"/api/copies/{cid}/revisions/diff",
                   params={"from_revision": 1, "to_revision": 3})
    assert r.status_code == 404
    r = client.get(f"/api/copies/{cid}/revisions/diff",
                   params={"from_revision": 99, "to_revision": 1})
    assert r.status_code == 404


@pytest.mark.parametrize("query", [
    "from_revision=0&to_revision=1",
    "from_revision=-2&to_revision=1",
    "from_revision=true&to_revision=1",
    "from_revision=false&to_revision=1",
    "from_revision=abc&to_revision=1",
    "from_revision=1&to_revision=0",
    "to_revision=1",
    "from_revision=1",
    "from_revision=1&to_revision=",
])
def test_http_diff_invalid_params_422(client, query):
    cid = _create(client, [ann("a", 0, 3)])
    r = client.get(f"/api/copies/{cid}/revisions/diff?{query}")
    assert r.status_code == 422, query


def test_http_diff_does_not_shadow_single_revision_route(client):
    # 字面段 diff 不影响 /revisions/{revision} 单版本回看
    cid = _create(client, [ann("a", 0, 3)])
    r = client.get(f"/api/copies/{cid}/revisions/1")
    assert r.status_code == 200 and r.json()["revision"] == 1
    r = client.get(f"/api/copies/{cid}/revisions/0")
    assert r.status_code == 404


def test_http_diff_readonly(client):
    cid = _create(client, [ann("a", 0, 3, "甲"), ann("b", 5, 8, "乙")])
    client.post(f"/api/copies/{cid}/annotations/a/claim",
                json={"reviewer": "alice"})
    before = client.get(f"/api/copies/{cid}").json()
    client.get(f"/api/copies/{cid}/revisions/diff",
               params={"from_revision": 1, "to_revision": 2})
    client.get(f"/api/copies/{cid}/revisions/diff",
               params={"from_revision": 2, "to_revision": 1})
    assert client.get(f"/api/copies/{cid}").json() == before
    assert [r["revision"] for r in
            client.get(f"/api/copies/{cid}/revisions").json()["revisions"]] \
        == [1, 2]
