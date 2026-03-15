import os
import json
import requests
from datetime import date
from flask import Flask, render_template, request, jsonify, stream_with_context, Response
from dotenv import load_dotenv
from prompts import SYSTEM_PROMPT, build_user_prompt, SEARCH_QUERIES

load_dotenv()

app = Flask(__name__)

SERPER_API_KEY = os.getenv("SERPER_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")


# ── Serper 搜索 ──────────────────────────────────────────────────────────────

def search_serper(query: str) -> dict:
    url = "https://google.serper.dev/search"
    headers = {
        "X-API-KEY": SERPER_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {"q": query, "num": 10, "gl": "us", "hl": "en"}
    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


def extract_snippets(result: dict, query: str) -> str:
    lines = [f"[搜索词: {query}]"]
    organic = result.get("organic", [])
    for i, item in enumerate(organic[:8], 1):
        title = item.get("title", "")
        snippet = item.get("snippet", "")
        link = item.get("link", "")
        lines.append(f"{i}. {title}\n   {snippet}\n   URL: {link}")
    knowledge = result.get("knowledgeGraph", {})
    if knowledge:
        lines.append(f"\n知识图谱: {knowledge.get('title', '')} - {knowledge.get('description', '')}")
    return "\n".join(lines)


# ── DeepSeek 调用（流式） ─────────────────────────────────────────────────────

def call_deepseek_stream(company_name: str, domain: str, all_snippets: str):
    today = date.today().strftime("%Y年%m月%d日")
    user_prompt = build_user_prompt(company_name, domain, all_snippets, today)

    url = f"{DEEPSEEK_BASE_URL}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "stream": True,
        "temperature": 0.3,
        "max_tokens": 4096,
    }
    with requests.post(url, headers=headers, json=payload, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            line = line.decode("utf-8")
            if line.startswith("data: "):
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk["choices"][0]["delta"].get("content", "")
                    if delta:
                        yield delta
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue


# ── Flask 路由 ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/investigate", methods=["POST"])
def investigate():
    data = request.get_json()
    company_name = (data.get("company_name") or "").strip()
    domain = (data.get("domain") or "").strip()

    if not company_name:
        return jsonify({"error": "公司名称不能为空"}), 400
    if not SERPER_API_KEY or not DEEPSEEK_API_KEY:
        return jsonify({"error": "服务器未配置 API Key，请检查 .env 文件"}), 500

    def generate():
        try:
            # ── 阶段1：执行搜索 ──
            all_snippets_parts = []
            total = len(SEARCH_QUERIES)
            for idx, tpl in enumerate(SEARCH_QUERIES, 1):
                query = tpl.format(company=company_name)
                yield _sse("progress", {
                    "step": idx,
                    "total": total,
                    "message": f"正在执行第 {idx}/{total} 轮搜索：{query}",
                    "phase": "search"
                })
                try:
                    result = search_serper(query)
                    snippet = extract_snippets(result, query)
                    all_snippets_parts.append(snippet)
                except Exception as e:
                    all_snippets_parts.append(f"[搜索词: {query}]\n搜索失败: {e}")

            all_snippets = "\n\n" + "─" * 60 + "\n\n".join(all_snippets_parts)

            # ── 阶段2：AI 分析 ──
            yield _sse("progress", {
                "step": total + 1,
                "total": total + 1,
                "message": "搜索完成，正在调用 AI 分析师生成报告…",
                "phase": "ai"
            })

            collected = []
            for chunk in call_deepseek_stream(company_name, domain, all_snippets):
                collected.append(chunk)
                yield _sse("chunk", {"text": chunk})

            raw_json = "".join(collected).strip()
            # 去掉可能包裹的 markdown 代码块
            if raw_json.startswith("```"):
                raw_json = raw_json.split("\n", 1)[-1]
                if raw_json.endswith("```"):
                    raw_json = raw_json[:-3].strip()

            try:
                report = json.loads(raw_json)
                yield _sse("done", {"report": report})
            except json.JSONDecodeError as e:
                yield _sse("error", {"message": f"AI 返回格式解析失败: {e}", "raw": raw_json[:500]})

        except requests.HTTPError as e:
            status = e.response.status_code if e.response else "?"
            yield _sse("error", {"message": f"API 请求失败 (HTTP {status}): {str(e)}"})
        except Exception as e:
            yield _sse("error", {"message": f"服务器错误: {str(e)}"})

    return Response(stream_with_context(generate()), content_type="text/event-stream")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
