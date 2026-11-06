import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from trafilatura import extract

MIN_ARTICLE_CHARACTERS = 20
MAX_WEB_BYTES = 5 * 1024 * 1024


def public_article_url(url: str) -> str:
    parsed = urlparse(url)
    sensitive = {
        "token", "code", "ticket", "jwt", "api_key", "password", "pwd",
        "authorization", "session", "session_id", "sessionid", "auth",
    }
    query = [
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower().replace("-", "_") not in sensitive
        and not key.lower().replace("-", "_").endswith("_token")
    ]
    return parsed._replace(query=urlencode(query), fragment="").geturl()


class WebContentError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WebArticle:
    title: str
    markdown: str
    extractor: str


def _meaningful_text(text: str) -> bool:
    return len(re.sub(r"\s+", "", text)) >= MIN_ARTICLE_CHARACTERS


def _article_text(element: Tag) -> str:
    content = BeautifulSoup(str(element), "html.parser")
    for item in content.select(
        "h1, h2, h3, h4, h5, h6, script, style, noscript, nav, aside, footer, form, button"
    ):
        item.decompose()
    return content.get_text(" ", strip=True)


def _title(soup: BeautifulSoup, url: str) -> str:
    for selector in ("#activity-name", "article h1", "main h1", "h1", "title"):
        element = soup.select_one(selector)
        if element and (text := element.get_text(" ", strip=True)):
            return text[:512]
    metadata = soup.select_one('meta[property="og:title"]')
    if metadata and isinstance(content := metadata.get("content"), str) and content.strip():
        return content.strip()[:512]
    return urlparse(url).hostname or "网页正文"


def extract_web_article(
    body: bytes, url: str = "", content_type: str = "text/html",
) -> WebArticle:
    if len(body) > MAX_WEB_BYTES:
        raise WebContentError("too_large", "网页超过 5 MB，无法导入。")
    if content_type == "text/plain":
        text = body.decode("utf-8-sig").strip()
        if not _meaningful_text(text):
            raise WebContentError("empty_content", "网页正文不足 20 个非空白字符，未导入。")
        return WebArticle(urlparse(url).hostname or "网页文本", text, "plain-text")
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise WebContentError("unsupported_content", "该网址没有返回可导入的 HTML 或文本正文。")
    try:
        decoded = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        soup = BeautifulSoup(body, "html.parser")
    else:
        soup = BeautifulSoup(decoded, "html.parser")
    title = _title(soup, url)
    candidate = soup.select_one("#js_content")
    extractor = "wechat" if candidate is not None else "trafilatura"
    if candidate is None:
        candidates = soup.select("article, main, [role=main]")
        candidate = max(
            candidates, key=lambda item: len(item.get_text(" ", strip=True)), default=None,
        )
    has_article = candidate is not None and _meaningful_text(_article_text(candidate))
    path = urlparse(url).path.lower()
    gate_title = re.fullmatch(
        r"(?:环境异常|安全验证|访问验证|验证码|访问受限|"
        r"just a moment\.{0,3}|access denied|security check|"
        r"verify (?:you are human|your identity))(?:\s*[-|—].*)?",
        title, re.IGNORECASE,
    )
    challenge = soup.select_one(
        "#challenge-form, #challenge-running, #captcha, .captcha-box, "
        'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare"]'
    )
    if (
        "/wappoc_appmsgcaptcha" in path
        or gate_title is not None
        or (not has_article and challenge is not None)
    ):
        raise WebContentError(
            "verification_required",
            "网站要求安全验证，未导入验证页面。请使用浏览器完成验证后再导入。",
        )
    login_title = re.fullmatch(
        r"(?:登录|登陆|扫码登录|微信登录|账号登录|用户登录|会员登录|"
        r"sign[ -]?in|log[ -]?in|login)(?:\s*[-|—].*)?", title, re.IGNORECASE,
    )
    login_path = re.match(
        r"^/(?:login|log-in|signin|sign-in|accounts/login|auth/login|oauth/authorize)(?:/|$)",
        path,
    )
    password_field = (
        candidate.select_one('input[type="password"]') if candidate is not None
        else soup.select_one('input[type="password"]')
    )
    if login_title or login_path or password_field is not None:
        raise WebContentError(
            "login_required", "网站要求登录，未导入登录页面。请使用浏览器登录后再导入。",
        )
    if not has_article and any(message in title for message in (
        "内容已被删除", "内容无法查看", "内容因违规", "页面不存在", "Page not found",
    )):
        raise WebContentError("content_unavailable", "文章已删除或不可访问，未导入。")
    content = BeautifulSoup(str(candidate if has_article else soup), "html.parser")
    for element in content.select(
        "script, style, noscript, nav, aside, footer, form, input, button, "
        ".comments, #comments, .advertisement, [role=navigation]"
    ):
        element.decompose()
    for link in content.find_all("a"):
        href = link.get("href")
        if isinstance(href, str):
            absolute = urljoin(url, href)
            if urlparse(absolute).scheme in {"http", "https"}:
                link["href"] = absolute
            else:
                link.attrs.pop("href", None)
    markdown = extract(
        str(content), url=public_article_url(url) or None, output_format="markdown",
        include_comments=False, include_tables=True, include_links=True,
        favor_precision=True,
    )
    if not markdown and has_article:
        markdown = content.get_text("\n", strip=True)
    body_text = re.sub(r"(?m)^#{1,6}\s+.*$", "", markdown or "")
    body_text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", body_text)
    if not markdown or not _meaningful_text(body_text):
        raise WebContentError(
            "empty_content",
            "没有提取到有效文章正文，未导入。若内容由脚本加载，请使用浏览器导入。",
        )
    if not markdown.startswith("# "):
        markdown = f"# {title}\n\n{markdown}"
    return WebArticle(title, markdown.strip(), extractor)
