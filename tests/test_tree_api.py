import pytest
from fastapi.testclient import TestClient

from app import books
from app.main import app, jobs


@pytest.fixture
def client(article_root):
    return TestClient(app)


def _branched(name="接口书"):
    books.create_book(name)
    books.write_outline(
        name,
        """# 《接口书》

## 大纲

### 第一章 开端

主干一。

### 第二章 分叉前

主干二。

### 第三章 一结局 路甲

甲。
""",
    )
    books.write_chapter(name, "01", "TRUNK_START\n")
    books.write_chapter(name, "02", "TRUNK_FORK\n")
    books.write_chapter(name, "03", "ENDING_A_START\n")
    return name


def test_tree_and_add_branch_api(client, article_root):
    name = _branched()
    tree = client.get(f"/api/books/{name}/tree").json()
    assert tree["has_forks"] is False or any(n.get("chapter_id") == "03" for n in tree["nodes"])
    assert [n["chapter_id"] for n in tree["nodes"]] == ["01", "02", "03"]

    added = client.post(
        f"/api/books/{name}/tree/branches",
        json={"from_chapter": "02", "branch_name": "二结局", "idea": "另一条路", "count": 1},
    )
    assert added.status_code == 200, added.text
    data = added.json()
    assert "二结局" in data["outline"]
    assert "主干一" in data["outline"]
    assert data["chapter_ids"] == ["04"]
    by_cid = {n["chapter_id"]: n for n in data["tree"]["nodes"]}
    assert by_cid["04"]["parent_id"] == "02"
    assert by_cid["04"]["branch"] == "二结局"


def test_delete_and_restore_api(client, article_root):
    name = _branched()
    deleted = client.delete(f"/api/books/{name}/chapters/03")
    assert deleted.status_code == 200, deleted.text
    body = deleted.json()
    assert body["ok"] is True
    assert body["backed_up"] is True
    assert "03" not in [ch["id"] for ch in body["chapters"]]
    # file gone, backup remains
    assert not books.chapter_path(name, "03").exists()
    restored = client.post(f"/api/books/{name}/chapters/03/restore-saved")
    assert restored.status_code == 200, restored.text
    assert "ENDING_A_START" in restored.json()["text"]


def test_delete_refused_while_generating(client, article_root):
    name = _branched()
    jobs._job_id = "testdelete"
    jobs._cancel = object()
    jobs._book = name
    jobs._chapter = "03"
    try:
        resp = client.delete(f"/api/books/{name}/chapters/03")
        assert resp.status_code == 400
        assert "正在生成" in resp.json()["detail"]
        assert books.chapter_path(name, "03").exists()
    finally:
        jobs.finish("testdelete")


def test_get_book_includes_tree(client, article_root):
    name = _branched()
    data = client.get(f"/api/books/{name}").json()
    assert "tree" in data
    assert len(data["tree"]["nodes"]) == 3
