#!/usr/bin/env python3
"""
每日 AI 圈早报自动搜集与总结程序
运行环境：GitHub Actions 云端定时运行（电脑关机照常执行）/ 本地 Python 3.9+
功能：
  1. 并行抓取过去 24~36 小时全球与国内 AI 圈公开资讯源（Hacker News、Hugging Face Daily Papers、
     arXiv、OpenAI、Google AI、TechCrunch、The Verge、VentureBeat、Google News 中英文 24h AI 聚合等）
  2. 去重并按热度/时效筛选高价值候选池
  3. 调用大模型 API（自动识别 GEMINI_API_KEY / DEEPSEEK_API_KEY / DASHSCOPE_API_KEY / OPENAI_API_KEY）
     梳理最热 Top 20、重磅大事件、重点标注、最新 AI 技术突破及后续发展趋势研判，保留真实原文链接
  4. 输出到子目录：`AI新闻/YYYY-MM-DD-AI早报.md` 与 `AI新闻/最新早报.md`（便于根目录后续扩展其他品类报告）
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

AI_KEYWORDS = re.compile(
    r"\b(ai|llm|llms|gpt|chatgpt|openai|anthropic|claude|gemini|deepmind|deepseek|"
    r"qwen|llama|mistral|nvidia|xai|grok|agent|agents|agentic|transformer|diffusion|"
    r"multimodal|reasoning|rag|mcp|vlm|rlhf|fine-tuning|open-source|arxiv|huggingface|"
    r"hugging face|copilot|cursor|robotics|humanoid|gpu|tpu|inference|benchmark)\b|"
    r"(大模型|人工智能|智能体|具身智能|通义千问|豆包|智谱|月之暗面|英伟达|算力|开源模型|多模态|推理模型)",
    re.IGNORECASE,
)

RSS_SOURCES: list[tuple[str, str, str, int]] = [
    ("OpenAI 官方动态", "https://openai.com/news/rss.xml", "大厂发布", 35),
    ("Google AI 官方博客", "https://blog.google/technology/ai/rss/", "大厂发布", 32),
    ("Hugging Face Blog", "https://huggingface.co/blog/feed.xml", "开源与技术", 28),
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/", "行业动态", 25),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "行业动态", 24),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/", "产业与应用", 22),
    ("Ars Technica AI", "https://arstechnica.com/ai/feed/", "深度科技", 22),
    ("MIT Tech Review AI", "https://www.technologyreview.com/topic/artificial-intelligence/feed", "前沿趋势", 24),
    (
        "全球 AI 24h 热点聚合",
        "https://news.google.com/rss/search?q=(OpenAI+OR+Anthropic+OR+Gemini+OR+DeepSeek+OR+Qwen+OR+NVIDIA+OR+xAI+OR+%22AI+model%22)+when:1d&hl=en-US&gl=US&ceid=US:en",
        "全球热点",
        26,
    ),
    (
        "国内 AI 圈 24h 热点聚合",
        "https://news.google.com/rss/search?q=(%E5%A4%A7%E6%A8%A1%E5%9E%8B+OR+DeepSeek+OR+%E9%80%9A%E4%B9%89%E5%8D%83%E9%97%AE+OR+OpenAI+OR+Anthropic+OR+%E8%8B%B1%E4%BC%9F%E8%BE%BE+OR+%E6%99%BA%E8%83%BD%E4%BD%93+OR+%E5%85%B7%E8%BA%AB%E6%99%BA%E8%83%BD)+when:1d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
        "国内热点",
        28,
    ),
    ("arXiv CS.AI 最新论文", "https://rss.arxiv.org/rss/cs.AI", "前沿论文", 18),
]


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    return ctx


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


def fetch_rss_source(source_tuple: tuple[str, str, str, int], now_bj: dt.datetime) -> list[dict[str, Any]]:
    source_name, url, category, base_weight = source_tuple
    items: list[dict[str, Any]] = []
    try:
        raw_bytes = _http_get(url, timeout=12)
        root = ET.fromstring(raw_bytes)
    except Exception as exc:
        print(f"[WARN] RSS 抓取跳过 ({source_name}): {exc}", file=sys.stderr)
        return items

    # RSS 2.0 <item>
    for elem in root.findall(".//item")[:25]:
        title = _clean_text(elem.findtext("title"), 180)
        link = (elem.findtext("link") or "").strip()
        desc = _clean_text(elem.findtext("description"), 280)
        pub = _parse_datetime(elem.findtext("pubDate") or elem.findtext("{http://purl.org/dc/elements/1.1/}date"))
        if not title or not link:
            continue
        items.append(
            {
                "title": title,
                "url": link,
                "summary": desc,
                "source": source_name,
                "category": category,
                "published_at": pub,
                "score": base_weight,
            }
        )

    # Atom <entry>
    atom_ns = "{http://www.w3.org/2005/Atom}"
    for elem in root.findall(f".//{atom_ns}entry")[:25]:
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
        items.append(
            {
                "title": title,
                "url": link,
                "summary": desc,
                "source": source_name,
                "category": category,
                "published_at": pub,
                "score": base_weight,
            }
        )
    return items


def fetch_hacker_news(now_bj: dt.datetime) -> list[dict[str, Any]]:
    cutoff_ts = int((now_bj - dt.timedelta(hours=32)).timestamp())
    api_url = (
        f"https://hn.algolia.com/api/v1/search?tags=story"
        f"&numericFilters=created_at_i>{cutoff_ts},points>20&hitsPerPage=50"
    )
    items: list[dict[str, Any]] = []
    try:
        data = json.loads(_http_get(api_url, timeout=12).decode("utf-8"))
    except Exception as exc:
        print(f"[WARN] Hacker News 抓取跳过: {exc}", file=sys.stderr)
        return items

    for hit in data.get("hits", []):
        title = _clean_text(hit.get("title"), 180)
        if not title or not AI_KEYWORDS.search(title):
            continue
        object_id = hit.get("objectID", "")
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={object_id}"
        points = int(hit.get("points") or 0)
        comments = int(hit.get("num_comments") or 0)
        pub = _parse_datetime(hit.get("created_at"))
        items.append(
            {
                "title": title,
                "url": url,
                "summary": f"Hacker News 社区高热讨论（热度 {points} 赞 / {comments} 评论）",
                "source": "Hacker News",
                "category": "社区热议与技术",
                "published_at": pub,
                "score": 25 + min(points // 8, 45) + min(comments // 10, 20),
            }
        )
    return items


def fetch_hf_daily_papers(now_bj: dt.datetime) -> list[dict[str, Any]]:
    api_url = "https://huggingface.co/api/daily_papers"
    items: list[dict[str, Any]] = []
    try:
        data = json.loads(_http_get(api_url, timeout=12).decode("utf-8"))
    except Exception as exc:
        print(f"[WARN] Hugging Face Daily Papers 抓取跳过: {exc}", file=sys.stderr)
        return items

    if not isinstance(data, list):
        return items

    for entry in data[:18]:
        paper = entry.get("paper") or {}
        paper_id = paper.get("id") or ""
        title = _clean_text(entry.get("title") or paper.get("title"), 180)
        summary = _clean_text(entry.get("summary") or paper.get("summary"), 300)
        upvotes = int(paper.get("upvotes") or entry.get("numComments") or 0)
        pub = _parse_datetime(entry.get("publishedAt") or paper.get("publishedAt"))
        if not title or not paper_id:
            continue
        items.append(
            {
                "title": f"[前沿论文] {title}",
                "url": f"https://huggingface.co/papers/{paper_id}",
                "summary": f"Hugging Face 每日热榜论文（{upvotes} Upvotes）：{summary}",
                "source": "Hugging Face Daily Papers",
                "category": "最新AI技术与论文",
                "published_at": pub,
                "score": 26 + min(upvotes, 35),
            }
        )
    return items


def _normalize_title_key(title: str) -> str:
    cleaned = re.sub(r"\s*[-|–—]\s*[^-|–—]{2,25}$", "", title.lower())
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "", cleaned)
    return cleaned[:42]


def collect_candidates(now_bj: dt.datetime) -> list[dict[str, Any]]:
    all_raw: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(fetch_rss_source, src, now_bj) for src in RSS_SOURCES]
        futures.append(pool.submit(fetch_hacker_news, now_bj))
        futures.append(pool.submit(fetch_hf_daily_papers, now_bj))
        for fut in concurrent.futures.as_completed(futures):
            try:
                all_raw.extend(fut.result())
            except Exception as exc:
                print(f"[WARN] 子任务异常: {exc}", file=sys.stderr)

    cutoff_strict = now_bj - dt.timedelta(hours=36)
    cutoff_relaxed = now_bj - dt.timedelta(hours=72)

    recent: list[dict[str, Any]] = []
    older_backup: list[dict[str, Any]] = []

    for item in all_raw:
        pub = item.get("published_at")
        # arXiv / general tech feeds check AI relevance
        if item["source"] not in ("OpenAI 官方动态", "Google AI 官方博客", "Hugging Face Blog", "Hugging Face Daily Papers"):
            combined = f"{item['title']} {item['summary']}"
            if not AI_KEYWORDS.search(combined) and "AI" not in item["source"]:
                continue
        if pub is None or pub >= cutoff_strict:
            if pub is not None and pub >= now_bj - dt.timedelta(hours=24):
                item["score"] += 8
            recent.append(item)
        elif pub >= cutoff_relaxed:
            older_backup.append(item)

    pool_items = recent if len(recent) >= 25 else (recent + older_backup)
    pool_items.sort(key=lambda x: x["score"], reverse=True)

    deduped: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    for item in pool_items:
        url = item["url"]
        tkey = _normalize_title_key(item["title"])
        if url in seen_urls or (tkey and tkey in seen_titles):
            continue
        seen_urls.add(url)
        if tkey:
            seen_titles.add(tkey)
        deduped.append(item)
        if len(deduped) >= 75:
            break

    return deduped


def build_llm_prompt(candidates: list[dict[str, Any]], report_date: str, yesterday_date: str) -> str:
    lines = []
    for idx, c in enumerate(candidates, 1):
        pub_str = c["published_at"].strftime("%Y-%m-%d %H:%M") if c.get("published_at") else "过去24h内"
        lines.append(
            f"[{idx}] 标题: {c['title']}\n"
            f"    来源: {c['source']} | 分类: {c['category']} | 时间: {pub_str} | 热度权重: {c['score']}\n"
            f"    摘要: {c['summary']}\n"
            f"    原文链接: {c['url']}"
        )
    materials = "\n\n".join(lines)

    return f"""你是一位资深 AI 行业主编与首席技术分析师。今天是北京时间 {report_date} 早上，请基于下方抓取到的前一天（{yesterday_date} 及过去 24 小时内）全球与国内 AI 圈真实资讯候选池，为用户撰写一份高质量的中文《AI 圈每日早报总结报告》。

### 用户的核心阅读诉求（必须全部高质量满足）：
1. **快速掌握昨天发生了什么重磅大事件**（全局视野、前因后果与行业震动）。
2. **梳理出最热的 20 条内容（Top 20）**（合并同一事件的多家重复报道，精选真正最热、最有价值的 20 条，涵盖全球巨头、国内大模型圈、重大投融资/商业合作、开源引爆点与技术突破）。
3. **重要的内容必须醒目标注**：在你判断最重要、最具转折意义或实战价值的条目前加上 `🔥【重点】` 标签，并单独设立“重点必读标注”板块点明为何重要。
4. **了解最新更新的 AI 相关技术**：专门拆解昨天新出现的模型架构、推理/训练方法、Agent 协议/框架、多模态/具身智能技术或高赞论文。
5. **洞察 AI 后续的发展方向**：结合昨天的真实信号，给出有深度的后续技术路线与行业演进研判（拒绝空话套话，要基于当天新闻事实推导）。
6. **每条内容必须附上可点击的原文链接**：格式统一为 `[查看原文](真实URL)`，**严禁编造或修改候选池以外的链接**。

### 严格输出格式（直接输出 Markdown 正文，不要包裹在代码块中）：

# 🗞️ AI 圈每日早报（{report_date}）
> 统计周期：{yesterday_date} 全天至今日早间 | 覆盖全球与国内 AI 核心动态 Top 20

## 一、昨日 AI 圈重磅大事件速览
（用 3~5 个精炼要点提炼昨天最轰动的大事件，让读者 1 分钟看清昨日全局。）

## 二、⭐ 重点必读标注（主编高优精选）
（从今日 20 条中挑出 3~5 条你认为最关键的必看内容，说明“发生了什么 + 为什么值得重点关注 + `[查看原文](URL)`”。）

## 三、昨日最热 AI 新闻 Top 20 全景榜
（严格按 1 到 20 编号输出 20 条。重要条目在标题前加 `🔥【重点】` 标记。每条包含：**中文标题**、2~3 句核心事实与关键指标提炼、一句话影响点评，以及末尾 `[查看原文](URL)`。）

## 四、最新 AI 技术与前沿论文追踪
（专门梳理候选池中更新的 AI 技术、开源项目、工程优化或热门论文，讲清解决了什么痛点、技术亮点是什么，并附 `[查看原文](URL)`。）

## 五、AI 后续发展方向与趋势研判
（基于昨日上述事件与技术动向，提炼 3~4 条清晰的行业后续演进方向判断：例如模型能力竞争焦点、Agent/应用落地形态、算力与开源生态走向等。）

---
### 候选资讯池（共 {len(candidates)} 条真实抓取数据）：
{materials}
"""


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int = 120) -> dict[str, Any]:
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


def call_gemini(prompt: str, api_key: str) -> str:
    preferred_model = os.environ.get("GEMINI_MODEL", "").strip()
    models_to_try = [m for m in [preferred_model, "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash"] if m]
    headers = {"x-goog-api-key": api_key}

    # 自动发现最新可用 Flash 模型作为兜底，防止未来模型名称迭代退役
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

    last_err: Exception | None = None
    for model in models_to_try:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{urllib.parse.quote(model)}:generateContent"
        )
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.35,
                "maxOutputTokens": 32768,
                "thinkingConfig": {"thinkingBudget": 1024},
            },
        }
        try:
            try:
                data = _post_json(url, payload, headers=headers, timeout=150)
            except urllib.error.HTTPError as http_err:
                if http_err.code == 400:
                    payload["generationConfig"] = {"temperature": 0.35, "maxOutputTokens": 16384}
                    data = _post_json(url, payload, headers=headers, timeout=150)
                else:
                    raise
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
            if text and "20." in text:
                print(f"[INFO] 已通过 Gemini ({model}) 完成深度总结。")
                return text
            if text:
                print(f"[WARN] Gemini ({model}) 输出疑似不完整，尝试备选模型...", file=sys.stderr)
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
            {"role": "system", "content": "你是严谨、深刻的中文 AI 行业主编与技术分析师。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.35,
        "max_tokens": 8000,
    }
    data = _post_json(
        endpoint,
        payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=150,
    )
    text = data["choices"][0]["message"]["content"].strip()
    print(f"[INFO] 已通过 {model} ({base_url}) 完成深度总结。")
    return text


def synthesize_report(candidates: list[dict[str, Any]], report_date: str, yesterday_date: str) -> str:
    prompt = build_llm_prompt(candidates, report_date, yesterday_date)

    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    dashscope_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()

    providers: list[tuple[str, Any]] = []
    if gemini_key:
        providers.append(("Gemini", lambda: call_gemini(prompt, gemini_key)))
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
            print(f"[WARN] {name} 总结失败: {exc}", file=sys.stderr)

    # 无 API Key 或全部异常时的结构化兜底输出（保证即使未配置 Key 也不中断早报生成）
    top20 = candidates[:20]
    tech_items = [c for c in candidates if "论文" in c["category"] or "技术" in c["category"]][:6]

    lines = [
        f"# 🗞️ AI 圈每日早报（{report_date}）",
        f"> 统计周期：{yesterday_date} 全天至今日早间 | 自动抓取源热榜 Top 20（提示：在 GitHub Secrets 配置 `GEMINI_API_KEY` 或 `DEEPSEEK_API_KEY` 后可自动升级为深度中文精编版）",
        "",
        "## 一、昨日 AI 圈重磅大事件速览",
    ]
    for item in top20[:4]:
        lines.append(f"- **🔥 {item['title']}**（来源：{item['source']}）— [查看原文]({item['url']})")

    lines.extend(["", "## 二、⭐ 重点必读标注（热度最高 Top 5）"])
    for item in top20[:5]:
        summary = item["summary"] or "高热度行业重点动态。"
        lines.append(f"- **🔥【重点】{item['title']}**：{summary} [查看原文]({item['url']})")

    lines.extend(["", "## 三、昨日最热 AI 新闻 Top 20 全景榜"])
    for idx, item in enumerate(top20, 1):
        prefix = "🔥【重点】" if idx <= 6 else ""
        summary = item["summary"] or f"来自 {item['source']} 的热门 AI 资讯。"
        lines.append(f"{idx}. **{prefix}{item['title']}**（{item['source']}）  ")
        lines.append(f"   {summary} [查看原文]({item['url']})")

    lines.extend(["", "## 四、最新 AI 技术与前沿论文追踪"])
    for item in tech_items or top20[:5]:
        lines.append(f"- **{item['title']}**：{item['summary']} [查看原文]({item['url']})")

    lines.extend(
        [
            "",
            "## 五、AI 后续发展方向与趋势研判",
            "- 请在 GitHub 仓库 `Settings -> Secrets and variables -> Actions` 中添加 `GEMINI_API_KEY`（Google AI Studio 免费申请）或 `DEEPSEEK_API_KEY`，即可解锁每日深度中文解读与行业趋势研判。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    now_bj = dt.datetime.now(BEIJING_TZ)
    report_date = now_bj.strftime("%Y-%m-%d")
    yesterday_date = (now_bj - dt.timedelta(days=1)).strftime("%Y-%m-%d")

    repo_root = Path(__file__).resolve().parent.parent
    ai_news_dir = repo_root / "AI新闻"
    ai_news_dir.mkdir(parents=True, exist_ok=True)
    dated_file = ai_news_dir / f"{report_date}-AI早报.md"
    latest_file = ai_news_dir / "最新早报.md"

    print(f"[INFO] 正在搜集 {yesterday_date} ~ {report_date} 全球与国内 AI 圈动态...")
    candidates = collect_candidates(now_bj)
    print(f"[INFO] 共筛选出 {len(candidates)} 条去重高价值候选资讯。")

    if not candidates:
        print("[ERROR] 未能抓取到候选资讯，请检查网络连接。", file=sys.stderr)
        return 1

    report_md = synthesize_report(candidates, report_date, yesterday_date)
    if not report_md.endswith("\n"):
        report_md += "\n"

    dated_file.write_text(report_md, encoding="utf-8")
    latest_file.write_text(report_md, encoding="utf-8")
    print(f"[OK] 早报已生成：\n  - {dated_file}\n  - {latest_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
