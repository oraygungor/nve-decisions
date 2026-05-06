import json
import os
import smtplib
import sys
from copy import deepcopy
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin

import requests

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


BASE = "https://api.einnsyn.no"

KNOWN_CASE_ID = "sm_01j76gtv03f0vb4e6n6gfg45k9"
CASE_NUMBER = "2023/9221"
RME_M_ADMIN_ENHET = "enh_01j73r5z2cf6xvstqj4d6fteq4"

OUTPUT_FILE = Path("latest_result.json")
LIMIT = 100

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")


def get_json(url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    response = requests.get(url, params=params, timeout=30)
    print(f"GET {response.url} -> {response.status_code}")
    response.raise_for_status()
    return response.json()


def extract_items(data: Any) -> List[Dict[str, Any]]:
    """
    eInnsyn responses may use different wrappers depending on endpoint.
    This keeps the script tolerant.
    """
    if isinstance(data, dict):
        if isinstance(data.get("items"), list):
            return data["items"]
        if isinstance(data.get("results"), list):
            return data["results"]
        if isinstance(data.get("searchHits"), list):
            return [x.get("source", x) for x in data["searchHits"]]
    if isinstance(data, list):
        return data
    return []


def make_web_link(entity: str, external_id: Optional[str]) -> Optional[str]:
    if not external_id:
        return None

    if entity == "Saksmappe":
        return "https://einnsyn.no/saksmappe?id=" + quote(external_id, safe="")

    if entity == "Journalpost":
        return "https://einnsyn.no/journalpost?id=" + quote(external_id, safe="")

    return None


def simplify_case(case: Dict[str, Any]) -> Dict[str, Any]:
    external_id = case.get("externalId")
    entity = case.get("entity") or "Saksmappe"

    return {
        "id": case.get("id"),
        "entity": entity,
        "title": case.get("offentligTittel") or case.get("title"),
        "case_number": case.get("saksnummer"),
        "published": case.get("publisertDato"),
        "updated": case.get("oppdatertDato"),
        "administrativ_enhet": case.get("administrativEnhet"),
        "administrativ_enhet_objekt": case.get("administrativEnhetObjekt"),
        "externalId": external_id,
        "api_link": f"{BASE}/saksmappe/{case.get('id')}" if case.get("id") else None,
        "web_link": make_web_link("Saksmappe", external_id),
        "raw": case,
    }


def simplify_journalpost(jp: Dict[str, Any]) -> Dict[str, Any]:
    external_id = jp.get("externalId")
    entity = jp.get("entity") or "Journalpost"

    return {
        "id": jp.get("id"),
        "entity": entity,
        "title": jp.get("offentligTittel") or jp.get("title"),
        "published": jp.get("publisertDato"),
        "updated": jp.get("oppdatertDato"),
        "journal_date": jp.get("journaldato"),
        "document_date": jp.get("dokumentetsDato"),
        "journal_year": jp.get("journalaar"),
        "journalpost_number": jp.get("journalpostnummer"),
        "journalpost_type": jp.get("journalposttype"),
        "saksmappe": jp.get("saksmappe"),
        "externalId": external_id,
        "api_link": f"{BASE}/journalpost/{jp.get('id')}" if jp.get("id") else None,
        "web_link": make_web_link("Journalpost", external_id),
        "raw": jp,
    }


def fetch_case() -> Dict[str, Any]:
    return get_json(f"{BASE}/saksmappe/{KNOWN_CASE_ID}")


def fetch_case_journalposts(case_id: str) -> List[Dict[str, Any]]:
    all_items: List[Dict[str, Any]] = []

    next_url = f"{BASE}/saksmappe/{case_id}/journalpost"
    params: Optional[Dict[str, Any]] = {
        "limit": LIMIT,
        "sortBy": "publisertDato",
        "sortOrder": "desc",
    }

    while next_url:
        data = get_json(next_url, params=params)
        params = None

        items = extract_items(data)
        all_items.extend(items)

        next_url = data.get("next") if isinstance(data, dict) else None
        if next_url and next_url.startswith("/"):
            next_url = urljoin(BASE, next_url)

    return all_items


def build_result() -> Dict[str, Any]:
    checked_at = datetime.now(timezone.utc).isoformat()

    case_raw = fetch_case()
    case = simplify_case(case_raw)

    if case.get("case_number") != CASE_NUMBER:
        raise RuntimeError(
            f"Known case ID returned unexpected case number: {case.get('case_number')}"
        )

    journalposts_raw = fetch_case_journalposts(KNOWN_CASE_ID)
    journalposts = [simplify_journalpost(jp) for jp in journalposts_raw]

    journalposts.sort(
        key=lambda x: (
            x.get("published") or "",
            str(x.get("journalpost_number") or ""),
            x.get("id") or "",
        ),
        reverse=True,
    )

    result = {
        "monitor": {
            "name": "RME-M 2023/9221 eInnsyn monitor",
            "checked_at_utc": checked_at,
            "case_id": KNOWN_CASE_ID,
            "case_number": CASE_NUMBER,
            "rme_m_administrativ_enhet": RME_M_ADMIN_ENHET,
            "method": "Direct lookup by known Saksmappe ID, then fetch journalposts belonging to the case.",
            "api_urls": {
                "case": f"{BASE}/saksmappe/{KNOWN_CASE_ID}",
                "journalposts": f"{BASE}/saksmappe/{KNOWN_CASE_ID}/journalpost?limit={LIMIT}&sortBy=publisertDato&sortOrder=desc",
            },
        },
        "case": case,
        "total_journalposts": len(journalposts),
        "journalposts": journalposts,
    }

    return result


def stable_for_comparison(result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Remove fields that change every run and should not trigger an alert.
    """
    stable = deepcopy(result)

    try:
        stable["monitor"].pop("checked_at_utc", None)
    except Exception:
        pass

    return stable


def canonical_json(data: Any) -> str:
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        default=str,
    )


def load_previous_result() -> Optional[Dict[str, Any]]:
    if not OUTPUT_FILE.exists():
        return None

    with OUTPUT_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_result(result: Dict[str, Any]) -> None:
    with OUTPUT_FILE.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")


def journalpost_ids(result: Dict[str, Any]) -> set:
    return {
        jp.get("id")
        for jp in result.get("journalposts", [])
        if jp.get("id")
    }


def basic_change_summary(previous: Dict[str, Any], current: Dict[str, Any]) -> str:
    previous_ids = journalpost_ids(previous)
    current_ids = journalpost_ids(current)

    added_ids = sorted(current_ids - previous_ids)
    removed_ids = sorted(previous_ids - current_ids)

    lines = [
        "The monitored eInnsyn result has changed.",
        "",
        f"Case ID: {KNOWN_CASE_ID}",
        f"Case number: {CASE_NUMBER}",
        f"Previous journalpost count: {previous.get('total_journalposts')}",
        f"Current journalpost count: {current.get('total_journalposts')}",
        "",
    ]

    if added_ids:
        lines.append("Added journalpost IDs:")
        lines.extend(f"- {x}" for x in added_ids)
        lines.append("")

    if removed_ids:
        lines.append("Removed journalpost IDs:")
        lines.extend(f"- {x}" for x in removed_ids)
        lines.append("")

    if not added_ids and not removed_ids:
        lines.append("No added or removed journalpost IDs were detected.")
        lines.append(
            "The change is likely in metadata, titles, dates, shielding information, or raw fields."
        )
        lines.append("")

    return "\n".join(lines)


def make_llm_prompt(previous: Dict[str, Any], current: Dict[str, Any]) -> str:
    previous_stable = stable_for_comparison(previous)
    current_stable = stable_for_comparison(current)

    return f"""
You are helping monitor a Norwegian eInnsyn case.

Task:
Compare the previous JSON and current JSON.
Write a clear email-ready explanation of what changed.

Requirements:
- Be specific.
- Mention whether journalposts were added, removed, or only changed in metadata.
- If counts are the same, explain that clearly.
- Identify changed journalpost numbers, dates, titles, shielding fields, or other relevant fields if visible.
- Do not invent facts.
- If a field is unclear, say that it changed but cannot determine why.
- Keep the tone professional and concise.
- Output only the email body text. Do not use markdown tables.

Context:
Case ID: {KNOWN_CASE_ID}
Case number: {CASE_NUMBER}
Organisation/unit: RME-M - Seksjon for marked og systemdrift

Previous JSON:
{canonical_json(previous_stable)}

Current JSON:
{canonical_json(current_stable)}
""".strip()


def summarize_change_with_openai(previous: Dict[str, Any], current: Dict[str, Any]) -> str:
    fallback = basic_change_summary(previous, current)

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return (
            fallback
            + "\nOpenAI summary was not generated because OPENAI_API_KEY is not set."
        )

    if OpenAI is None:
        return (
            fallback
            + "\nOpenAI summary was not generated because the openai package is not installed."
        )

    try:
        client = OpenAI(api_key=api_key)

        response = client.responses.create(
            model=OPENAI_MODEL,
            input=make_llm_prompt(previous, current),
        )

        text = getattr(response, "output_text", None)
        if text and text.strip():
            return text.strip()

        return fallback + "\nOpenAI returned no summary text."

    except Exception as exc:
        return fallback + f"\nOpenAI summary failed: {exc}"


def split_recipients(raw: str) -> List[str]:
    if not raw:
        return []

    cleaned = raw.replace(";", ",")
    return [x.strip() for x in cleaned.split(",") if x.strip()]


def send_email(subject: str, body: str) -> None:
    gmail_user = os.getenv("GMAIL_USER")
    gmail_app_password = os.getenv("GMAIL_APP_PASSWORD")
    alert_to_raw = os.getenv("ALERT_TO", "")

    recipients = split_recipients(alert_to_raw)

    if not gmail_user:
        raise RuntimeError("GMAIL_USER is not set.")

    if not gmail_app_password:
        raise RuntimeError("GMAIL_APP_PASSWORD is not set.")

    if not recipients:
        raise RuntimeError("ALERT_TO is not set or contains no recipients.")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = ", ".join(recipients)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_app_password)
        server.sendmail(gmail_user, recipients, msg.as_string())

    print(f"Email sent to: {', '.join(recipients)}")


def main() -> None:
    print("Building current eInnsyn result...")
    current_result = build_result()

    previous_result = load_previous_result()

    if previous_result is None:
        print("No previous JSON found. Creating baseline only.")
        save_result(current_result)
        print(canonical_json(current_result))
        return

    previous_stable = stable_for_comparison(previous_result)
    current_stable = stable_for_comparison(current_result)

    if canonical_json(previous_stable) == canonical_json(current_stable):
        print("No meaningful change detected. latest_result.json was not overwritten.")
        return

    print("Meaningful change detected.")

    summary = summarize_change_with_openai(previous_result, current_result)

    subject = f"eInnsyn monitor changed: {CASE_NUMBER}"
    send_email(subject, summary)

    save_result(current_result)

    print("latest_result.json updated.")
    print("\nEMAIL SUMMARY")
    print(summary)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
