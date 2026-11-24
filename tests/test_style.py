import pytest

from app import books, style
from app.config import UNSAFE_TEMPLATE_REASON
from app.style import StyleSafetyError, check_sample_text


def _write_templates(article_root):
    folder = article_root / "template"
    adult = "这是可用的成年文风样本。夜色沉静，两位十八岁以上的人站在廊下。"
    for name in ("1.txt", "2.txt", "3.txt", "4.txt", "6.txt", "7.txt", "9.txt", "10.txt", "11.txt"):
        (folder / name).write_text(adult, encoding="utf-8")
    (folder / "5.txt").write_text("十六岁的学生在教室里发生性行为。", encoding="utf-8")
    (folder / "8.txt").write_text("幼女相关的不适内容", encoding="utf-8")
    (folder / "12.txt").write_text("高中生出示学生证后发生性行为。", encoding="utf-8")
    (folder / "99.txt").write_text("一份未点名的成年文风样本。", encoding="utf-8")


def test_adult_templates_are_selectable(article_root):
    _write_templates(article_root)
    books.create_book("风格书")
    listed = style.list_samples("风格书")
    shared = {item["name"]: item for item in listed["shared"]}
    assert "1.txt" in shared and shared["1.txt"]["allowed"] is True
    assert "4.txt" in shared and shared["4.txt"]["allowed"] is True
    assert "10.txt" in shared and shared["10.txt"]["allowed"] is True
    assert "99.txt" in shared and shared["99.txt"]["allowed"] is True
    assert "5.txt" in shared and shared["5.txt"]["allowed"] is False
    assert shared["5.txt"]["allowed"] is False
    assert "12.txt" in shared and shared["12.txt"]["allowed"] is False
    assert "8.txt" in shared and shared["8.txt"]["allowed"] is False


def test_never_resolve_blocked_templates(article_root):
    _write_templates(article_root)
    books.create_book("风格书")
    excerpts = style.resolve_style_ids(
        "风格书",
        ["template:5.txt", "template:8.txt", "template:12.txt", "template:4.txt", "template:1.txt"],
        200,
    )
    assert [item["name"] for item in excerpts] == ["4.txt", "1.txt"]


def test_reject_pasted_minor_sexual_content(article_root):
    books.create_book("风格书")
    with pytest.raises(StyleSafetyError):
        style.save_pasted_style("风格书", "这是一段涉及16岁未成年人的色情描写，包含性行为。")
    with pytest.raises(StyleSafetyError):
        style.save_pasted_style("风格书", "A lolicon style sample should be refused.")
    # 18-year-old adult content should be accepted.
    saved18 = style.save_pasted_style("风格书", "两位十八岁的成年人在雨夜里互相爱抚，描写含性爱内容。")
    assert saved18["id"].startswith("book:")
    # Adult sample should be accepted.
    saved = style.save_pasted_style("风格书", "两位成年角色在雨夜里对坐饮酒，语气克制。")
    assert saved["id"].startswith("book:")
    folder = article_root / "风格书" / "style"
    assert list(folder.glob("*.txt"))


def test_uploaded_book_sample_is_scanned(article_root):
    books.create_book("风格书")
    with pytest.raises(StyleSafetyError):
        style.save_uploaded_style("风格书", "bad.txt", "幼女相关的不适内容".encode("utf-8"))


def test_extract_readable_text_strips_nav_and_scripts():
    from app.webstyle import extract_readable_text

    html = """
    <html><head><script>var secret='skip';</script><style>p{}</style></head>
    <body>
      <nav>首页 登录 导航</nav>
      <article><p>这是正文段落，描写两位成年人在雨夜长谈。</p></article>
      <footer>版权所有</footer>
    </body></html>
    """
    text = extract_readable_text(html)
    assert "这是正文段落" in text
    assert "secret" not in text
    assert "首页 登录" not in text
    assert "版权所有" not in text


def test_reject_non_public_and_non_http_urls():
    from app.webstyle import StyleFetchError, validate_public_http_url
    import pytest

    for url in [
        "file:///etc/passwd",
        "ftp://example.com/a",
        "http://localhost/index.html",
        "http://127.0.0.1/",
        "http://192.168.1.8/foo",
        "http://10.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest",
    ]:
        with pytest.raises(StyleFetchError):
            validate_public_http_url(url)

    ok = validate_public_http_url("https://example.com/path")
    assert ok.startswith("https://")


def test_child_protection_and_adult_sex_are_allowed():
    adult_sex = "夜色里两人互相爱抚，描写含性爱与高潮。"
    check_sample_text("必须保护未成年人，严禁对儿童的伤害。" + adult_sex)
    check_sample_text("公园里儿童滑梯和废弃的儿童沙坑在夜色中投下轮廓。" + adult_sex)
    check_sample_text("林默，18岁，女。" + adult_sex)
    check_sample_text("申请人年龄：19岁。" + adult_sex)
    check_sample_text("22岁的林雪刚刚大学毕业。" + adult_sex)
    check_sample_text("拦着她的是一个五六十岁的老爷爷。" + adult_sex)
    check_sample_text("林默，18岁。或许是在10岁时作为画师的过程中看到第一张女性被五花大绑的画。" + adult_sex)
    check_sample_text("少女名为林默，18岁，从11岁开始接触绘画，很快成为了职业画师。" + adult_sex)
    check_sample_text("林默，18岁。有人一直对她虎视眈眈，只不过因为少女还未成年而苦苦忍耐着呢。" + adult_sex)


def test_current_minor_identity_still_blocked():
    sex = "随后出现了性行为和高潮描写。"
    with pytest.raises(StyleSafetyError):
        check_sample_text("少女名叫林默，十六岁，有着一头短发。" + sex)
    with pytest.raises(StyleSafetyError):
        check_sample_text("姓名：林默。性别：女。年龄：十六岁。籍贯：某市。" + sex)
    with pytest.raises(StyleSafetyError):
        check_sample_text("这妹子是个高中生！随后出现了色情和性爱描写。")
    with pytest.raises(StyleSafetyError):
        check_sample_text("十六岁的学生在教室里发生性行为。")
    with pytest.raises(StyleSafetyError):
        check_sample_text("这段文字写未成年人发生性行为。")
