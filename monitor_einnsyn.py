import json
import os
import smtplib
import sys
from copy import deepcopy
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin

import requests


BASE = "https://api.einnsyn.no"

# This is the eInnsyn internal Saksmappe ID for the RME-M case 2023/9221.
KNOWN_CASE_ID = "sm_01j76gtv03f0vb4e6n6gfg45k9"

OUTPUT_FILE = Path("latest_result.json")
LIMIT = 100
TIMEOUT = 30


def get_json(url: str, params: Optional[dict] = None) -> Any:
    response = requests.get(url, params=params, timeout=TIMEOUT)
    print(f"GET {response.url} -> {response.status_code}", file=sys.stderr)
    response.raise_for_status()
    return response.json()


def extract_items(data: Any) -> List[dict]:
    """
    eInnsyn endpoints may return lists directly or wrap lists in keys.
    This keeps the script tolerant.
    """
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("items", "results", "data"):
            if isinstance(data.get(key), list):
                return data[key]

        if isinstance(data.get("searchHits"), list):
            return [hit.get("source", hit) for hit in data["searchHits"]]

    return []


def web_link_for(entity: str, external_id: Optional[str]) -> Optional[str]:
    if not external_id:
        return None

    if entity == "Saksmappe":
        return "https://einnsyn.no/saksmappe?id=" + quote(external_id, safe="")

    if entity == "Journalpost":
        return "https://einnsyn.no/journalpost?id=" + quote(external_id, safe="")

    return None


def simplify_case(case: dict) -> dict:
    external_id = case.get("externalId")

    return {
        "id": case.get("id"),
        "entity": case.get("entity"),
        "title": case.get("offentligTittel") or case.get("title"),
        "case_number": case.get("saksnummer"),
        "published": case.get("publisertDato"),
        "updated": case.get("oppdatertDato"),
        "administrativ_enhet": case.get("administrativEnhet"),
        "administrativ_enhet_objekt": case.get("administrativEnhetObjekt"),
        "externalId": external_id,
        "api_link": f"{BASE}/saksmappe/{case.get('id')}" if case.get("id") else None,
        "web_link": web_link_for("Saksmappe", external_id),
        "raw": case,
    }


def simplify_journalpost(jp: dict) -> dict:
    external_id = jp.get("externalId")

    return {
        "id": jp.get("id"),
        "entity": jp.get("entity"),
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
        "web_link": web_link_for("Journalpost", external_id),
        "raw": jp,
    }


def fetch_case(case_id: str) -> dict:
    data = get_json(f"{BASE}/saksmappe/{case_id}")

    if not isinstance(data, dict):
        raise RuntimeError("Unexpected response from saksmappe endpoint.")

    return data


def fetch_case_journalposts(case_id: str) -> List[dict]:
    all_items: List[dict] = []

    next_url = f"{BASE}/saksmappe/{case_id}/journalpost"
    params = {
        "limit": LIMIT,
        "sortBy": "publisertDato",
        "sortOrder": "desc",
    }

    while next_url:
        data = get_json(next_url, params=params)
        params = None

        items = extract_items(data)
        all_items.extend(items)

        if isinstance(data, dict):
            next_url = data.get("next")
            if next_url and next_url.startswith("/"):
                next_url = urljoin(BASE, next_url)
        else:
            next_url = None

    return all_items


def build_result() -> dict:
    case_raw = fetch_case(KNOWN_CASE_ID)
    case = simplify_case(case_raw)

    journalposts_raw = fetch_case_journalposts(KNOWN_CASE_ID)
    journalposts = [simplify_journalpost(jp) for jp in journalposts_raw]

    journalposts.sort(
        key=lambda item: (
            item.get("published") or "",
            str(item.get("journalpost_number") or ""),
            item.get("id") or "",
        ),
        reverse=True,
    )

    result = {
        "monitor": {
            "name": "RME-M 2023/9221 eInnsyn monitor",
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "case_id": KNOWN_CASE_ID,
            "case_number": case.get("case_number"),
            "method": "known_saksmappe_id_only",
            "filters_used": {
                "known_case_id": KNOWN_CASE_ID,
            },
            "filters_not_used": [
                "searchTerm",
                "state",
                "stat0",
                "saved_search_id",
                "journalenhet",
                "CASE_TITLE",
            ],
        },
        "case": case,
        "total_journalposts": len(journalposts),
        "journalposts": journalposts,
    }

    return result


def comparable_result(result: dict) -> dict:
    """
    Remove volatile fields before comparing.

    checked_at_utc changes every run, so it must not trigger alerts.
    """
    clean = deepcopy(result)

    if isinstance(clean.get("monitor"), dict):
        clean["monitor"].pop("checked_at_utc", None)

    return clean


def load_previous_result() -> Optional[dict]:
    if not OUTPUT_FILE.exists():
        return None

    with OUTPUT_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_result(result: dict) -> None:
    with OUTPUT_FILE.open("w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


def split_recipients(value: str) -> List[str]:
    if not value:
        return []

    normalized = value.replace(";", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def make_change_summary(previous: Optional[dict], current: dict) -> str:
    if previous is None:
        return "No previous latest_result.json existed. A baseline has been created."

    previous_posts = previous.get("journalposts", []) if isinstance(previous, dict) else []
    current_posts = current.get("journalposts", [])

    previous_ids = {item.get("id") for item in previous_posts if isinstance(item, dict)}
    current_ids = {item.get("id") for item in current_posts if isinstance(item, dict)}

    added_ids = sorted(current_ids - previous_ids)
    removed_ids = sorted(previous_ids - current_ids)

    lines = [
        "The monitored eInnsyn result has changed.",
        "",
        f"Case ID: {KNOWN_CASE_ID}",
        f"Case number: {current.get('case', {}).get('case_number')}",
        f"Previous journalpost count: {len(previous_posts)}",
        f"Current journalpost count: {len(current_posts)}",
        "",
    ]

    if added_ids:
        lines.append("Added journalposts:")
        for item in current_posts:
            if item.get("id") in added_ids:
                lines.append(
                    f"- {item.get('id')} | no. {item.get('journalpost_number')} | "
                    f"{item.get('published')} | {item.get('title')} | {item.get('web_link')}"
                )
        lines.append("")

    if removed_ids:
        lines.append("Removed journalposts:")
        for item in previous_posts:
            if item.get("id") in removed_ids:
                lines.append(
                    f"- {item.get('id')} | no. {item.get('journalpost_number')} | "
                    f"{item.get('published')} | {item.get('title')} | {item.get('web_link')}"
                )
        lines.append("")

    if not added_ids and not removed_ids:
        lines.append("No added or removed journalpost IDs were detected.")
        lines.append("The change is likely in metadata, titles, dates, shielding information, or raw fields.")

    return "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    gmail_user = os.environ.get("GMAIL_USER", "").strip()
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    alert_to_raw = os.environ.get("ALERT_TO", "").strip()

    recipients = split_recipients(alert_to_raw)

    if not gmail_user or not gmail_app_password or not recipients:
        print(
            "Email not sent because GMAIL_USER, GMAIL_APP_PASSWORD, or ALERT_TO is missing.",
            file=sys.stderr,
        )
        return

    msg = EmailMessage()
    msg["From"] = gmail_user
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(gmail_user, gmail_app_password)
        smtp.send_message(msg)

    print(f"Email sent to: {', '.join(recipients)}", file=sys.stderr)


def main() -> None:
    previous_result = load_previous_result()
    current_result = build_result()

    previous_comparable = comparable_result(previous_result) if previous_result else None
    current_comparable = comparable_result(current_result)

    changed = previous_comparable != current_comparable

    if previous_result is None:
        print("No previous JSON found. Creating baseline only.", file=sys.stderr)
    elif changed:
        print("Change detected.", file=sys.stderr)
        summary = make_change_summary(previous_result, current_result)
        send_email(
            subject="eInnsyn monitor changed: RME-M 2023/9221",
            body=summary,
        )
    else:
        print("No change detected.", file=sys.stderr)

    save_result(current_result)

    print(json.dumps(current_result, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
