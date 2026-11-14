from app import books
from app.generate import _chapter_outline_context, _previous_chapter_context
from app.tree import (
    add_ending_branch,
    new_ending_plan,
    ancestor_chapter_ids,
    get_book_tree,
    outline_section_for_chapter,
    parse_outline_nodes,
    path_outline_chapters,
    sync_book_tree,
)


BRANCHED_OUTLINE = """# 《枝书》

## 故事线

一条共用主干，随后分成两个结局。

## 大纲

### 第一章 开端

主干开端说明 TRUNK_OUTLINE_1。

### 第二章 分叉前

主干分叉前说明 TRUNK_OUTLINE_2。

### 第三章 一结局 路甲

结局甲前半 OUTLINE_ENDING_A1。

### 第四章 一结局 路甲终

结局甲收束 OUTLINE_ENDING_A2。

### 第三章 二结局 路乙

结局乙前半 OUTLINE_ENDING_B1。

### 第四章 二结局 路乙终

结局乙收束 OUTLINE_ENDING_B2。
"""


LINEAR_OUTLINE = """# 《线书》

## 大纲

### 第一章 起

线性一。

### 第二章 承

线性二。

### 第三章 转

线性三。
"""


def _write_branched_book(name="枝书"):
    books.create_book(name)
    books.write_outline(name, BRANCHED_OUTLINE)
    books.write_chapter(name, "01", "TRUNK_START\n")
    books.write_chapter(name, "02", "TRUNK_FORK\n")
    books.write_chapter(name, "03", "ENDING_A_START\n")
    books.write_chapter(name, "04", "ENDING_A_END\n")
    books.write_chapter(name, "05", "ENDING_B_START\n")
    books.write_chapter(name, "06", "ENDING_B_END\n")
    return sync_book_tree(name)


def test_parse_repeated_chapter_numbers_as_sibling_endings():
    nodes = parse_outline_nodes(BRANCHED_OUTLINE)
    assert [node["num"] for node in nodes] == [1, 2, 3, 4, 3, 4]
    assert [node.get("branch") for node in nodes] == [None, None, "一结局", "一结局", "二结局", "二结局"]
    # parents: 0 none, 1->0, 2->1 (fork after ch2), 3->2, 4->1 (sibling fork), 5->4
    assert nodes[0]["parent_index"] is None
    assert nodes[1]["parent_index"] == 0
    assert nodes[2]["parent_index"] == 1
    assert nodes[3]["parent_index"] == 2
    assert nodes[4]["parent_index"] == 1
    assert nodes[5]["parent_index"] == 4


def test_sync_maps_files_in_order_for_duplicate_chapter_numbers(article_root):
    tree = _write_branched_book()
    by_cid = {node["chapter_id"]: node for node in tree["nodes"]}
    assert set(by_cid) == {"01", "02", "03", "04", "05", "06"}
    assert by_cid["03"]["branch"] == "一结局"
    assert by_cid["03"]["num"] == 3
    assert by_cid["04"]["parent_id"] == "03"
    assert by_cid["05"]["branch"] == "二结局"
    assert by_cid["05"]["parent_id"] == "02"
    assert by_cid["06"]["parent_id"] == "05"
    assert by_cid["02"]["children"] == ["03", "05"]
    assert tree["has_forks"] is True


def test_previous_context_skips_sibling_ending(article_root):
    _write_branched_book()
    ctx_b = _previous_chapter_context("枝书", "05")
    assert "ENDING_A_END" not in ctx_b
    assert "ENDING_A_START" not in ctx_b
    assert "TRUNK_FORK" in ctx_b
    assert "ENDING_B_START" not in ctx_b

    ctx_b2 = _previous_chapter_context("枝书", "06")
    assert "ENDING_A_END" not in ctx_b2
    assert "ENDING_A_START" not in ctx_b2
    assert "ENDING_B_START" in ctx_b2

    ctx_a = _previous_chapter_context("枝书", "04")
    assert "ENDING_A_START" in ctx_a
    assert "ENDING_B_START" not in ctx_a
    assert "ENDING_B_END" not in ctx_a

    assert ancestor_chapter_ids("枝书", "05") == ["02", "01"]
    assert ancestor_chapter_ids("枝书", "06") == ["05", "02", "01"]
    assert ancestor_chapter_ids("枝书", "04") == ["03", "02", "01"]


def test_outline_section_and_compact_outline_stay_on_path(article_root):
    _write_branched_book()
    section = outline_section_for_chapter("枝书", "05")
    assert "OUTLINE_ENDING_B1" in section
    assert "OUTLINE_ENDING_A1" not in section

    preamble, chapters = books.split_outline_chapters(BRANCHED_OUTLINE)
    current_section, compact, display_num = _chapter_outline_context(
        "枝书", "05", BRANCHED_OUTLINE, preamble, chapters, 5
    )
    assert display_num == 3
    assert "OUTLINE_ENDING_B1" in current_section
    assert "OUTLINE_ENDING_A1" not in compact
    assert "OUTLINE_ENDING_A2" not in compact
    assert "TRUNK_OUTLINE_2" in compact
    path_chapters = path_outline_chapters("枝书", "05", chapters)
    assert [ch["num"] for ch in path_chapters] == [1, 2, 3, 4]
    assert all("OUTLINE_ENDING_A" not in (ch["text"] or "") for ch in path_chapters)


def test_linear_book_previous_context_unchanged(article_root):
    books.create_book("线书")
    books.write_outline("线书", LINEAR_OUTLINE)
    books.write_chapter("线书", "01", "LIN_ONE\n")
    books.write_chapter("线书", "02", "LIN_TWO\n")
    books.write_chapter("线书", "03", "LIN_THREE\n")
    tree = get_book_tree("线书")
    assert tree["has_forks"] is False
    assert [node["parent_id"] for node in tree["nodes"]] == [None, "01", "02"]
    ctx = _previous_chapter_context("线书", "03")
    assert "LIN_TWO" in ctx
    assert "LIN_ONE" in ctx
    assert "LIN_THREE" not in ctx


def test_linear_book_without_outline_uses_numeric_previous(article_root):
    books.create_book("空纲")
    books.write_chapter("空纲", "01", "LIN_ONE\n")
    books.write_chapter("空纲", "02", "LIN_TWO\n")
    books.write_chapter("空纲", "03", "LIN_THREE\n")
    ctx = _previous_chapter_context("空纲", "03")
    assert "LIN_TWO" in ctx
    assert "LIN_ONE" in ctx


def test_add_branch_preserves_existing_outline_and_files(article_root):
    _write_branched_book()
    marker = "TRUNK_OUTLINE_1"
    result = add_ending_branch("枝书", "02", "三结局", idea="另辟一条路", chapter_count=2)
    assert result["ok"] is True
    outline = books.read_outline("枝书")
    assert marker in outline
    assert "### 第三章 一结局 路甲" in outline
    assert "### 第三章 二结局 路乙" in outline
    assert "三结局" in outline
    assert "另辟一条路" in outline
    assert "<!-- 分支：三结局；分叉于：02 -->" in outline
    new_ids = result["chapter_ids"]
    assert len(new_ids) == 2
    assert new_ids[0] == "07"
    assert new_ids[1] == "08"
    # existing files untouched
    assert "ENDING_A_END" in books.read_chapter("枝书", "04")["text"]
    assert "ENDING_B_START" in books.read_chapter("枝书", "05")["text"]
    tree = result["tree"]
    by_cid = {node["chapter_id"]: node for node in tree["nodes"]}
    assert by_cid["07"]["parent_id"] == "02"
    assert by_cid["07"]["branch"] == "三结局"
    assert by_cid["08"]["parent_id"] == "07"
    assert "07" in by_cid["02"]["children"]
    ctx = _previous_chapter_context("枝书", "07")
    assert "ENDING_A_END" not in ctx
    assert "ENDING_B_START" not in ctx
    assert "TRUNK_FORK" in ctx


def test_add_ending_from_existing_ending_aligns_chapter_numbers(article_root):
    tree = _write_branched_book()
    # Clicking 第四章 二结局 (file 06) should fork from 第二章, start at 第三章, name 三结局.
    plan = new_ending_plan(tree, "06")
    assert plan["from_chapter"] == "02"
    assert plan["start_num"] == 3
    assert plan["start_label"] == "第三章"
    assert plan["suggested_branch"] == "三结局"
    assert "一结局" in plan["sibling_branches"] and "二结局" in plan["sibling_branches"]
    from_first = new_ending_plan(tree, "03")  # 第三章 一结局
    assert from_first["from_chapter"] == "02"
    assert from_first["start_num"] == 3
    from_fork = new_ending_plan(tree, "02")
    assert from_fork["from_chapter"] == "02"
    assert from_fork["start_num"] == 3
    assert from_fork["suggested_branch"] == "三结局"

    result = add_ending_branch("枝书", "06", "三结局", idea="第三条路", chapter_count=2)
    from app import books
    outline = books.read_outline("枝书")
    assert "<!-- 分支：三结局；分叉于：02 -->" in outline
    assert "### 第三章 三结局" in outline
    assert "### 第四章 三结局" in outline
    by_cid = {node["chapter_id"]: node for node in result["tree"]["nodes"]}
    new_ids = result["chapter_ids"]
    assert by_cid[new_ids[0]]["parent_id"] == "02"
    assert by_cid[new_ids[0]]["num"] == 3
    assert by_cid[new_ids[0]]["branch"] == "三结局"
    assert by_cid[new_ids[1]]["num"] == 4
    # existing endings untouched
    assert "ENDING_A_END" in books.read_chapter("枝书", "04")["text"]
    assert "ENDING_B_END" in books.read_chapter("枝书", "06")["text"]


def test_delete_chapter_keeps_backup_and_does_not_renumber(article_root):
    _write_branched_book()
    deleted = books.delete_chapter("枝书", "05")
    assert deleted["ok"] is True
    assert deleted["backed_up"] is True
    assert not books.chapter_path("枝书", "05").exists()
    ids = [item["id"] for item in books.list_chapters("枝书")]
    assert "05" not in ids
    assert ids == ["01", "02", "03", "04", "06"]
    assert books.chapter_has_backup("枝书", "05") is True
    restored = books.restore_saved_chapter("枝书", "05")
    assert "ENDING_B_START" in restored["text"]
    assert books.chapter_path("枝书", "05").exists()
    # other chapters and numbers stay put
    assert "ENDING_B_END" in books.read_chapter("枝书", "06")["text"]
    assert "ENDING_A_END" in books.read_chapter("枝书", "04")["text"]


def test_delete_missing_chapter_raises(article_root):
    books.create_book("空书")
    import pytest

    with pytest.raises(books.BookError, match="章节不存在"):
        books.delete_chapter("空书", "01")


def test_revise_outline_in_place_keeps_chapter_attachments(article_root):
    _write_branched_book()
    outline = books.read_outline("枝书")
    revised = outline.replace("路乙终", "路乙终修订")
    books.write_outline("枝书", revised)
    tree = get_book_tree("枝书")
    by_cid = {node["chapter_id"]: node for node in tree["nodes"]}
    assert by_cid["06"]["title"] == "路乙终修订"
    assert by_cid["05"]["parent_id"] == "02"
    assert "ENDING_B_END" in books.read_chapter("枝书", "06")["text"]
    assert "ENDING_A_END" in books.read_chapter("枝书", "04")["text"]


def test_parse_chinese_numerals_and_ending_labels():
    outline = """# 《试》

## 大纲

### 第八章 分叉前

共用。

### 第九章 一结局 路甲

甲一。

### 第十章 一结局 路甲终

甲二。

### 第九章 二结局 路乙

乙一。

### 第十章 二结局 路乙终

乙二。
"""
    nodes = parse_outline_nodes(outline)
    assert [node["num"] for node in nodes] == [8, 9, 10, 9, 10]
    assert [node.get("branch") for node in nodes] == [None, "一结局", "一结局", "二结局", "二结局"]
    assert nodes[1]["parent_index"] == 0
    assert nodes[3]["parent_index"] == 0
    assert nodes[4]["parent_index"] == 3



def test_mismatched_body_heading_keeps_file_id(article_root):
    """书2-style: 01.md opens with 第五章, ending-2 files are numbered 11/12."""
    books.create_book("错题")
    outline = """# 《错题》

## 大纲

### 第一章 501与502

开端。

### 第二章 监控下的罪证

其二。

### 第五章 K9乳胶与牵引

其五。UNIQUE_CH5_OUTLINE

### 第八章 四爱之姿

其八。

### 第九章 一结局 反噬与逼近

甲一。ENDING_A1_OUTLINE

### 第十章 一结局 最终的囚笼

甲二。

### 第九章 二结局 反噬与逃离

乙一。ENDING_B1_OUTLINE

### 第十章 二结局 互为主奴的囚笼

乙二。
"""
    books.write_outline(name := "错题", outline)
    books.write_chapter(name, "01", "第五章 K9乳胶与牵引\nCHAPTER_ONE_BODY\n")
    books.write_chapter(name, "02", "第二章 监控下的罪证\nCHAPTER_TWO_BODY\n")
    books.write_chapter(name, "05", "第五章 K9，牵引，露出与释放\nCHAPTER_FIVE_BODY\n")
    books.write_chapter(name, "08", "第八章 四爱之姿\nCHAPTER_EIGHT_BODY\n")
    books.write_chapter(name, "09", "第九章 一结局 反噬与逼近\nENDING_A_START\n")
    books.write_chapter(name, "10", "# 第十章 一结局 最终的囚笼\nENDING_A_END\n")
    books.write_chapter(name, "11", "第十一章 二结局 反噬与逃离\nENDING_B_START\n")
    books.write_chapter(name, "12", "")
    tree = sync_book_tree(name)
    by_cid = {node["chapter_id"]: node for node in tree["nodes"]}
    assert by_cid["01"]["num"] == 1
    assert by_cid["01"]["title"] == "501与502"
    assert by_cid["01"]["display_title"] == "第一章 501与502"
    assert "第五章" not in by_cid["01"]["display_title"]
    assert by_cid["05"]["num"] == 5
    assert by_cid["05"]["title"] == "K9乳胶与牵引"
    assert "CHAPTER_FIVE_BODY" in books.read_chapter(name, "05")["text"]
    assert "CHAPTER_ONE_BODY" in books.read_chapter(name, "01")["text"]
    assert by_cid["09"]["branch"] == "一结局"
    assert by_cid["11"]["branch"] == "二结局"
    assert by_cid["11"]["num"] == 9
    assert by_cid["11"]["parent_id"] == "08"
    assert by_cid["12"]["parent_id"] == "11"
    assert by_cid["10"]["branch"] == "一结局"
    assert "01" not in (by_cid["05"].get("children") or [])
    assert ancestor_chapter_ids(name, "11") == ["08", "05", "02", "01"]
    ctx_b = _previous_chapter_context(name, "11")
    assert "ENDING_A_END" not in ctx_b
    assert "ENDING_A_START" not in ctx_b
    assert "CHAPTER_EIGHT_BODY" in ctx_b
    ctx_a = _previous_chapter_context(name, "10")
    assert "ENDING_A_START" in ctx_a
    assert "ENDING_B_START" not in ctx_a
