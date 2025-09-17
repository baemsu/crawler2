# function_app.py
# ------------------------------------------------------------
# Azure Functions (Python v2) - HTTP 트리거로 TechCrunch AI 카테고리 "오늘(KST)" 기사 크롤링
# 엔드포인트: GET/POST https://baemsutest-gdcaamfshgg9ayfy.canadacentral-01.azurewebsites.net/api/ai-today?code=
# 쿼리/바디 파라미터:
#   - date: "YYYY-MM-DD" (옵션, 기본=오늘 KST)
#   - limit: int (옵션, 기본=40, 범위 1~80)
#   - sleep: float 초 (옵션, 기본=0.7, 범위 0~2)
#   - category_url: str (옵션, 기본=TechCrunch AI 카테고리)
# 응답: { date_kst, count, items: [{url,title,published_utc,published_kst,body}] }
# ------------------------------------------------------------

import os
import json
import re
import time
import logging
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo  # Python 3.9+

import requests
from bs4 import BeautifulSoup
import azure.functions as func

app = func.FunctionApp()
logger = logging.getLogger("func")

# 기본 대상 카테고리
CATEGORY_URL = "https://techcrunch.com/category/artificial-intelligence/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

KST = ZoneInfo("Asia/Seoul")


def fetch(url: str) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r


def get_article_links(category_url: str = CATEGORY_URL, limit: int = 50):
    """
    카테고리 페이지에서 기사 링크를 최대 limit개까지 수집.
    """
    html = fetch(category_url).text
    soup = BeautifulSoup(html, "html.parser")
    links = set()

    # 1) h3 내부의 앵커
    for h3 in soup.find_all("h3"):
        a = h3.find("a", href=True)
        if a and is_article_url(a["href"]):
            links.add(normalize_link(a["href"]))

    # 2) 보강: 모든 a 중 연-월 패턴 포함 URL
    if len(links) < limit:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if is_article_url(href):
                links.add(normalize_link(href))
            if len(links) >= limit:
                break

    return list(links)[:limit]


def is_article_url(href: str) -> bool:
    """
    TechCrunch 기사 URL은 일반적으로 /YYYY/MM/ 형태를 가짐.
    """
    try:
        u = urlparse(href)
        path = u.path
        return (
            ("techcrunch.com" in u.netloc or u.netloc == "")
            and re.search(r"/20\d{2}/\d{2}/", path) is not None
        )
    except Exception:
        return False


def normalize_link(href: str) -> str:
    return urljoin(CATEGORY_URL, href)


def parse_article(url: str):
    """
    기사 페이지에서 제목, 본문, 발행일시(UTC/KST 변환)를 파싱.
    발행일 우선순위:
      1) <meta property="article:published_time" content="ISO8601">
      2) JSON-LD(NewsArticle/BlogPosting) 내 datePublished
      3) <time datetime="...">
      4) 페이지 텍스트에서 월/일/연도 패턴
    본문 우선순위:
      1) JSON-LD의 articleBody
      2) <article> 내 <p>들 연결
    """
    res = fetch(url)
    soup = BeautifulSoup(res.text, "html.parser")

    # 제목
    title = soup.find("h1")
    title_text = title.get_text(strip=True) if title else ""

    # 발행일
    published_dt = (
        get_meta_datetime(soup, "article:published_time")
        or get_ldjson_datetime(soup)
        or get_time_tag_datetime(soup)
        or get_text_datetime_fallback(soup)
    )

    # 본문
    body_text = get_ldjson_article_body(soup) or extract_paragraphs(soup)

    return {
        "url": url,
        "title": title_text,
        "published_utc": published_dt.astimezone(timezone.utc).isoformat()
        if published_dt
        else None,
        "published_kst": published_dt.astimezone(KST).isoformat()
        if published_dt
        else None,
        "body": (body_text or "").strip(),
    }


def get_meta_datetime(soup: BeautifulSoup, prop: str):
    tag = soup.find("meta", attrs={"property": prop}) or soup.find(
        "meta", attrs={"name": prop}
    )
    if tag and tag.get("content"):
        try:
            return datetime.fromisoformat(tag["content"].replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def get_ldjson_datetime(soup: BeautifulSoup):
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
            candidates = data if isinstance(data, list) else [data]
            for obj in candidates:
                if isinstance(obj, dict) and obj.get("@type") in {
                    "NewsArticle",
                    "Article",
                    "BlogPosting",
                }:
                    dp = obj.get("datePublished") or obj.get("dateCreated")
                    if dp:
                        return datetime.fromisoformat(dp.replace("Z", "+00:00"))
        except Exception:
            continue
    return None


def get_time_tag_datetime(soup: BeautifulSoup):
    t = soup.find("time")
    if t and t.get("datetime"):
        try:
            return datetime.fromisoformat(t["datetime"].replace("Z", "+00:00"))
        except Exception:
            pass
    # 화면표시 텍스트에 월 일, 연도 패턴이 있을 수 있음
    if t and t.get_text(strip=True):
        return parse_human_datetime(t.get_text(" ", strip=True))
    return None


def get_text_datetime_fallback(soup: BeautifulSoup):
    text = soup.get_text(" ", strip=True)
    return parse_human_datetime(text)


def parse_human_datetime(text: str):
    # 예: "September 10, 2025" 또는 "10:10 PM PDT · September 10, 2025"
    m = re.search(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}",
        text,
    )
    if m:
        try:
            d = datetime.strptime(m.group(0), "%B %d, %Y")
            # 시각 정보가 없으면 UTC 자정으로 가정
            return d.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def get_ldjson_article_body(soup: BeautifulSoup):
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
            candidates = data if isinstance(data, list) else [data]
            for obj in candidates:
                if isinstance(obj, dict) and obj.get("@type") in {
                    "NewsArticle",
                    "Article",
                    "BlogPosting",
                }:
                    body = obj.get("articleBody")
                    if body:
                        return body
        except Exception:
            continue
    return None


def extract_paragraphs(soup: BeautifulSoup):
    # 기사 본문 컨테이너 추정: <article> 내부 p 수집(aside/figure/nav 등 제외)
    article = soup.find("article") or soup
    paragraphs = []
    for p in article.find_all("p"):
        bad = p.find_parent(["aside", "figcaption", "nav", "footer"])
        if bad:
            continue
        txt = p.get_text(" ", strip=True)
        if len(txt) >= 2:
            paragraphs.append(txt)
    return "\n\n".join(paragraphs)


def is_today_kst(dt: datetime, today_kst: datetime):
    if not dt:
        return False
    return dt.astimezone(KST).date() == today_kst.date()


def crawl_today(
    category_url: str = CATEGORY_URL,
    today_kst: datetime | None = None,
    limit: int = 40,
    sleep_sec: float = 0.7,
):
    if today_kst is None:
        today_kst = datetime.now(KST)
    links = get_article_links(category_url, limit=limit)
    results = []
    for url in links:
        try:
            art = parse_article(url)
            if art["published_kst"] and is_today_kst(
                datetime.fromisoformat(art["published_kst"]), today_kst
            ):
                results.append(art)
        except Exception as e:
            logger.warning("Parse failed for %s: %s", url, e)
        time.sleep(sleep_sec)  # 예의상 천천히
    return results


@app.route(route="ai-today", methods=["GET", "POST"], auth_level=func.AuthLevel.FUNCTION)
def ai_today(req: func.HttpRequest) -> func.HttpResponse:
    """
    TechCrunch AI 카테고리에서 '오늘자(KST)' 기사만 크롤링하여 JSON으로 반환.
    """
    try:
        # --- 파라미터 수집 (쿼리 > 바디)
        qs = req.params
        date_str = qs.get("date")
        limit_str = qs.get("limit")
        sleep_str = qs.get("sleep")
        category_url = qs.get("category_url") or CATEGORY_URL

        if req.method == "POST":
            try:
                body = req.get_json()
            except ValueError:
                body = {}
            date_str = body.get("date", date_str)
            limit_str = str(body.get("limit")) if "limit" in body else limit_str
            sleep_str = str(body.get("sleep")) if "sleep" in body else sleep_str
            category_url = body.get("category_url", category_url)

        # --- date 파싱
        today_kst = None
        if date_str:
            try:
                yyyy, mm, dd = map(int, date_str.split("-"))
                today_kst = datetime(yyyy, mm, dd, tzinfo=KST)
            except Exception:
                return func.HttpResponse(
                    json.dumps({"error": "invalid date format, use YYYY-MM-DD"}),
                    status_code=400,
                    mimetype="application/json",
                )

        # --- limit/sleep 파싱 및 범위 제한
        limit = 40
        if limit_str:
            try:
                limit = max(1, min(int(limit_str), 80))
            except Exception:
                pass

        sleep_sec = 0.7
        if sleep_str:
            try:
                sleep_sec = float(sleep_str)
                if sleep_sec < 0:
                    sleep_sec = 0.0
                if sleep_sec > 2:
                    sleep_sec = 2.0
            except Exception:
                pass

        items = crawl_today(
            category_url=category_url, today_kst=today_kst, limit=limit, sleep_sec=sleep_sec
        )

        out = {
            "date_kst": (today_kst or datetime.now(KST)).strftime("%Y-%m-%d"),
            "count": len(items),
            "items": items,
        }
        return func.HttpResponse(
            json.dumps(out, ensure_ascii=False),
            status_code=200,
            mimetype="application/json",
        )

    except Exception as e:
        logger.exception("Unhandled error in ai-today")
        return func.HttpResponse(
            json.dumps({"error": "internal_error", "detail": str(e)}),
            status_code=500,
            mimetype="application/json",
        )
