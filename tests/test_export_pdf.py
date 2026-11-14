"""Unit checks for full-book PDF export: walk order, shared trunk, TOC destinations."""

from app import export_pdf


def _node(nid, chapter_id, parent_id=None, children=None, num=None, title="", branch=None):
    return {
        "id": nid,
        "chapter_id": chapter_id,
        "parent_id": parent_id,
        "children": list(children or []),
        "num": num,
        "title": title,
        "branch": branch,
    }


def test_linear_tree_has_no_headings():
    tree = {
        "nodes": [
            _node("n1", "01", None, ["n2"], 1, "一"),
            _node("n2", "02", "n1", ["n3"], 2, "二"),
            _node("n3", "03", "n2", [], 3, "三"),
        ]
    }
    items = export_pdf.walk_export_tree(tree)
    assert [i["kind"] for i in items] == ["chapter", "chapter", "chapter"]
    assert [i["chapter_id"] for i in items] == ["01", "02", "03"]
    assert [i["dest"] for i in items] == ["ch-01", "ch-02", "ch-03"]
    assert all(i["toc_level"] == 0 for i in items)


def test_labeled_fork_shared_trunk_once():
    tree = {
        "nodes": [
            _node("n1", "01", None, ["n2"], 1),
            _node("n2", "02", "n1", ["n3a", "n3b", "n3c"], 2),
            _node("n3a", "03", "n2", ["n4a"], 3, branch="一结局"),
            _node("n4a", "04", "n3a", [], 4, branch="一结局"),
            _node("n3b", "05", "n2", ["n4b"], 3, branch="二结局"),
            _node("n4b", "06", "n3b", [], 4, branch="二结局"),
            _node("n3c", "07", "n2", [], 3, branch="三结局"),
        ]
    }
    items = export_pdf.walk_export_tree(tree)
    kinds = [(i["kind"], i.get("chapter_id") or i.get("title"), i["toc_level"], i["dest"]) for i in items]
    assert kinds == [
        ("chapter", "01", 0, "ch-01"),
        ("chapter", "02", 0, "ch-02"),
        ("heading", "一结局", 0, "br-n3a"),
        ("chapter", "03", 1, "ch-03"),
        ("chapter", "04", 1, "ch-04"),
        ("heading", "二结局", 0, "br-n3b"),
        ("chapter", "05", 1, "ch-05"),
        ("chapter", "06", 1, "ch-06"),
        ("heading", "三结局", 0, "br-n3c"),
        ("chapter", "07", 1, "ch-07"),
    ]
    assert [i["chapter_id"] for i in items if i["kind"] == "chapter" and i["toc_level"] == 0] == ["01", "02"]


def test_children_list_order_not_num_order():
    tree = {
        "nodes": [
            _node("root", "01", None, ["late", "early"], 1),
            _node("early", "02", "root", [], 2, branch="先写的"),
            _node("late", "03", "root", [], 3, branch="后写的"),
        ]
    }
    items = export_pdf.walk_export_tree(tree)
    heads = [i["title"] for i in items if i["kind"] == "heading"]
    assert heads == ["后写的", "先写的"]


def test_one_unlabeled_sibling_is_mainline():
    tree = {
        "nodes": [
            _node("root", "01", None, ["a", "b"], 1),
            _node("a", "02", "root", [], 2, branch=None),
            _node("b", "03", "root", [], 2, branch="三结局"),
        ]
    }
    items = export_pdf.walk_export_tree(tree)
    assert [i.get("title") or i.get("chapter_id") for i in items] == ["01", "主线", "02", "三结局", "03"]


def test_several_unlabeled_use_ending_numerals():
    tree = {
        "nodes": [
            _node("root", "01", None, ["a", "b"], 1),
            _node("a", "02", "root", [], 2),
            _node("b", "03", "root", [], 2),
        ]
    }
    items = export_pdf.walk_export_tree(tree)
    assert [i.get("title") for i in items if i["kind"] == "heading"] == ["结局一", "结局二"]


def test_nested_fork_recurses():
    tree = {
        "nodes": [
            _node("n1", "01", None, ["n2a", "n2b"], 1),
            _node("n2a", "02", "n1", ["n3"], 2, branch="一结局"),
            _node("n3", "03", "n2a", ["n4x", "n4y"], 3, branch="一结局"),
            _node("n4x", "04", "n3", [], 4, branch="分支甲"),
            _node("n4y", "05", "n3", [], 4, branch="分支乙"),
            _node("n2b", "06", "n1", [], 2, branch="二结局"),
        ]
    }
    items = export_pdf.walk_export_tree(tree)
    snapshot = [(i["kind"], i.get("chapter_id") or i.get("title"), i["toc_level"]) for i in items]
    assert snapshot == [
        ("chapter", "01", 0),
        ("heading", "一结局", 0),
        ("chapter", "02", 1),
        ("chapter", "03", 1),
        ("heading", "分支甲", 1),
        ("chapter", "04", 2),
        ("heading", "分支乙", 1),
        ("chapter", "05", 2),
        ("heading", "二结局", 0),
        ("chapter", "06", 1),
    ]


def test_destinations_are_ascii_and_unique():
    tree = {
        "nodes": [
            _node("中文", "01", None, ["a", "b"], 1),
            _node("a", "02", "中文", [], 2, branch="一结局"),
            _node("b", "02", "中文", [], 2, branch="二结局"),
        ]
    }
    items = export_pdf.walk_export_tree(tree)
    dests = [i["dest"] for i in items]
    assert len(dests) == len(set(dests))
    for dest in dests:
        assert dest[0].isalpha() and dest.isascii()
        assert all(not ("\u4e00" <= ch <= "\u9fff") for ch in dest)


def test_prune_drops_heading_without_chapters():
    items = [
        {"kind": "chapter", "chapter_id": "01", "toc_level": 0, "dest": "ch-01"},
        {"kind": "heading", "title": "空结局", "toc_level": 0, "dest": "br-empty"},
        {"kind": "heading", "title": "有内容", "toc_level": 0, "dest": "br-ok"},
        {"kind": "chapter", "chapter_id": "02", "toc_level": 1, "dest": "ch-02"},
    ]
    pruned = export_pdf._prune_empty_headings(items)
    titles = [i.get("title") or i.get("chapter_id") for i in pruned]
    assert titles == ["01", "有内容", "02"]
