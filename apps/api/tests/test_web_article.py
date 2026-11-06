from pathlib import Path

import pytest
from wiki_agent.models import SourceKind
from wiki_agent.sources.article import WebContentError, extract_web_article, public_article_url
from wiki_agent.sources.parser import DocumentParser

ARTICLE = (
    b'<html><head><meta charset="utf-8"><title>Reliable importing</title></head><body>'
    b'<nav>Menu noise that must not be imported</nav>'
    b'<article><h1>Reliable importing</h1>'
    b'<p>Article extraction preserves the useful main content without importing navigation.</p>'
    b'<h2>Details</h2><p>Read <a href="/guide">the guide</a> for additional instructions.</p>'
    b'</article><footer>Footer noise that must not be imported</footer></body></html>'
)


def test_public_article_extracts_body_title_and_absolute_links_without_navigation() -> None:
    result = extract_web_article(ARTICLE, "https://example.com/article")
    assert result.title == "Reliable importing"
    assert "Article extraction preserves" in result.markdown
    assert "# Reliable importing" in result.markdown
    assert "https://example.com/guide" in result.markdown
    assert "Menu noise" not in result.markdown
    assert "Footer noise" not in result.markdown


def test_wechat_extracts_nested_main_content_instead_of_page_chrome(tmp_path: Path) -> None:
    html = (
        '<html><head><meta charset="utf-8"><title>微信公众平台</title></head><body>'
        '<h1 id="activity-name">测试文章标题</h1><div id="js_content">'
        '<section><p><span>这是微信公众号文章的完整正文，包含足够的内容用于知识整理。</span></p>'
        '<p>第二段讲解如何可靠地导入文章，不能保存登录页或验证码页面。</p></section></div>'
        '<div>点赞、分享、关注公众号，这部分不是文章正文。</div>'
        '<script>const SECRET_TEST_MARKER="not article content";</script></body></html>'
    )
    result = extract_web_article(html.encode(), "https://mp.weixin.qq.com/s/test")
    assert result.title == "测试文章标题"
    assert result.extractor == "wechat"
    assert "完整正文" in result.markdown
    assert "第二段" in result.markdown
    assert "点赞" not in result.markdown
    assert "SECRET_TEST_MARKER" not in result.markdown
    source = tmp_path / "wechat.html"
    source.write_text(html, encoding="utf-8")
    parsed = DocumentParser().parse(
        source, SourceKind.WEBSITE, source_url="https://mp.weixin.qq.com/s/test",
    )
    assert parsed.parser == "wechat"
    assert parsed.markdown == result.markdown
    assert parsed.sections


@pytest.mark.parametrize(
    ("body", "url", "code"),
    [
        (
            '<title>环境异常</title><main><p>请完成安全验证之后继续阅读文章，'
            '此处只有验证提示而不是文章正文。</p></main>',
            "https://mp.weixin.qq.com/s/test", "verification_required",
        ),
        (
            ARTICLE.decode(), "https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha",
            "verification_required",
        ),
        (
            '<title>登录</title><main><p>登录后才可以阅读此文章，请输入账号密码继续。</p>'
            '<form><input type="password" value="SECRET_TEST_MARKER"></form></main>',
            "https://example.com/login", "login_required",
        ),
        (
            "<title>扫码登录 - 微信</title><main><p>请使用微信扫描二维码完成登录，"
            "登录之后才可以查看正文内容。</p></main>",
            "https://example.com/", "login_required",
        ),
        (
            "<title>Welcome</title><main><p>Please sign in to continue reading the full "
            "article. The login form was removed from the captured page.</p></main>",
            "https://example.com/auth/login", "login_required",
        ),
        ("<div id='app'></div><script>renderArticle()</script>",
         "https://example.com/article", "empty_content"),
        ("<article><h1>A very long title without any actual article text</h1></article>",
         "https://example.com/article", "empty_content"),
    ],
)
def test_non_articles_are_rejected_without_exposing_page_content(
    body: str, url: str, code: str,
) -> None:
    with pytest.raises(WebContentError) as failure:
        extract_web_article(body.encode(), url)
    assert failure.value.code == code
    assert "SECRET_TEST_MARKER" not in str(failure.value)


def test_article_about_captcha_is_not_mistaken_for_a_verification_page() -> None:
    html = (
        "<title>How CAPTCHA systems work</title><article><h1>How CAPTCHA systems work</h1>"
        "<p>This article explains CAPTCHA design and login flows without requiring a login."
        "</p></article>"
    )
    assert "explains CAPTCHA" in extract_web_article(html.encode()).markdown
    assert "explains CAPTCHA" in extract_web_article(
        html.encode(), "https://example.com/docs/login",
    ).markdown


def test_saved_article_urls_drop_login_tokens_but_keep_article_identifiers() -> None:
    assert public_article_url(
        "https://example.com/article?id=123&access_token=SECRET&code=SECRET"
        "&session_id=SECRET&csrf-token=SECRET&password=SECRET#section"
    ) == "https://example.com/article?id=123"


def test_plain_text_threshold_counts_non_whitespace_characters(tmp_path: Path) -> None:
    assert extract_web_article(
        ("a " * 20).encode(), "https://example.com/readme", "text/plain",
    ).markdown
    with pytest.raises(WebContentError, match="20"):
        extract_web_article(("a " * 19).encode(), content_type="text/plain")
    source = tmp_path / "page.txt"
    source.write_text("A useful public plain text document.", encoding="utf-8")
    parsed = DocumentParser().parse(source, SourceKind.WEBSITE, media_type="text/plain")
    assert parsed.parser == "plain-text"
    assert "useful public" in parsed.markdown
