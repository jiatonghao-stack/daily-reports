#!/usr/bin/env python3
"""
每日国内外新闻早报自动搜集与总结程序
运行环境：GitHub Actions 云端定时运行（电脑关机照常执行）/ 本地 Python 3.9+
功能：
  1. 分别并行抓取过去 24~36 小时「国内新闻」与「国外新闻」权威公开信源
  2. 去重并按热度/时效筛选国内候选池与国际候选池
  3. 分两次调用大模型 API（避免单次输出超长截断，确保国内 Top 20 与国外 Top 20 均完整详实），
     生成重磅大事件速览、重点标注（🔥【重点】）、国内 Top 20、国外 Top 20 及局势风向研判，附真实原文链接
  4. 输出到子目录：`国内外新闻/YYYY-MM-DD-国内外早报.md` 与 `国内外新闻/最新早报.md`
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import email.utils
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

BEIJING_TZ = dt.timezone(dt.timedelta(hours=8))
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 国内新闻源：(来源名称, URL, 分类, 基础权重)
DOMESTIC_SOURCES: list[tuple[str, str, str, int]] = [
    (
        "Google 新闻中国区头条",
        "https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
        "国内要闻",
        34,
    ),
    (
        "国内时政与宏观经济 24h",
        "https://news.google.com/rss/search?q=(%E4%B8%AD%E5%9B%BD+OR+%E5%9B%BD%E5%8A%A1%E9%99%A2+OR+%E5%A4%AE%E8%A1%8C+OR+%E5%8F%91%E6%94%B9%E5%A7%94+OR+%E8%B4%A2%E6%94%BF%E9%83%A8+OR+%E7%BB%8F%E6%B5%8E+OR+%E6%94%BF%E7%AD%96)+when:1d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
        "时政宏观",
        32,
    ),
    (
        "国内财经商业与市场 24h",
        "https://news.google.com/rss/search?q=(A%E8%82%A1+OR+%E6%B8%AF%E8%82%A1+OR+%E8%B4%A2%E6%8A%A5+OR+%E6%88%BF%E5%9C%B0%E4%BA%A7+OR+%E6%B6%88%E8%B4%B9+OR+%E6%96%B0%E8%83%BD%E6%BA%90+OR+%E5%88%B6%E9%80%A0%E4%B8%9A)+when:1d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
        "财经商业",
        30,
    ),
    (
        "国内社会民生与热点 24h",
        "https://news.google.com/rss/search?q=(%E9%80%9A%E6%8A%A5+OR+%E6%B0%91%E7%94%9F+OR+%E6%95%99%E8%82%B2+OR+%E5%8C%BB%E7%96%97+OR+%E4%BA%A4%E9%80%9A+OR+%E7%A4%BE%E4%BC%9A+OR+%E8%B0%83%E6%9F%A5)+when:1d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
        "社会民生",
        28,
    ),
    (
        "中国新闻网重点新闻",
        "https://www.chinanews.com.cn/rss/importnews.xml",
        "权威要闻",
        31,
    ),
    (
        "中国新闻网财经频道",
        "https://www.chinanews.com.cn/rss/finance.xml",
        "国内财经",
        27,
    ),
    (
        "外媒涉华政经焦点 24h",
        "https://news.google.com/rss/search?q=(China+economy+OR+Beijing+policy+OR+Chinese+companies+OR+China+trade)+when:1d&hl=en-US&gl=US&ceid=US:en",
        "涉华观察",
        26,
    ),
]

# 国外新闻源：(来源名称, URL, 分类, 基础权重)
INTERNATIONAL_SOURCES: list[tuple[str, str, str, int]] = [
    ("BBC World News", "https://feeds.bbci.co.uk/news/world/rss.xml", "国际要闻", 35),
    ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml", "全球商业", 30),
    ("New York Times World", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "全球时政", 34),
    ("New York Times Top Stories", "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml", "美国与全球", 31),
    ("The Guardian World", "https://www.theguardian.com/world/rss", "国际深度", 30),
    ("Al Jazeera Global", "https://www.aljazeera.com/xml/rss/all.xml", "地缘局势", 29),
    (
        "CNBC World & Economy",
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362",
        "全球金融市场",
        31,
    ),
    ("NPR World News", "https://feeds.npr.org/1004/rss.xml", "国际动态", 27),
    (
        "Google News Global World 24h",
        "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-US&gl=US&ceid=US:en",
        "全球热榜",
        33,
    ),
    (
        "Google 新闻国际频道中文聚合",
        "https://news.google.com/rss/headlines/section/topic/WORLD?hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
        "国际中文焦点",
        32,
    ),
]


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def _http_get(url: str, timeout: int = 12) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            return resp.read()
    except ssl.SSLError:
        unverified = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=timeout, context=unverified) as resp:
            return resp.read()


def _clean_text(raw: str | None, max_len: int = 280) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        return text[: max_len - 1].rstrip() + "…"
    return text


def _parse_datetime(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(BEIJING_TZ)
    except Exception:
        pass
    iso_candidate = raw.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(iso_candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(BEIJING_TZ)
    except Exception:
        return None


def fetch_rss_source(source_tuple: tuple[str, str, str, int]) -> list[dict[str, Any]]:
    source_name, url, category, base_weight = source_tuple
    items: list[dict[str, Any]] = []
    try:
        raw_bytes = _http_get(url, timeout=12)
        root = ET.fromstring(raw_bytes)
    except Exception as exc:
        print(f"[WARN] RSS 抓取跳过 ({source_name}): {exc}", file=sys.stderr)
        return items

    for idx, elem in enumerate(root.findall(".//item")[:28]):
        title = _clean_text(elem.findtext("title"), 180)
        link = (elem.findtext("link") or "").strip()
        desc = _clean_text(elem.findtext("description"), 280)
        pub = _parse_datetime(elem.findtext("pubDate") or elem.findtext("{http://purl.org/dc/elements/1.1/}date"))
        if not title or not link:
            continue
        rank_bonus = max(0, 10 - idx // 3)
        items.append(
            {
                "title": title,
                "url": link,
                "summary": desc,
                "source": source_name,
                "category": category,
                "published_at": pub,
                "score": base_weight + rank_bonus,
            }
        )

    atom_ns = "{http://www.w3.org/2005/Atom}"
    for idx, elem in enumerate(root.findall(f".//{atom_ns}entry")[:28]):
        title = _clean_text(elem.findtext(f"{atom_ns}title"), 180)
        link = ""
        for link_el in elem.findall(f"{atom_ns}link"):
            rel = link_el.attrib.get("rel", "alternate")
            href = link_el.attrib.get("href", "").strip()
            if href and rel == "alternate":
                link = href
                break
            if href and not link:
                link = href
        desc = _clean_text(
            elem.findtext(f"{atom_ns}summary") or elem.findtext(f"{atom_ns}content"),
            280,
        )
        pub = _parse_datetime(elem.findtext(f"{atom_ns}published") or elem.findtext(f"{atom_ns}updated"))
        if not title or not link:
            continue
        rank_bonus = max(0, 10 - idx // 3)
        items.append(
            {
                "title": title,
                "url": link,
                "summary": desc,
                "source": source_name,
                "category": category,
                "published_at": pub,
                "score": base_weight + rank_bonus,
            }
        )
    return items


def _normalize_title_key(title: str) -> str:
    cleaned = re.sub(r"\s*[-|–—]\s*[^-|–—]{2,25}$", "", title.lower())
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "", cleaned)
    return cleaned[:42]


def collect_pool(sources: list[tuple[str, str, str, int]], now_bj: dt.datetime, limit: int = 65) -> list[dict[str, Any]]:
    all_raw: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_rss_source, src) for src in sources]
        for fut in concurrent.futures.as_completed(futures):
            try:
                all_raw.extend(fut.result())
            except Exception as exc:
                print(f"[WARN] 抓取子任务异常: {exc}", file=sys.stderr)

    cutoff_strict = now_bj - dt.timedelta(hours=36)
    cutoff_relaxed = now_bj - dt.timedelta(hours=72)

    recent: list[dict[str, Any]] = []
    backup: list[dict[str, Any]] = []
    for item in all_raw:
        pub = item.get("published_at")
        if pub is None or pub >= cutoff_strict:
            if pub is not None and pub >= now_bj - dt.timedelta(hours=24):
                item["score"] += 8
            recent.append(item)
        elif pub >= cutoff_relaxed:
            backup.append(item)

    merged = recent if len(recent) >= 28 else (recent + backup)
    merged.sort(key=lambda x: x["score"], reverse=True)

    deduped: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    for item in merged:
        url = item["url"]
        tkey = _normalize_title_key(item["title"])
        if url in seen_urls or (tkey and tkey in seen_titles):
            continue
        seen_urls.add(url)
        if tkey:
            seen_titles.add(tkey)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return deduped


def _format_candidates(candidates: list[dict[str, Any]]) -> str:
    lines = []
    for idx, c in enumerate(candidates, 1):
        pub_str = c["published_at"].strftime("%Y-%m-%d %H:%M") if c.get("published_at") else "过去24h内"
        lines.append(
            f"[{idx}] 标题: {c['title']}\n"
            f"    来源: {c['source']} | 分类: {c['category']} | 时间: {pub_str} | 权重: {c['score']}\n"
            f"    摘要: {c['summary']}\n"
            f"    原文链接: {c['url']}"
        )
    return "\n\n".join(lines)


def build_domestic_prompt(candidates: list[dict[str, Any]], report_date: str, yesterday_date: str) -> str:
    materials = _format_candidates(candidates)
    return f"""你是一位资深中文时政与财经总编辑。今天是北京时间 {report_date} 早上，请基于下方抓取到的前一天（{yesterday_date} 及过去 24 小时内）【国内新闻】真实候选池，撰写今日早报的**国内新闻全部分析章节**。

### 核心要求（必须严格满足）：
1. **聚焦中国国内核心大事**：涵盖宏观政策、经济金融、重大产业商业动态、社会民生焦点、基础设施与区域大事（注：用户已有专门的《AI 新闻早报》，除非某件 AI 事件属于国家级重大政策或轰动全社会的宏观财经大事件，否则常规纯技术类 AI 资讯请尽量让位于更广泛的时政、经济、商业与社会大事）。
2. **合并重复报道，精选最热、最重要的 20 条国内新闻（Top 20）**。
3. **醒目标注重点**：在你判断最重要、对普通人/商业决策影响最大的条目前加上 `🔥【重点】` 标记，并单独列出 3~5 条“国内重点必读标注”说明为什么重要。
4. **保留真实原文链接**：每条末尾统一用 `[查看原文](真实URL)`，**严禁编造或篡改候选池以外的 URL**。
5. **直接输出以下 Markdown 章节正文（不要加最外层代码块，不要加报告大标题）**：

## 🇨🇳 第一部分：国内新闻全览（{yesterday_date} ~ {report_date} 早间）

### 1.1 昨日国内重磅大事件速览
（用 3~4 条高度凝练的要点概括昨天国内最核心的大事，让人 30 秒掌握国内全局。）

### 1.2 ⭐ 国内重点必读标注（主编高优关注）
（挑出 3~5 条最关键的国内大事，按“发生了什么 + 为什么值得重点关注 + `[查看原文](URL)`”格式呈现。）

### 1.3 昨日国内最热新闻 Top 20 全景榜
（严格按 1 到 20 编号输出完整 20 条国内新闻。重要条目前加 `🔥【重点】`。每条包含：**中文标题**、2~3 句核心事实与关键细节、一句话简评，以及 `[查看原文](URL)`。）

### 1.4 国内政策、经济与社会风向洞察
（基于昨日国内新闻事实，提炼 2~3 条清晰的宏观政策信号、经济产业风向或民生趋势研判。）

---
### 国内候选资讯池（共 {len(candidates)} 条真实抓取数据）：
{materials}
"""


def build_international_prompt(candidates: list[dict[str, Any]], report_date: str, yesterday_date: str) -> str:
    materials = _format_candidates(candidates)
    return f"""你是一位资深国际问题与全球市场总编辑。今天是北京时间 {report_date} 早上，请基于下方抓取到的前一天（{yesterday_date} 及过去 24 小时内）【国外/国际新闻】真实候选池，用流畅专业的**中文**撰写今日早报的**国外新闻全部分析章节**。

### 核心要求（必须严格满足）：
1. **全面覆盖全球重大事件**：涵盖大国博弈、国际地缘冲突与外交谈判、欧美日亚太政局变动、美联储及各国央行政策、全球金融与大宗商品市场震动、跨国巨头重磅商业事件及海外重大突发社会新闻（常规纯 AI 技术细节尽量少选，聚焦国际政经、商业与社会全局大事）。
2. **全部翻译精编为地道中文**，合并多家外媒对同一事件的报道，**精选最热、最重要的 20 条国外新闻（Top 20）**。
3. **醒目标注重点**：在你判断影响全球局势或市场走向的重大条目前加上 `🔥【重点】` 标记，并单独列出 3~5 条“国外重点必读标注”说明为什么重要。
4. **保留真实原文链接**：每条末尾统一用 `[查看原文](真实URL)`，**严禁编造或篡改候选池以外的 URL**。
5. **直接输出以下 Markdown 章节正文（不要加最外层代码块，不要加报告大标题）**：

## 🌐 第二部分：国外新闻全览（{yesterday_date} ~ {report_date} 早间）

### 2.1 昨日国际重磅大事件速览
（用 3~4 条高度凝练的要点概括昨天国际舞台最震动的大事件，让人 30 秒掌握全球风云。）

### 2.2 ⭐ 国外重点必读标注（主编高优关注）
（挑出 3~5 条最关键的国际大事，按“发生了什么 + 为什么值得重点关注 + `[查看原文](URL)`”格式呈现。）

### 2.3 昨日国外最热新闻 Top 20 全景榜
（严格按 1 到 20 编号输出完整 20 条国外新闻。重要条目前加 `🔥【重点】`。每条包含：**中文精编标题**、2~3 句核心事实背景提炼、一句话全球/对华影响点评，以及 `[查看原文](URL)`。）

### 2.4 国际局势与全球市场后续走向研判
（基于昨日国际事件信号，提炼 2~3 条关于国际地缘格局、贸易政策或全球资本市场的后续演进研判。）

---
### 国外候选资讯池（共 {len(candidates)} 条真实抓取数据）：
{materials}
"""


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int = 150) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req_headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    req_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except ssl.SSLError:
        unverified = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=timeout, context=unverified) as resp:
            return json.loads(resp.read().decode("utf-8"))


def _discover_gemini_models(api_key: str) -> list[str]:
    preferred_model = os.environ.get("GEMINI_MODEL", "").strip()
    models_to_try = [m for m in [preferred_model, "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash"] if m]
    try:
        req = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"User-Agent": USER_AGENT, "x-goog-api-key": api_key},
        )
        with urllib.request.urlopen(req, timeout=15, context=_ssl_context()) as resp:
            catalog = json.loads(resp.read().decode("utf-8")).get("models", [])
        discovered = [
            m["name"].split("/")[-1]
            for m in catalog
            if "generateContent" in m.get("supportedGenerationMethods", [])
            and "flash" in m.get("name", "").lower()
            and "tts" not in m.get("name", "").lower()
            and "image" not in m.get("name", "").lower()
        ]
        for dm in reversed(discovered):
            if dm not in models_to_try:
                models_to_try.append(dm)
    except Exception as exc:
        print(f"[WARN] 模型列表自动探测跳过: {exc}", file=sys.stderr)
    return models_to_try


def call_gemini(prompt: str, api_key: str, models_to_try: list[str]) -> str:
    headers = {"x-goog-api-key": api_key}
    last_err: Exception | None = None
    for model in models_to_try:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{urllib.parse.quote(model)}:generateContent"
        )
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 32768,
                "thinkingConfig": {"thinkingBudget": 1024},
            },
        }
        try:
            try:
                data = _post_json(url, payload, headers=headers, timeout=150)
            except urllib.error.HTTPError as http_err:
                if http_err.code == 400:
                    payload["generationConfig"] = {"temperature": 0.3, "maxOutputTokens": 16384}
                    data = _post_json(url, payload, headers=headers, timeout=150)
                else:
                    raise
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
            if text and "##" in text and "20." in text:
                print(f"[INFO] 已通过 Gemini ({model}) 完成章节生成。")
                return text
            if text:
                print(f"[WARN] Gemini ({model}) 章节输出疑似截断，尝试重试或备选...", file=sys.stderr)
        except Exception as exc:
            last_err = exc
            print(f"[WARN] Gemini 模型 {model} 调用失败，尝试备选: {exc}", file=sys.stderr)
    raise RuntimeError(f"Gemini API 调用失败: {last_err}")


def call_openai_compatible(prompt: str, api_key: str, base_url: str, model: str) -> str:
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint = f"{endpoint}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是严谨、客观、深刻的中文时政与国际新闻总编辑。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 8000,
    }
    data = _post_json(
        endpoint,
        payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=150,
    )
    return data["choices"][0]["message"]["content"].strip()


def run_llm_section(prompt: str, gemini_models: list[str] | None) -> str | None:
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    dashscope_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()

    providers: list[tuple[str, Any]] = []
    if gemini_key:
        m_list = gemini_models or _discover_gemini_models(gemini_key)
        providers.append(("Gemini", lambda: call_gemini(prompt, gemini_key, m_list)))
    if deepseek_key:
        providers.append(
            (
                "DeepSeek",
                lambda: call_openai_compatible(
                    prompt,
                    deepseek_key,
                    os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
                    os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
                ),
            )
        )
    if dashscope_key:
        providers.append(
            (
                "DashScope/Qwen",
                lambda: call_openai_compatible(
                    prompt,
                    dashscope_key,
                    "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    os.environ.get("DASHSCOPE_MODEL", "qwen-plus"),
                ),
            )
        )
    if openai_key:
        providers.append(
            (
                "OpenAI-Compatible",
                lambda: call_openai_compatible(
                    prompt,
                    openai_key,
                    os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                    os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                ),
            )
        )

    for name, fn in providers:
        try:
            return fn()
        except Exception as exc:
            print(f"[WARN] {name} 章节总结失败: {exc}", file=sys.stderr)
    return None


def _fallback_section(title: str, items: list[dict[str, Any]]) -> str:
    top20 = items[:20]
    lines = [f"## {title}", "", "### ⭐ 重点关注（Top 5）"]
    for it in top20[:5]:
        lines.append(f"- **🔥【重点】{it['title']}**（{it['source']}）：{it['summary']} [查看原文]({it['url']})")
    lines.extend(["", "### 最热新闻 Top 20"])
    for idx, it in enumerate(top20, 1):
        prefix = "🔥【重点】" if idx <= 6 else ""
        lines.append(f"{idx}. **{prefix}{it['title']}**（{it['source']}）  ")
        lines.append(f"   {it['summary'] or '热点新闻动态。'} [查看原文]({it['url']})")
    return "\n".join(lines)


def main() -> int:
    now_bj = dt.datetime.now(BEIJING_TZ)
    report_date = now_bj.strftime("%Y-%m-%d")
    yesterday_date = (now_bj - dt.timedelta(days=1)).strftime("%Y-%m-%d")

    repo_root = Path(__file__).resolve().parent.parent
    out_dir = repo_root / "国内外新闻"
    out_dir.mkdir(parents=True, exist_ok=True)
    dated_file = out_dir / f"{report_date}-国内外早报.md"
    latest_file = out_dir / "最新早报.md"

    print(f"[INFO] 正在并行搜集 {yesterday_date} ~ {report_date} 国内与国外权威新闻源...")
    domestic_pool = collect_pool(DOMESTIC_SOURCES, now_bj, limit=65)
    international_pool = collect_pool(INTERNATIONAL_SOURCES, now_bj, limit=65)
    print(f"[INFO] 筛选完成：国内候选 {len(domestic_pool)} 条，国外候选 {len(international_pool)} 条。")

    if not domestic_pool and not international_pool:
        print("[ERROR] 未能抓取到国内外新闻候选源，请检查网络。", file=sys.stderr)
        return 1

    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    gemini_models = _discover_gemini_models(gemini_key) if gemini_key else None

    dom_md = run_llm_section(
        build_domestic_prompt(domestic_pool, report_date, yesterday_date),
        gemini_models,
    )
    if not dom_md:
        dom_md = _fallback_section("🇨🇳 第一部分：国内新闻全览", domestic_pool)

    # 短暂间隔 3 秒，友好适配免费层级并发速率限制
    time.sleep(3)

    intl_md = run_llm_section(
        build_international_prompt(international_pool, report_date, yesterday_date),
        gemini_models,
    )
    if not intl_md:
        intl_md = _fallback_section("🌐 第二部分：国外新闻全览", international_pool)

    header = (
        f"# 🌍 国内外新闻每日早报（{report_date}）\n"
        f"> 统计周期：{yesterday_date} 全天至今日早间 | 包含：**国内新闻 Top 20** + **国外新闻 Top 20**（重点事件已标注 `🔥【重点】`）\n\n"
        f"**快速导航**：[🇨🇳 第一部分：国内新闻 Top 20](#第一部分国内新闻全览{yesterday_date}--{report_date}-早间) ｜ "
        f"[🌐 第二部分：国外新闻 Top 20](#第二部分国外新闻全览{yesterday_date}--{report_date}-早间)\n\n---\n\n"
    )

    full_report = f"{header}{dom_md.strip()}\n\n---\n\n{intl_md.strip()}\n"
    dated_file.write_text(full_report, encoding="utf-8")
    latest_file.write_text(full_report, encoding="utf-8")
    print(f"[OK] 国内外早报已生成：\n  - {dated_file}\n  - {latest_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
