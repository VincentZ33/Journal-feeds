#!/usr/bin/env python3
"""
为多本期刊生成带完整摘要的 RSS, 每本输出 docs/<key>.xml。

期刊列表在 journals.json, 每条有一个 type:
  - "ajcass"   : 社科院期刊平台 (xxx.ajcass.com), 抓首页当期目录 + 文章页
  - "magtech"  : 玛格泰克期刊系统 (.../CN/<ISSN>/home.shtml), 抓当期 + 网络首发
  - "crossref" : 用 Crossref 接口按 ISSN 取最新文章和摘要 (多数英文期刊)

用法:
    pip install requests beautifulsoup4
    python3 feeds.py                 # 全部期刊
    python3 feeds.py shxyj smr       # 只跑指定的 key
    python3 feeds.py --debug smr     # 打印解析结果

某一本失败不会影响其他期刊, 也不会覆盖它原来的 feed 文件。
"""
import argparse
import hashlib
import html as htmllib
import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin
from xml.sax.saxutils import escape

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CST = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130 Safari/537.36")

session = requests.Session()
session.headers.update({"User-Agent": UA})
DEBUG = False


def log(*a):
    print(*a, file=sys.stderr)


def get(url, retries=3, **kw):
    for i in range(retries):
        try:
            r = session.get(url, timeout=30, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if i == retries - 1:
                raise
            log(f"  重试 {url}: {e}")
            time.sleep(3)


def norm(s):
    s = (s or "").replace("&nbsp;", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- ajcass

ISSUE_RE = re.compile(r"Magazine/\?Year=(\d{4})&(?:amp;)?Issue=(\d+)", re.I)
HOME_TOC_RE = re.compile(r'<a\s+id="a\s*(\d+)"\s+href="/Magazine/Show\?id=\1"', re.I)
LABELS = {
    "authors": ["作者", "作 者"],
    "abstract": ["摘要", "摘 要", "内容摘要", "中文摘要"],
    "keywords": ["关键词", "关 键 词", "中文关键词"],
}


def extract_field(soup, text, names, min_len=1):
    """按"标签: 值"版式找字段。min_len 用来排除"摘要"这类导航按钮误匹配。"""
    clean = lambda s: re.sub(r"[\s：:]", "", s or "")
    targets = {clean(n) for n in names}
    for tag in soup.find_all(["td", "th", "span", "b", "strong", "label", "div", "dt", "p"]):
        own = tag.get_text(" ", strip=True)
        if clean(own) in targets:
            sib = tag.find_next_sibling()
            if sib is not None:
                val = norm(sib.get_text(" ", strip=True))
                if len(val) >= min_len:
                    return val
        for n in names:
            m = re.match(rf"^\s*{re.escape(n)}\s*[：:]\s*(.+)$", own, re.S)
            if m and len(tag.find_all(True)) <= 2 and len(norm(m.group(1))) >= min_len:
                return norm(m.group(1))
    stop = r"(?:关\s*键\s*词|Keywords|Abstract|英文|作者简介|基金|DOI|分类号|$)"
    for n in names:
        m = re.search(rf"{re.escape(n)}\s*[：:]\s*(.+?){stop}", text, re.S)
        if m and len(norm(m.group(1))) >= min_len:
            return norm(m.group(1))
    return ""


def fetch_ajcass(cfg):
    base = cfg["url"].rstrip("/") + "/"
    r = get(base)
    r.encoding = r.apparent_encoding or "utf-8"
    home = r.text
    issues = sorted({(int(y), int(i)) for y, i in ISSUE_RE.findall(home)}, reverse=True)
    if not issues:
        raise RuntimeError("首页没有找到期次链接")
    year, issue = issues[0]
    ids = list(dict.fromkeys(HOME_TOC_RE.findall(home)))
    log(f"  {year}年第{issue}期: {len(ids)} 个条目")
    # 双月刊第 N 期 ≈ (2N-1) 月; 月刊可在 journals.json 设 "monthly": true
    month = issue if cfg.get("monthly") else min(2 * issue - 1, 12)
    issue_dt = datetime(year, month, 15, 8, 0, tzinfo=CST)

    items = []
    for idx, aid in enumerate(ids):
        url = urljoin(base, f"Magazine/Show?id={aid}")
        try:
            r = get(url)
        except Exception as e:
            log(f"  跳过 id={aid}: {e}")
            continue
        r.encoding = r.apparent_encoding or "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        title = norm(soup.title.get_text()) if soup.title else ""
        title = re.sub(rf"\s*[-_|—]\s*{re.escape(cfg['name'])}\s*$", "", title)
        text = soup.get_text("\n")
        a = {
            "guid": f"{cfg['key']}-{aid}",
            "url": url,
            "title": title,
            "authors": extract_field(soup, text, LABELS["authors"]),
            "abstract": extract_field(soup, text, LABELS["abstract"]),
            "keywords": extract_field(soup, text, LABELS["keywords"]),
            "note": f"{cfg['name']} {year}年第{issue}期",
            # 同一期按目录顺序递减, 让阅读器保持目录顺序
            "date": issue_dt - timedelta(minutes=idx),
        }
        if DEBUG:
            log(f"  [{aid}] {a['title']} | 作者={a['authors']!r} | 摘要前40字={a['abstract'][:40]!r}")
        if not a["abstract"] or "目录" in a["title"]:
            continue
        items.append(a)
        time.sleep(1)
    return items


# ---------------------------------------------------------------- magtech

# 玛格泰克期刊系统 (网址形如 .../CN/1004-8804/home.shtml), 很多中文期刊在用
MAG_ART_RE = re.compile(
    r'href="((?:https?://[^"/]+)?/CN/(?:Y\d{4}/V\d+/I\d+/\d+|10\.\d{4,}/[^"#?]+|abstract/abstract\d+\.shtml))"',
    re.I)
MAG_SKIP = re.compile(r"(目录|目次|征稿|启事|稿约|勘误|更正|编后|索引)")


def parse_date(s):
    m = re.search(r"(\d{4})[-/年.](\d{1,2})(?:[-/月.](\d{1,2}))?", s or "")
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3) or 1)
    try:
        return datetime(y, mo, d, 8, 0, tzinfo=CST)
    except ValueError:
        return None


def fetch_magtech(cfg):
    home = cfg["url"]                       # .../CN/1004-8804/home.shtml
    root = re.sub(r"/CN/.*$", "", home)     # https://www.society.shu.edu.cn
    pages = [urljoin(home, "current.shtml")]
    if cfg.get("online_first", True):
        pages.append(root + "/CN/article/showAsap.do")
    links = []
    for p in pages:
        try:
            r = get(p)
        except Exception as e:
            log(f"  无法打开 {p}: {e}")
            continue
        found = [urljoin(root, h) for h in MAG_ART_RE.findall(r.text)]
        log(f"  {p.rsplit('/', 1)[-1]}: {len(set(found))} 篇")
        links += found
    links = list(dict.fromkeys(links))

    items, seen_titles = [], set()
    for idx, url in enumerate(links):
        try:
            r = get(url)
        except Exception as e:
            log(f"  跳过 {url}: {e}")
            continue
        r.encoding = r.apparent_encoding or "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        meta = {}
        for m in soup.find_all("meta"):
            k = (m.get("name") or m.get("property") or "").strip()
            if k and m.get("content") and k not in meta:
                meta[k] = m["content"].strip()
        title = norm(meta.get("citation_title") or meta.get("DC.Title")
                     or (soup.title.get_text() if soup.title else ""))
        if not title or MAG_SKIP.search(title) or title in seen_titles:
            continue
        seen_titles.add(title)
        text = soup.get_text("\n")
        # 摘要: 先看 meta, 不够长再按版面找; 少于 60 字视为没找到
        abstract = ""
        for k in ("citation_abstract", "DC.Description", "description"):
            v = norm(htmllib.unescape(meta.get(k, "")))
            if len(v) >= 60 and re.search(r"[一-鿿]", v):
                abstract = v
                break
        if not abstract:
            abstract = extract_field(soup, text, ["摘要", "摘 要", "内容摘要"], min_len=60)
        authors = norm(meta.get("citation_authors") or "") or \
            "、".join(m["content"] for m in soup.find_all("meta", attrs={"name": "citation_author"}) if m.get("content"))
        keywords = norm(meta.get("citation_keywords") or meta.get("DC.Keywords") or meta.get("keywords") or "").strip(",，;； ")
        doi = norm(meta.get("citation_doi") or "")
        m = re.search(r"/Y(\d{4})/V\d+/I(\d+)/", url)
        where = f"{m.group(1)}年第{m.group(2)}期" if m else "网络首发"
        date = (parse_date(meta.get("citation_online_date")) or parse_date(meta.get("citation_publication_date"))
                or parse_date(meta.get("citation_date")) or parse_date(meta.get("DC.Date"))
                or datetime.now(CST))
        a = {
            # 用标题做 guid: 网络首发文章正式刊出后换了网址, 也不会重复出现
            "guid": f"{cfg['key']}-" + hashlib.md5(title.encode()).hexdigest()[:12],
            "url": url,
            "title": title,
            "authors": authors.replace(";", "、").replace("；", "、"),
            "abstract": abstract,
            "keywords": keywords,
            "doi": doi,
            "note": f"{cfg['name']} {where}",
            "date": date - timedelta(minutes=idx),
        }
        if DEBUG:
            log(f"  [{where}] {title[:30]} | 作者={a['authors']!r} | 关键词={keywords[:30]!r}"
                f" | 摘要{len(abstract)}字: {abstract[:30]!r}…{abstract[-15:]!r}")
        if not abstract and cfg.get("require_abstract", True):
            continue
        items.append(a)
        time.sleep(1)
    return items


# ---------------------------------------------------------------- crossref

def jats_to_text(s):
    s = re.sub(r"<jats:title>.*?</jats:title>", "", s or "", flags=re.S | re.I)  # 去掉 "Abstract" 小标题
    s = re.sub(r"</jats:p>\s*<jats:p>", "\n\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    paras = [norm(htmllib.unescape(p)) for p in s.split("\n\n")]
    return [p for p in paras if p]


def cr_date(m):
    for k in ("published-online", "published-print", "published", "created"):
        parts = (m.get(k) or {}).get("date-parts") or [[]]
        p = parts[0]
        if p and p[0]:
            y, mo, d = (p + [1, 1])[:3]
            return datetime(y, mo or 1, d or 1, 8, 0, tzinfo=CST)
    return datetime.now(CST)


def fetch_crossref(cfg):
    params = {
        "sort": "created",          # 按 DOI 注册时间, 最接近"刚上线"
        "order": "desc",
        "rows": cfg.get("rows", 30),
        "select": "DOI,title,author,abstract,created,published,published-online,"
                  "published-print,URL,type,volume,issue",
    }
    if cfg.get("mailto"):
        params["mailto"] = cfg["mailto"]   # Crossref "polite pool", 更稳定
    r = get(f"https://api.crossref.org/journals/{cfg['issn']}/works", params=params)
    works = r.json()["message"]["items"]
    log(f"  Crossref 返回 {len(works)} 条")

    skip_title = re.compile(cfg.get("skip_title_regex",
                            r"^(Erratum|Corrigendum|Correction|Editorial|Book Review|Index|Issue Information|Front Matter|Back Matter|Contributors|Table of Contents)"), re.I)
    items = []
    for m in works:
        if m.get("type") not in (None, "journal-article"):
            continue
        raw_title = " ".join(m.get("title") or [])
        # 书评的标题是整本书名, Crossref 里通常整个包在 <i> 里
        if cfg.get("skip_book_reviews", True) and re.match(r"^\s*<i>.*</i>\s*$", raw_title, re.S):
            continue
        title = norm(htmllib.unescape(re.sub(r"<[^>]+>", " ", raw_title)))
        title = re.sub(r"\s+([,.:;?!)])", r"\1", title)
        if not title or skip_title.search(title):
            continue
        authors = ", ".join(
            norm(f"{a.get('given', '')} {a.get('family', '')}") or a.get("name", "")
            for a in m.get("author", [])
        )
        paras = jats_to_text(m.get("abstract", ""))
        vol, iss = m.get("volume"), m.get("issue")
        where = f"Vol. {vol}({iss})" if vol and iss else "Ahead of Print"
        a = {
            "guid": f"doi:{m['DOI'].lower()}",
            "url": f"https://doi.org/{m['DOI']}",
            "title": title,
            "authors": authors,
            "abstract_paras": paras,
            "abstract": " ".join(paras),
            "keywords": "",
            "doi": m["DOI"],
            "note": f"{cfg['name']} · {where}",
            "date": cr_date(m),
        }
        if DEBUG:
            log(f"  [{m['DOI']}] {title[:60]} | 摘要 {len(a['abstract'])} 字符")
        if not a["abstract"] and cfg.get("require_abstract", False):
            continue
        items.append(a)
    return items


# ---------------------------------------------------------------- output

def item_xml(a):
    desc = ""
    if a["authors"]:
        label = "" if a["guid"].startswith("doi:") else "<b>作者：</b>"
        desc += f"<p>{label}{escape(a['authors'])}</p>"
    paras = a.get("abstract_paras") or ([a["abstract"]] if a["abstract"] else [])
    desc += "".join(f"<p>{escape(p)}</p>" for p in paras) or "<p>(无摘要)</p>"
    if a["keywords"]:
        desc += f"<p><b>关键词：</b>{escape(a['keywords'])}</p>"
    desc += f"<p><i>{escape(a['note'])}</i></p>"
    if a.get("doi"):
        d = escape(a["doi"])
        desc += f'<p>DOI: <a href="https://doi.org/{d}">{d}</a></p>'
    return f"""    <item>
      <title>{escape(a['title'])}</title>
      <link>{escape(a['url'])}</link>
      <guid isPermaLink="false">{escape(a['guid'])}</guid>
      <pubDate>{format_datetime(a['date'])}</pubDate>
      <description><![CDATA[{desc}]]></description>
    </item>"""


def write_feed(cfg, items):
    items.sort(key=lambda a: a["date"], reverse=True)
    link = cfg.get("url") or f"https://portal.issn.org/resource/ISSN/{cfg.get('issn', '')}"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>{escape(cfg['name'])}</title>
    <link>{escape(link)}</link>
    <description>{escape(cfg['name'])} 最新文章, 含完整摘要 (非官方)</description>
    <lastBuildDate>{format_datetime(datetime.now(CST))}</lastBuildDate>
{chr(10).join(item_xml(a) for a in items)}
  </channel>
</rss>
"""
    out = ROOT / "docs" / f"{cfg['key']}.xml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(xml, encoding="utf-8")
    return out


FETCHERS = {"ajcass": fetch_ajcass, "magtech": fetch_magtech, "crossref": fetch_crossref}


def main():
    global DEBUG
    ap = argparse.ArgumentParser()
    ap.add_argument("keys", nargs="*", help="只处理这些 key")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    DEBUG = args.debug

    conf = json.loads((ROOT / "journals.json").read_text(encoding="utf-8"))
    defaults = conf.get("defaults", {})
    failed = []
    for j in conf["journals"]:
        cfg = {**defaults, **j}
        if args.keys and cfg["key"] not in args.keys:
            continue
        log(f"== {cfg['name']} ({cfg['key']}, {cfg['type']})")
        try:
            items = FETCHERS[cfg["type"]](cfg)
            if not items:
                raise RuntimeError("一篇都没解析出来")
            out = write_feed(cfg, items)
            n_abs = sum(1 for a in items if a["abstract"])
            log(f"  写入 {out.relative_to(ROOT)}: {len(items)} 篇, 其中 {n_abs} 篇有摘要")
        except Exception as e:
            log(f"  失败, 保留旧文件: {e}")
            failed.append(cfg["key"])
    if failed:
        log(f"\n失败: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
