#!/usr/bin/env python3
"""
每日 GitHub 好用的 AI Skill 与 AI 工具早报生成程序
运行环境：GitHub Actions 云端定时运行（电脑关机照常执行）/ 本地 Python 3.9+
功能：
  1. 多路并行挖掘 GitHub 过去 24h~7d 最热门的 AI 工具与 AI Skill：
     - GitHub Trending 每日飙升榜（含今日新增 Star 数）
     - GitHub Search API：最新爆火 AI Skill（SKILL.md / Agent Skills / Claude & Codex & Cursor Skills / MCP Server）
     - GitHub Search API：近期新诞生且高星暴涨的实用 AI 工具、Agent、多模态与效率神器
  2. 并行拉取候选项目 README 核心片段，确保准确提炼“它是干什么的 + 具体能做出什么东西”
  3. 调用大模型精编 Top 20、重点推荐标注（🔥【重点推荐】）、实战产出场景拆解及风向观察
  4. 输出到子目录：`AI工具与Skill/YYYY-MM-DD-AI工具与Skill早报.md` 与 `AI工具与Skill/最新早报.md`
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import html
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

BEIJING_TZ = dt.timezone(dt.timedelta(hours=8))
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

AI_REPO_KEYWORDS = re.compile(
    r"\b(ai|llm|llms|gpt|claude|gemini|deepseek|qwen|agent|agents|agentic|skill|skills|"
    r"mcp|rag|prompt|copilot|cursor|codex|vlm|tts|asr|voice|diffusion|video|ocr|pdf|"
    r"browser|automation|workflow|sandbox|inference|fine-tune|eval|benchmark|memory|"
    r"knowledge|chat|assistant|coding|code-gen|avatar|robotics|transformer|open-source)\b|"
    r"(大模型|智能体|提示词|技能|工作流|多模态|语音|视频生成|知识库|自动化)",
    re.IGNORECASE,
)


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def _github_headers(accept: str = "application/vnd.github+json") -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"
    return headers


def _http_get(url: str, headers: dict[str, str] | None = None, timeout: int = 15) -> bytes:
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            return resp.read()
    except ssl.SSLError:
        unverified = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=timeout, context=unverified) as resp:
            return resp.read()


def _clean_text(raw: str | None, max_len: int = 320) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[#>*`~_]{2,}", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        return text[: max_len - 1].rstrip() + "…"
    return text


def fetch_github_trending(url: str, source_label: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    try:
        page_html = _http_get(url, timeout=15).decode("utf-8", errors="ignore")
    except Exception as exc:
        print(f"[WARN] GitHub Trending 抓取跳过 ({source_label}): {exc}", file=sys.stderr)
        return items

    articles = re.findall(r"<article[^>]*class=\"[^\"]*Box-row[^\"]*\"[^>]*>(.*?)</article>", page_html, re.S)
    for rank, art in enumerate(articles[:25], 1):
        m_repo = re.search(r"<h2[^>]*>.*?<a[^>]*href=\"/([^\"/]+/[^\"/\s]+)\"", art, re.S)
        if not m_repo:
            continue
        full_name = m_repo.group(1).strip()
        m_desc = re.search(r"<p[^>]*class=\"[^\"]*col-9[^\"]*\"[^>]*>(.*?)</p>", art, re.S)
        desc = _clean_text(m_desc.group(1) if m_desc else "", 260)
        m_lang = re.search(r"itemprop=\"programmingLanguage\"[^>]*>(.*?)<", art, re.S)
        lang = _clean_text(m_lang.group(1) if m_lang else "Unknown", 40)
        m_total = re.search(r"href=\"/[^\"]+/stargazers\"[^>]*>(.*?)</a>", art, re.S)
        total_stars_str = re.sub(r"[^\d]", "", m_total.group(1)) if m_total else "0"
        total_stars = int(total_stars_str or 0)
        m_today = re.search(r"([\d,]+)\s+stars\s+today", art, re.I)
        stars_today = int(m_today.group(1).replace(",", "")) if m_today else 0

        combined = f"{full_name} {desc}"
        if not AI_REPO_KEYWORDS.search(combined):
            continue

        items.append(
            {
                "full_name": full_name,
                "url": f"https://github.com/{full_name}",
                "description": desc,
                "language": lang or "Multi",
                "stars": total_stars,
                "stars_today": stars_today,
                "topics": [],
                "category": "GitHub 今日热榜飙升",
                "score": 55 + min(stars_today // 10, 45) + max(0, 15 - rank),
            }
        )
    return items


def fetch_github_search(query: str, category: str, base_weight: int, per_page: int = 20) -> list[dict[str, Any]]:
    encoded_q = urllib.parse.quote(query)
    url = f"https://api.github.com/search/repositories?q={encoded_q}&sort=stars&order=desc&per_page={per_page}"
    items: list[dict[str, Any]] = []
    try:
        data = json.loads(_http_get(url, headers=_github_headers(), timeout=15).decode("utf-8"))
    except Exception as exc:
        print(f"[WARN] GitHub Search 跳过 ({category}): {exc}", file=sys.stderr)
        return items

    for rank, repo in enumerate(data.get("items", []), 1):
        full_name = repo.get("full_name") or ""
        if not full_name:
            continue
        desc = _clean_text(repo.get("description"), 260)
        stars = int(repo.get("stargazers_count") or 0)
        lang = repo.get("language") or "Markdown/Multi"
        topics = (repo.get("topics") or [])[:8]
        items.append(
            {
                "full_name": full_name,
                "url": repo.get("html_url") or f"https://github.com/{full_name}",
                "description": desc,
                "language": lang,
                "stars": stars,
                "stars_today": 0,
                "topics": topics,
                "category": category,
                "score": base_weight + min(stars // 40, 30) + max(0, 12 - rank),
            }
        )
    return items


def fetch_readme_excerpt(full_name: str) -> str:
    url = f"https://api.github.com/repos/{full_name}/readme"
    try:
        raw = _http_get(
            url,
            headers=_github_headers(accept="application/vnd.github.raw+json"),
            timeout=10,
        ).decode("utf-8", errors="ignore")
        # 去掉顶部徽章与 HTML 注释，保留核心功能介绍段落
        raw = re.sub(r"<!--.*?-->", " ", raw, flags=re.S)
        lines = []
        for line in raw.splitlines():
            s = line.strip()
            if not s or s.startswith("[![") or s.startswith("<img") or s.startswith("<p align"):
                continue
            if s.startswith("|") and "---" in s:
                continue
            lines.append(s)
            if sum(len(x) for x in lines) > 900:
                break
        return _clean_text(" ".join(lines), 650)
    except Exception:
        return ""


def collect_candidates(now_bj: dt.datetime) -> list[dict[str, Any]]:
    d7 = (now_bj - dt.timedelta(days=7)).strftime("%Y-%m-%d")
    d14 = (now_bj - dt.timedelta(days=14)).strftime("%Y-%m-%d")
    d2 = (now_bj - dt.timedelta(days=2)).strftime("%Y-%m-%d")

    search_tasks = [
        # 1. 近期新诞生或活跃飙升的 AI Skill / Agent Skills / Claude & Cursor & Codex Skills
        (
            f'("agent-skills" OR "claude-skills" OR "ai-skills" OR "SKILL.md" OR "codex-skills" OR "cursor-skills" OR "agent skill") pushed:>={d2} stars:>=15',
            "🧩 热门 AI Skill 技能包",
            52,
            18,
        ),
        (
            f'(skill OR skills OR mcp-server OR "model context protocol") (ai OR agent OR claude OR llm OR cursor) created:>={d14} stars:>=15',
            "🔌 新晋 AI Skill 与 MCP 工具",
            50,
            18,
        ),
        # 2. 过去 7 天新诞生且飙升最猛的 AI 工具黑马
        (
            f'(ai OR llm OR agent OR rag OR multimodal OR workflow OR browser-use OR coding) created:>={d7} stars:>=35',
            "🚀 本周新晋爆款 AI 工具",
            48,
            20,
        ),
        # 3. 过去 48 小时活跃更新的高口碑实用 AI 生产力工具
        (
            f'(ai-agent OR mcp-server OR llm-tool OR automation OR rag OR tts OR video-generation) pushed:>={d2} stars:500..25000',
            "🛠️ 高口碑活跃 AI 实用工具",
            40,
            15,
        ),
    ]

    merged_map: dict[str, dict[str, Any]] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futs = [
            pool.submit(fetch_github_trending, "https://github.com/trending?since=daily", "All Daily"),
            pool.submit(
                fetch_github_trending,
                "https://github.com/trending/python?since=daily",
                "Python Daily",
            ),
            pool.submit(
                fetch_github_trending,
                "https://github.com/trending/typescript?since=daily",
                "TypeScript Daily",
            ),
        ]
        for q, cat, weight, pp in search_tasks:
            futs.append(pool.submit(fetch_github_search, q, cat, weight, pp))

        for fut in concurrent.futures.as_completed(futs):
            try:
                for item in fut.result():
                    key = item["full_name"].lower()
                    if key not in merged_map:
                        merged_map[key] = item
                    else:
                        existing = merged_map[key]
                        existing["score"] = max(existing["score"], item["score"]) + 6
                        if item["stars_today"] > existing["stars_today"]:
                            existing["stars_today"] = item["stars_today"]
                        if item["stars"] > existing["stars"]:
                            existing["stars"] = item["stars"]
                        if item["topics"] and not existing["topics"]:
                            existing["topics"] = item["topics"]
            except Exception as exc:
                print(f"[WARN] 汇总任务异常: {exc}", file=sys.stderr)

    candidates = sorted(merged_map.values(), key=lambda x: x["score"], reverse=True)[:34]

    # 并行拉取前 30 个候选仓库的 README 摘要，支撑深度解读“能干什么 + 能做出什么东西”
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        readme_map = {
            pool.submit(fetch_readme_excerpt, c["full_name"]): c for c in candidates[:30]
        }
        for fut, item in readme_map.items():
            try:
                item["readme_excerpt"] = fut.result()
            except Exception:
                item["readme_excerpt"] = ""

    return candidates


def build_llm_prompt(candidates: list[dict[str, Any]], report_date: str, yesterday_date: str) -> str:
    blocks = []
    for idx, c in enumerate(candidates, 1):
        today_str = f" | 今日新增 +{c['stars_today']} Stars" if c.get("stars_today") else ""
        topics_str = ", ".join(c.get("topics") or []) or "AI / Tool / Skill"
        readme_str = c.get("readme_excerpt") or "（详见仓库主页）"
        blocks.append(
            f"[{idx}] 仓库: {c['full_name']}\n"
            f"    链接: {c['url']}\n"
            f"    分类: {c['category']} | 语言: {c['language']} | 总 Star: {c['stars']}{today_str}\n"
            f"    标签: {topics_str}\n"
            f"    一句话简介: {c['description']}\n"
            f"    README 核心内容摘录: {readme_str}"
        )
    materials = "\n\n".join(blocks)

    return f"""你是一位极客向的资深 AI 工具猎手与全栈产品架构师。今天是北京时间 {report_date} 早上，请基于下方实时抓取自 GitHub（涵盖每日 Trending 飙升榜、最新 AI Skill 技能包、MCP 插件及高星开源 AI 工具，共 {len(candidates)} 个候选仓库及 README 详情），为用户撰写一份极其实用、接地气的中文《GitHub 每日最热 AI Skill 与实用工具早报》。

### 用户的核心诉求（每一条都必须透彻回答，拒绝笼统空话）：
1. **精选每日最热的 Top 20 项目**（兼顾“好用的 AI Skill / MCP 技能插件”与“高实用性 AI 工具/框架”，剔除纯水榜或空壳仓库）。
2. **用大白话讲透两个核心问题**：
   - **① 它是干什么的？**（核心功能、解决什么工作/开发痛点、适合嵌入什么环境如 Claude / Antigravity / Cursor / 本地终端 / Web 应用）。
   - **② 用它具体能做出来什么东西？**（必须给出 2~3 个具象、可落地的产出物或场景示例！例如：“能一键把你的混乱 PDF 合同提取成结构化 Excel 台账”、“能给 Agent 装上自动操作飞书/浏览器的技能做出无人值守客服”、“能输入一段录音 30 秒做出带字幕的数字人播客视频”等）。
3. **醒目标注重点推荐**：挑出你认为最惊艳、最值得今天立刻上手试用的项目加上 `🔥【重点推荐】` 标记，并在“今日必试精选”板块单独重点点拨。
4. **保留真实 GitHub 链接**：格式统一为 `[GitHub 仓库](真实URL)`，严禁编造候选池以外的链接。

### 严格输出格式（直接输出 Markdown 正文，不要包裹在最外层代码块中）：

# 🛠️ GitHub 每日最热 AI Skill 与实用工具早报（{report_date}）
> 统计周期：{yesterday_date} ~ {report_date} 早间 | 聚焦好用的 AI Skill、MCP 插件与高能 AI 开源神器 Top 20

---

## 一、今日 GitHub AI 工具圈风向速览
（用 3~4 条精炼要点概括今天 GitHub 上最火的工具/Skill 趋势：大家都在造什么轮子、什么类型的 Skill 最受欢迎。）

---

## 二、⭐ 今日必试重点推荐（主编严选 4~5 款神器）
（挑出今天最值得优先点开试用的 4~5 个 AI Skill 或工具，清晰写明：**项目名 + 为什么值得重点关注 + 装上它能立竿见影做出什么 + `[GitHub 仓库](URL)`**。）

---

## 三、每日最热 AI Skill 与实用工具 Top 20 全景榜
（严格按 1 到 20 编号输出完整 20 个项目。特别值得推荐的在标题前加 `🔥【重点推荐】`。每个项目严格包含以下四行清晰结构：）

1. 🔥【重点推荐】**`owner/repo` — 中文一句话定位**（类型标签：`AI Skill` / `MCP 工具` / `Agent 神器` / `多模态工具` 等 ｜ ⭐ 总数 / 今日热度 ｜ 语言）
   - **它是干什么的**：用通俗清晰的语言讲清它的核心机制与解决的痛点。
   - **能做出来什么东西**：列举具体能做出的成品、自动化效果或实际业务产出。
   - **上手链接**：[GitHub 仓库](真实URL)

（……依此类推完整列满 1~20 条……）

---

## 四、工具组合拳灵感（用今天的工具/Skill 搭一套工作流）
（从今日榜单中挑 2~3 个工具或 Skill，给用户设计 1~2 个“组合搭配能搓出什么酷东西/提效流水线”的实操脑洞。）

---
### GitHub 实时候选仓库池（含 README 摘录）：
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


def call_gemini(prompt: str, api_key: str) -> str:
    preferred_model = os.environ.get("GEMINI_MODEL", "").strip()
    models_to_try = [
        m for m in [preferred_model, "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash"] if m
    ]
    headers = {"x-goog-api-key": api_key}

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
                print(f"[INFO] 已通过 Gemini ({model}) 完成深度精编。")
                return text
            if text:
                print(f"[WARN] Gemini ({model}) 输出疑似未满 20 条，尝试备选...", file=sys.stderr)
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
            {"role": "system", "content": "你是精通开源生态的中文 AI 工具猎手与架构师。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.35,
        "max_tokens": 12000,
    }
    data = _post_json(
        endpoint,
        payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=150,
    )
    return data["choices"][0]["message"]["content"].strip()


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

    # 兜底结构化输出
    top20 = candidates[:20]
    lines = [
        f"# 🛠️ GitHub 每日最热 AI Skill 与实用工具早报（{report_date}）",
        f"> 统计周期：{yesterday_date} ~ {report_date} 早间 | GitHub AI 工具与 Skill Top 20",
        "",
        "## 三、每日最热 AI Skill 与实用工具 Top 20 全景榜",
    ]
    for idx, c in enumerate(top20, 1):
        prefix = "🔥【重点推荐】" if idx <= 6 else ""
        today_badge = f" / 今日 +{c['stars_today']}" if c.get("stars_today") else ""
        lines.append(
            f"{idx}. {prefix}**`{c['full_name']}`**（{c['category']} ｜ ⭐ {c['stars']}{today_badge} ｜ {c['language']}）  \n"
            f"   - **简介**：{c['description'] or c.get('readme_excerpt', '')[:160]}  \n"
            f"   - **上手链接**：[GitHub 仓库]({c['url']})"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    now_bj = dt.datetime.now(BEIJING_TZ)
    report_date = now_bj.strftime("%Y-%m-%d")
    yesterday_date = (now_bj - dt.timedelta(days=1)).strftime("%Y-%m-%d")

    repo_root = Path(__file__).resolve().parent.parent
    out_dir = repo_root / "AI工具与Skill"
    out_dir.mkdir(parents=True, exist_ok=True)
    dated_file = out_dir / f"{report_date}-AI工具与Skill早报.md"
    latest_file = out_dir / "最新早报.md"

    print(f"[INFO] 正在挖掘 {yesterday_date} ~ {report_date} GitHub 最热 AI Skill 与实用工具...")
    candidates = collect_candidates(now_bj)
    print(f"[INFO] 共锁定 {len(candidates)} 个高热候选仓库（已补充 README 核心详情）。")

    if not candidates:
        print("[ERROR] 未能抓取到 GitHub 候选项目，请检查网络。", file=sys.stderr)
        return 1

    report_md = synthesize_report(candidates, report_date, yesterday_date)
    if not report_md.endswith("\n"):
        report_md += "\n"

    dated_file.write_text(report_md, encoding="utf-8")
    latest_file.write_text(report_md, encoding="utf-8")
    print(f"[OK] AI 工具与 Skill 早报已生成：\n  - {dated_file}\n  - {latest_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
