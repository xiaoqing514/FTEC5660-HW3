#!/usr/bin/env python3
"""FTEC5660 HW3 student starter: an agent that applies 3 of Fisher's 15 points using tools."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sqlite3
import threading
import zlib
from pathlib import Path
from typing import Any


MODEL_NAME = "deepseek-v4-flash"
INTERVIEW_LIMIT = 10
MAX_SQL_ROWS = 50

# Philip Fisher, "Common Stocks and Uncommon Profits", Part One, Chapter 3.
# For every point, PASS means the company looks good on it. You choose 3
# points and decide yourself what PASS means for each of them.
FISHER_POINTS = {
    1: "Does the company have products or services with sufficient market potential to make possible a sizable increase in sales for at least several years?",
    2: "Does the management have a determination to continue to develop products or processes that will still further increase total sales potentials when the growth potentials of currently attractive product lines have largely been exploited?",
    3: "How effective are the company's research and development efforts in relation to its size?",
    4: "Does the company have an above-average sales organization?",
    5: "Does the company have a worthwhile profit margin?",
    6: "What is the company doing to maintain or improve profit margins?",
    7: "Does the company have outstanding labor and personnel relations?",
    8: "Does the company have outstanding executive relations?",
    9: "Does the company have depth to its management?",
    10: "How good are the company's cost analysis and accounting controls?",
    11: "Are there other aspects of the business, somewhat peculiar to the industry involved, which will give the investor important clues as to how outstanding the company may be in relation to its competition?",
    12: "Does the company have a short-range or long-range outlook in regard to profits?",
    13: "In the foreseeable future will the growth of the company require sufficient equity financing so that the larger number of shares then outstanding will largely cancel the existing stockholders' benefit from this anticipated growth?",
    14: "Does the management talk freely to investors about its affairs when things are going well but 'clam up' when troubles and disappointments occur?",
    15: "Does the company have a management of unquestionable integrity?",
}
VERDICTS = ("PASS", "FAIL")


def load_env_file(path: Path = Path(".env")) -> None:
    """Load the simple KEY=VALUE entries used by this homework."""
    if not path.is_file():
        return
    import os

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


# Choose any 3 of the 15 points, e.g. CHOSEN_POINTS = [1, 7, 15]. Your agent
# gives a PASS or FAIL verdict on these 3 points only, with your own rule for each.
CHOSEN_POINTS: list[int] = [5, 10, 11]

# --- Agent configuration ----------------------------------------------------
# Point 5 (worthwhile profit margin): FY2025 operating margin vs the median of
# the company's own industry.  Point 10 (cost analysis / accounting controls):
# the revenue management discloses on each of the four earnings calls has to
# agree with the revenue recorded in the order database.  Point 11 (advantage
# over competitors): a competitor or customer has to describe a durable
# advantage (switching costs, hard-to-copy know-how, unmatched performance)
# rather than a commodity product sold on price.

QUARTERS = ("2025Q3", "2025Q4", "2026Q1", "2026Q2")
MAX_WORKERS = 4
REVENUE_TOLERANCE_MUSD = 0.25  # calls quote revenue to 0.1 USD million
MODEL_TEMPERATURE = 0.0

POINT5_SQL = """
SELECT c.industry AS industry,
       m.operating_margin_pct AS industry_median_pct,
       (SELECT ROUND(SUM(amount), 2) FROM orders
         WHERE order_date >= '2025-01-01' AND order_date < '2026-01-01') AS revenue_kusd,
       (SELECT ROUND(SUM(cost_of_sales), 2) FROM monthly_costs
         WHERE month >= '2025-01' AND month <= '2025-12') AS cost_of_sales_kusd,
       (SELECT ROUND(SUM(selling_admin), 2) FROM monthly_costs
         WHERE month >= '2025-01' AND month <= '2025-12') AS selling_admin_kusd,
       (SELECT ROUND(SUM(research_development), 2) FROM monthly_costs
         WHERE month >= '2025-01' AND month <= '2025-12') AS rnd_kusd
FROM company c
JOIN industry_medians m ON m.industry = c.industry
"""

POINT10_SQL = """
SELECT CASE
         WHEN order_date >= '2026-04-01' THEN '2026Q2'
         WHEN order_date >= '2026-01-01' THEN '2026Q1'
         WHEN order_date >= '2025-10-01' THEN '2025Q4'
         ELSE '2025Q3'
       END AS quarter,
       ROUND(SUM(amount) / 1000.0, 3) AS revenue_musd
FROM orders
WHERE order_date >= '2025-07-01' AND order_date < '2026-07-01'
GROUP BY quarter
ORDER BY quarter
"""

EXTRACT_PROMPT = """You are reading earnings-call transcripts for one company.
For every quarter below, find the sentence in which management states that
quarter's revenue and report the number in USD millions.

Rules:
- Report the revenue of the quarter the call is about, not guidance, not a
  year-on-year growth rate, and not a comparison quarter mentioned in passing.
- Amounts are in USD millions in these transcripts; convert if another unit is used.
- Keep the exact wording of the sentence that states the figure in "quote".
- If a quarter states no explicit revenue figure, use null for that quarter.

Return ONLY a JSON object with this shape (one key per quarter):
{{"2025Q3": {{"revenue_musd": 72.0, "quote": "..."}},
  "2025Q4": {{"revenue_musd": 70.5, "quote": "..."}},
  "2026Q1": {{"revenue_musd": 71.6, "quote": "..."}},
  "2026Q2": {{"revenue_musd": 79.7, "quote": "..."}}}}

Transcripts:
{transcripts}
"""

JUDGE_PROMPT = """You are applying point 11 of Philip Fisher's checklist:
"Are there other aspects of the business, somewhat peculiar to the industry
involved, which will give the investor important clues as to how outstanding
the company may be in relation to its competition?"

An informed {role} said about this company:
"{quote}"

Decide whether the company has a durable competitive advantage.
PASS when the quote describes something that protects the business: customers
face real switching costs, the installed base or know-how is hard to copy,
rivals cannot match the performance, or customers who leave come back.
FAIL when the product is interchangeable or a commodity, the company competes
mainly on price, or several equivalent suppliers are available.

Return ONLY JSON:
{{"verdict": "PASS" or "FAIL", "evidence": "<the decisive words>", "reason": "<one sentence>"}}
"""

SUMMARY_PROMPT = """Write the summary for one company in an investor report that
applies three of Fisher's points. Base every statement on the facts below.
Use two or three sentences and plain English. Do not use bullet points.

Facts:
{facts}

Mention the verdicts (PASS/FAIL) for points 5, 10 and 11 and the evidence that
drives them. Do not invent numbers, do not call management honest or dishonest,
and do not mention Fisher's numbering unless it helps clarity. Return only the
summary text.
"""

REVENUE_RE = re.compile(
    r"revenue[^.\n]{0,120}?(?:USD|US\$|\$)\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(million|billion)",
    re.IGNORECASE,
)


def _tool(tools: list[Any], name: str) -> Any:
    """Return the per-company tool called *name*."""
    for candidate in tools:
        if getattr(candidate, "name", "") == name:
            return candidate
    raise KeyError(f"tool {name!r} is not available")


def _table(text: Any) -> list[dict[str, str]]:
    """Parse the pipe-separated table returned by sql_query."""
    lines = [line for line in str(text).strip().splitlines() if line.strip()]
    if not lines:
        return []
    header = [cell.strip() for cell in lines[0].split("|")]
    rows = []
    for line in lines[1:]:
        if line.startswith("...") or line.startswith("(") or line.startswith("SQL error"):
            continue
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) == len(header):
            rows.append(dict(zip(header, cells)))
    return rows


def _number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _json_object(text: Any) -> dict[str, Any] | None:
    """Extract the first JSON object from a model reply."""
    raw = text if isinstance(text, str) else str(text)
    for block in raw.replace("```json", "```").split("```"):
        block = block.strip()
        start, end = block.find("{"), block.rfind("}")
        if start != -1 and end > start:
            try:
                value = json.loads(block[start : end + 1])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    return None


def _model_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, list):
        return " ".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    return str(content)


def _quote_from_regex(document: str) -> tuple[float | None, str]:
    match = REVENUE_RE.search(document)
    if not match:
        return None, ""
    value = float(match.group(1).replace(",", ""))
    if match.group(2).lower() == "billion":
        value *= 1000.0
    return value, match.group(0)


def _earnings_documents(tools: list[Any]) -> dict[str, str]:
    """Map '2025Q3' -> document name using list_documents."""
    listing = str(_tool(tools, "list_documents").invoke({}))
    documents: dict[str, str] = {}
    for name in re.findall(r"[A-Za-z0-9_.\-]+\.md", listing):
        match = re.search(r"(20\d{2})[_\-]?q([1-4])", name, re.IGNORECASE)
        if match:
            documents[f"{match.group(1)}Q{match.group(2)}"] = name
    return documents


def _point5(tools: list[Any]) -> dict[str, Any]:
    rows = _table(_tool(tools, "sql_query").invoke({"query": POINT5_SQL}))
    if not rows:
        return {"verdict": "FAIL", "detail": "no company or industry data returned", "error": True}
    row = rows[0]
    industry = row.get("industry", "?")
    median = _number(row.get("industry_median_pct"))
    revenue = _number(row.get("revenue_kusd"))
    parts = [_number(row.get(key)) for key in ("cost_of_sales_kusd", "selling_admin_kusd", "rnd_kusd")]
    if median is None or not revenue or any(part is None for part in parts):
        return {"verdict": "FAIL", "detail": "revenue, cost or industry benchmark missing", "error": True}
    operating_profit = revenue - sum(parts)  # type: ignore[arg-type]
    margin = operating_profit / revenue * 100.0
    verdict = "PASS" if margin > median else "FAIL"
    detail = (
        f"FY2025 revenue USD {revenue / 1000:.1f}m, operating profit USD {operating_profit / 1000:.1f}m, "
        f"operating margin {margin:.1f}% vs {industry} median {median:.1f}%"
    )
    return {"verdict": verdict, "margin": margin, "median": median, "detail": detail}


def _extract_revenues(llm: Any, documents: dict[str, str], quarter_texts: dict[str, str]) -> dict[str, float | None]:
    transcripts = "\n".join(
        f"### {quarter} (document: {documents.get(quarter, 'missing')})\n{quarter_texts.get(quarter, '')}"
        for quarter in QUARTERS
    )
    reply = _model_text(llm.invoke(EXTRACT_PROMPT.format(transcripts=transcripts)))
    data = _json_object(reply) or {}
    extracted: dict[str, float | None] = {}
    for quarter in QUARTERS:
        entry = data.get(quarter)
        value = entry.get("revenue_musd") if isinstance(entry, dict) else entry
        number = _number(value) if value is not None else None
        if number is not None:
            extracted[quarter] = number
    return extracted


def _point10(llm: Any, tools: list[Any]) -> dict[str, Any]:
    documents = _earnings_documents(tools)
    missing = [quarter for quarter in QUARTERS if quarter not in documents]
    if missing:
        return {"verdict": "FAIL", "detail": f"earnings call missing for {', '.join(missing)}", "error": True}
    texts = {
        quarter: str(_tool(tools, "read_document").invoke({"name": documents[quarter]}))
        for quarter in QUARTERS
    }
    extracted = _extract_revenues(llm, documents, texts)
    database = {
        row["quarter"]: _number(row.get("revenue_musd"))
        for row in _table(_tool(tools, "sql_query").invoke({"query": POINT10_SQL}))
        if row.get("quarter") in QUARTERS
    }

    disagreements = []
    for quarter in QUARTERS:
        disclosed = extracted.get(quarter)
        fallback, quote = _quote_from_regex(texts[quarter])
        # Cross-check the model against the literal sentence in the document.
        if disclosed is not None and fallback is not None and abs(disclosed - fallback) > 1.0:
            disagreements.append(
                f"{quarter}: extracted {disclosed:.1f} but transcript says {fallback:.1f} ({quote})"
            )
        if disclosed is None and fallback is not None:
            extracted[quarter] = fallback

    if disagreements:
        # Reflection pass: ask once more, pointing at the suspicious quarters.
        note = EXTRACT_PROMPT.format(
            transcripts="\n".join(f"### {quarter}\n{texts[quarter]}" for quarter in QUARTERS)
        )
        note += "\nNote: a simple pattern check disagrees with the first reading:\n" + "\n".join(disagreements)
        note += "\nRe-read those sentences and return the corrected JSON."
        revised = _json_object(_model_text(llm.invoke(note))) or {}
        for quarter in QUARTERS:
            entry = revised.get(quarter)
            value = entry.get("revenue_musd") if isinstance(entry, dict) else entry
            number = _number(value) if value is not None else None
            if number is not None:
                extracted[quarter] = number

    pieces = []
    comparisons = []
    all_match = True
    for quarter in QUARTERS:
        disclosed = extracted.get(quarter)
        recorded = database.get(quarter)
        if disclosed is None or recorded is None:
            all_match = False
            comparisons.append((quarter, disclosed, recorded, None))
            pieces.append(f"{quarter}: revenue unavailable")
            continue
        difference = abs(disclosed - recorded)
        comparisons.append((quarter, disclosed, recorded, difference))
        tolerance = max(REVENUE_TOLERANCE_MUSD, abs(recorded) * 0.001)
        ok = difference <= tolerance
        all_match = all_match and ok
        pieces.append(
            f"{quarter}: disclosed USD {disclosed:.1f}m vs recorded USD {recorded:.1f}m"
            + ("" if ok else " (difference beyond rounding)")
        )
    verdict = "PASS" if all_match else "FAIL"
    return {"verdict": verdict, "detail": "; ".join(pieces), "comparisons": comparisons}


def _interview(tools: list[Any], role: str, topic: str) -> str:
    return str(_tool(tools, "interview").invoke({"role": role, "topic": topic}))


def _point11(llm: Any, tools: list[Any]) -> dict[str, Any]:
    role = "competitor"
    quote = _interview(tools, role, "competitive_edge")
    refusal = re.search(
        r"can(?:no|['\u2019])t comment|could(?:n['\u2019]t| not) comment|no view|not my place"
        r"|don['\u2019]t know|do not know|wouldn['\u2019]t know",
        quote,
        re.I,
    )
    if refusal or len(quote.strip()) < 20:
        role = "customer"
        quote = _interview(tools, role, "competitive_edge")
    judge = _model_text(llm.invoke(JUDGE_PROMPT.format(role=role, quote=quote.strip())))
    data = _json_object(judge) or {}
    verdict = str(data.get("verdict", "")).strip().upper()
    if verdict not in VERDICTS:
        verdict = "FAIL"
    return {
        "verdict": verdict,
        "role": role,
        "quote": quote.strip(),
        "reason": str(data.get("reason", "")).strip(),
        "evidence": str(data.get("evidence", "")).strip(),
    }


def _fallback_summary(results: dict[int, dict[str, Any]]) -> str:
    pieces = []
    for point in CHOSEN_POINTS:
        result = results.get(point, {})
        pieces.append(f"Point {point} is {result.get('verdict', 'FAIL')}: {result.get('detail', '')}".strip())
    return " ".join(pieces)


def _analyze_one(llm: Any, tools: list[Any]) -> dict[str, Any]:
    results: dict[int, dict[str, Any]] = {}
    errors: list[str] = []

    for point, handler in ((5, _point5), (10, _point10), (11, _point11)):
        try:
            if point == 5:
                results[point] = handler(tools)
            else:
                results[point] = handler(llm, tools)
        except Exception as exc:  # keep one failed point from sinking the company
            results[point] = {"verdict": "FAIL", "detail": f"analysis error: {exc}", "error": True}
            errors.append(f"point {point}: {exc}")

    five, ten, eleven = results[5], results[10], results[11]
    facts = (
        f"Point 5 - worthwhile profit margin: {five['verdict']}. {five.get('detail', '')}\n"
        f"Point 10 - cost analysis and accounting controls: {ten['verdict']}. {ten.get('detail', '')}\n"
        f"Point 11 - advantage over competitors: {eleven['verdict']}. "
        f"A {eleven.get('role', 'competitor')} said: \"{eleven.get('quote', '')}\" "
        f"({eleven.get('reason', '')})"
    )
    try:
        summary = _model_text(llm.invoke(SUMMARY_PROMPT.format(facts=facts))).strip()
    except Exception as exc:
        summary = _fallback_summary(results)
        errors.append(f"summary: {exc}")
    if not summary:
        summary = _fallback_summary(results)

    points = {point: results[point]["verdict"] for point in CHOSEN_POINTS}
    if errors:
        summary = f"{summary} [issues: {'; '.join(errors)}]"
    return {"points": points, "summary": summary, "evidence": {str(k): v for k, v in results.items()}}


def build_agent() -> Any:
    """Create and return your model or agent once.

    Suggested imports:
        from langchain_deepseek import ChatDeepSeek
        from langchain.agents import create_agent

    Use the DeepSeek model named by ``MODEL_NAME``. The API key is loaded
    from .env. Each company has its own tools (see ``analyze_companies``), so
    you will probably create one tool-calling agent per company there.
    """
    ### YOUR CODE HERE
    from langchain_deepseek import ChatDeepSeek

    return {
        "model": ChatDeepSeek(
            model=MODEL_NAME,
            temperature=MODEL_TEMPERATURE,
            max_retries=2,
            timeout=120,
        )
    }


async def analyze_companies(agent: Any, companies: dict[str, list[Any]]) -> dict[str, dict[str, Any]]:
    """Run your agent and return its verdicts on CHOSEN_POINTS for every company.

    ``companies`` maps each company folder name to its four LangChain tools:

        list_documents()             names and dates of the company's documents
        read_document(name)          the text of one document
        sql_query(query)             one read-only SQLite query on company.db
        interview(role, topic)       one interview answer (INTERVIEW_LIMIT per company)

    Give the tools to a tool-calling agent, e.g.
    ``create_agent(model, tools=companies[name], system_prompt=...)``, or call
    them yourself with ``tool.invoke({...})``. The README explains the
    documents, the database tables and the interview roles and topics.

    Return one dictionary per company folder name, e.g. with
    CHOSEN_POINTS = [1, 7, 15]:

        {
            "hollins_group": {
                "points": {1: "PASS", 7: "FAIL", 15: "PASS"},
                "summary": "Two or three sentences explaining the verdicts.",
            },
            ...
        }

    Each chosen point needs a verdict ("PASS" or "FAIL"; True/False also
    works). Other points are ignored.

    Catch errors per company (e.g. a failed API call) and still return an
    entry for it: an exception here means no results.csv, which scores zero.
    You may analyse companies in parallel (e.g. ``asyncio.gather``).
    """
    ### YOUR CODE HERE
    llm = agent["model"] if isinstance(agent, dict) else agent
    semaphore = asyncio.Semaphore(MAX_WORKERS)

    async def run_one(name: str, tools: list[Any]) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            try:
                return name, await asyncio.to_thread(_analyze_one, llm, tools)
            except Exception as exc:  # never let one company crash the batch
                return name, {
                    "points": {point: "FAIL" for point in CHOSEN_POINTS},
                    "summary": f"Analysis failed for this company: {exc}",
                }

    gathered = await asyncio.gather(*(run_one(name, tools) for name, tools in companies.items()))
    return dict(gathered)


# Everything below is provided runner/scoring code. No edits are needed.
# Your code must read company data only through these tools: do not open
# company.db or interviews.dat directly.

INTERVIEWS_USED: dict[str, int] = {}


def company_folders(folder: Path) -> list[Path]:
    """Return one sub-folder per company directly inside *folder*, sorted by name."""
    return sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith("."))


def make_tools(company_folder: Path) -> list[Any]:
    """Build the four tools for one company."""
    from langchain_core.tools import tool

    name = company_folder.name
    db_uri = f"{(company_folder / 'company.db').resolve().as_uri()}?mode=ro"
    answers = json.loads(zlib.decompress((company_folder / "interviews.dat").read_bytes()))
    documents = {p.name: p for p in sorted(company_folder.glob("*.md"))}
    lock = threading.Lock()
    INTERVIEWS_USED[name] = 0

    @tool
    def list_documents() -> str:
        """List this company's documents (annual letter, earnings calls, news) with their dates."""
        lines = []
        for doc_name, path in documents.items():
            text = path.read_text(encoding="utf-8").splitlines()
            dated = next((line for line in text if line.startswith("Date:")), "")
            lines.append(f"{doc_name}: {text[0].lstrip('# ')} ({dated or 'undated'})")
        return "\n".join(lines)

    @tool
    def read_document(name: str) -> str:
        """Return the full text of one document named by list_documents, e.g. 'annual_letter_2025.md'."""
        path = documents.get(name.strip())
        if path is None:
            return f"No document named {name!r}. Available: {', '.join(documents)}"
        return path.read_text(encoding="utf-8")

    @tool
    def sql_query(query: str) -> str:
        """Run one read-only SQLite query on this company's database and return at most 50 rows.

        Amounts are in USD thousands. Dates are 'YYYY-MM-DD', months 'YYYY-MM', quarters like '2025Q3'.
        Tables:
          company(name, industry)
          industry_medians(industry, revenue_growth_2y_pct, new_product_share_pct, new_product_sales_per_rd, operating_margin_pct)
          orders(order_id, order_date, product, customer, amount, note)
          products(product, launch_date)
          monthly_costs(month, cost_of_sales, selling_admin, research_development)
          quarterly_cash(quarter, operating_cash_flow, capex, cash_end)
          capex_plan(year, planned_capex)
          returns(month, units_shipped, units_returned)
          insider_trades(trade_date, person, title, side, shares, price)
        Use SUM, GROUP BY and WHERE to compute totals rather than reading raw rows.
        """
        try:
            con = sqlite3.connect(db_uri, uri=True)
            try:
                cursor = con.execute(query)
                columns = [c[0] for c in cursor.description or []]
                rows = cursor.fetchmany(MAX_SQL_ROWS + 1)
            finally:
                con.close()
        except sqlite3.Error as exc:
            return f"SQL error: {exc}"
        if not columns:
            return "Query returned no columns."
        fmt = lambda v: f"{v:.2f}" if isinstance(v, float) else str(v)  # noqa: E731
        out = [" | ".join(columns)] + [" | ".join(fmt(v) for v in row) for row in rows[:MAX_SQL_ROWS]]
        if len(rows) > MAX_SQL_ROWS:
            out.append(f"... more rows not shown (limit {MAX_SQL_ROWS}); aggregate or add LIMIT/OFFSET.")
        if not rows:
            out.append("(no rows)")
        return "\n".join(out)

    @tool
    def interview(role: str, topic: str) -> str:
        """Interview one person about the company (at most 10 interviews per company).

        role: customer, current_employee, former_employee, competitor or supplier.
        topic: sales_team, working_conditions, top_management, decision_making or competitive_edge.
        People who would not know about a topic say so; the README lists who knows what.
        """
        role, topic = role.strip().lower(), topic.strip().lower()
        if role not in answers:
            return f"Unknown role {role!r}. Roles: {', '.join(answers)}. (Not counted.)"
        if topic not in answers[role]:
            return f"Unknown topic {topic!r}. Topics: {', '.join(answers[role])}. (Not counted.)"
        with lock:
            if INTERVIEWS_USED[name] >= INTERVIEW_LIMIT:
                return f"Interview limit reached: {INTERVIEW_LIMIT} interviews per company."
            INTERVIEWS_USED[name] += 1
        return answers[role][topic]

    return [list_documents, read_document, sql_query, interview]


def normalize_verdict(value: Any) -> str | None:
    """Accept PASS/FAIL (any case) or a boolean."""
    if isinstance(value, bool):
        return "PASS" if value else "FAIL"
    text = str(getattr(value, "content", value)).strip().upper()
    return text if text in VERDICTS else None


def check_chosen_points(chosen: Any) -> list[int]:
    """CHOSEN_POINTS must name 3 different points between 1 and 15."""
    try:
        points = sorted({int(p) for p in chosen})
    except (TypeError, ValueError):
        points = []
    if len(points) != 3 or len(chosen) != 3 or not all(p in FISHER_POINTS for p in points):
        raise SystemExit("Set CHOSEN_POINTS at the top of hw3.py to 3 different point numbers "
                         f"between 1 and 15, e.g. [1, 7, 15] (now: {chosen!r}).")
    return points


def normalize_points(points: Any, chosen: list[int]) -> dict[int, str | None]:
    """Map each chosen point number to PASS/FAIL, or None when missing/invalid."""
    points = points if isinstance(points, dict) else {}
    by_number: dict[int, Any] = {}
    for key, value in points.items():
        try:
            by_number[int(key)] = value
        except (TypeError, ValueError):
            continue
    return {p: normalize_verdict(by_number.get(p)) for p in chosen}


def read_ground_truth(folder: Path) -> dict[str, dict[str, Any]]:
    """Read the expected 15 point verdicts per company."""
    path = folder / "ground_truth.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def point_errors(points: dict[int, str | None], expected: dict[str, Any] | None) -> tuple[int, str]:
    """Count correct point verdicts and describe the wrong ones."""
    if expected is None:
        return 0, ""
    wrong = []
    for p in points:
        truth = expected["points"][str(p)]
        if points[p] != truth:
            wrong.append(f"{p}: expected {truth}, predicted {points[p] or 'missing'}")
    if all(v is None for v in points.values()):
        return 0, "all point verdicts missing"
    return len(points) - len(wrong), "; ".join(wrong)


def balanced_point_accuracy(
    analyses: dict[str, Any], truth: dict[str, dict[str, Any]], names: list[str], chosen: list[int]
) -> float:
    """Mean of the accuracy on true-PASS points and on true-FAIL points.

    Most verdicts are PASS, so answering PASS everywhere gets a high plain
    accuracy but only 50% balanced accuracy. Grading uses the balanced number.
    """
    hits = {"PASS": [0, 0], "FAIL": [0, 0]}
    for name in names:
        if name not in truth:
            continue
        entry = analyses.get(name)
        points = normalize_points(entry.get("points") if isinstance(entry, dict) else None, chosen)
        for p in chosen:
            expected = truth[name]["points"][str(p)]
            hits[expected][0] += points[p] == expected
            hits[expected][1] += 1
    rates = [h / n for h, n in hits.values() if n]
    return sum(rates) / len(rates) if rates else 0.0


def write_results(
    names: list[str], analyses: dict[str, Any], truth: dict[str, dict[str, Any]], chosen: list[int]
) -> tuple[Path, int]:
    """Write results.csv and return the number of correct point verdicts."""
    output = Path("results.csv")
    correct_points = 0
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["company", "points", "points_correct", "point_errors", "interviews_used", "summary"])
        for name in names:
            entry = analyses.get(name)
            entry = entry if isinstance(entry, dict) else {}
            points = normalize_points(entry.get("points"), chosen)
            expected = truth.get(name)
            n_points, errors = point_errors(points, expected)
            correct_points += n_points
            writer.writerow([
                name,
                " ".join(f"{p}:{points[p] or 'missing'}" for p in chosen),
                f"{n_points}/{len(chosen)}" if expected else "",
                errors,
                INTERVIEWS_USED.get(name, 0),
                str(entry.get("summary", "")).strip(),
            ])
    return output, correct_points


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run FTEC5660 HW3 on company data folders")
    parser.add_argument(
        "--data-folder",
        required=True,
        type=Path,
        help="folder containing one sub-folder per company",
    )
    return parser.parse_args()


async def run(folder: Path) -> int:
    folders = company_folders(folder)
    if not folders:
        raise SystemExit(f"no company folders found in {folder}")

    chosen = check_chosen_points(CHOSEN_POINTS)
    load_env_file()
    companies = {path.name: make_tools(path) for path in folders}
    agent = build_agent()
    analyses = await analyze_companies(agent, companies)
    if not isinstance(analyses, dict):
        raise TypeError("analyze_companies() must return a dictionary")

    truth = read_ground_truth(folder)
    output, correct_points = write_results(list(companies), analyses, truth, chosen)
    summary = f" Your points: {', '.join(map(str, chosen))}."
    if truth:
        summary += (
            f" Point accuracy: {correct_points}/{len(companies) * len(chosen)}"
            f" (balanced: {balanced_point_accuracy(analyses, truth, list(companies), chosen):.1%})."
        )
    print(f"Processed {len(companies)} compan{'y' if len(companies) == 1 else 'ies'}. Wrote {output}.{summary}")
    return 0


def main() -> int:
    args = parse_args()
    if not args.data_folder.is_dir():
        raise SystemExit(f"not a folder: {args.data_folder}")
    return asyncio.run(run(args.data_folder))


if __name__ == "__main__":
    raise SystemExit(main())
