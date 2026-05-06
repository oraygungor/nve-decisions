import os
import json
import smtplib
import difflib
from pathlib import Path
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

import requests


BASE_URL = "https://api.einnsyn.no"

SEARCH_TERMS = [
    '"2023/09221"',
    '"2023/9221"',
    "2023/09221",
    "2023/9221",
]

ADMINISTRATIV_ENHET = "enh_01j73r5z2cf6xvstqj4d6fteq4"

OUTPUT_DIR = Path("output")
CURRENT_FILE = OUTPUT_DIR / "einnsyn_current.json"
PREVIOUS_FILE = OUTPUT_DIR / "einnsyn_previous.json"


def get_json(url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    response = requests.get(
        url,
        params=params,
        timeout=30,
        headers={
            "Accept": "application/json",
            "User-Agent": "einnsyn-monitor/1.0",
        },
    )
    response.raise_for_status()
    return response.json()


def normalize_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": item.get("id"),
        "entity": item.get("entity"),
        "title": item.get("offentligTittel"),
        "published": item.get("publisertDato"),
        "updated": item.get("oppdatertDato"),
        "journal_date": item.get("journaldato"),
        "document_date": item.get("dokumentetsDato"),
        "case_number": item.get("saksnummer"),
        "journalpost_number": item.get("journalpostnummer"),
        "journalpost_type": item.get("journalposttype"),
        "saksmappe": item.get("saksmappe"),
        "externalId": item.get("externalId"),
        "api_link": build_api_link(item),
        "web_link": build_web_link(item),
    }


def build_api_link(item: Dict[str, Any]) -> Optional[str]:
    entity = item.get("entity")
    item_id = item.get("id")

    if not entity or not item_id:
        return None

    if entity == "Journalpost":
        return f"{BASE_URL}/journalpost/{item_id}"

    if entity == "Saksmappe":
        return f"{BASE_URL}/saksmappe/{item_id}"

    return None


def build_web_link(item: Dict[str, Any]) -> Optional[str]:
    entity = item.get("entity")
    external_id = item.get("externalId")

    if not entity or not external_id:
        return None

    from urllib.parse import quote

    encoded_external_id = quote(external_id, safe="")

    if entity == "Journalpost":
        return f"https://einnsyn.no/journalpost?id={encoded_external_id}"

    if entity == "Saksmappe":
        return f"https://einnsyn.no/saksmappe?id={encoded_external_id}"

    return None


def find_case() -> Dict[str, Any]:
    for search_term in SEARCH_TERMS:
        params = {
            "query": search_term,
            "limit": 100,
            "sortBy": "publisertDato",
            "sortOrder": "desc",
            "administrativEnhet": ADMINISTRATIV_ENHET,
        }

        data = get_json(f"{BASE_URL}/search", params=params)

        results = data.get("results", [])
        for item in results:
            if item.get("entity") == "Saksmappe":
                case_number = item.get("saksnummer")
                external_id = item.get("externalId", "")

                if case_number == "2023/9221" or "Saksmappe--970205039--9221--2023" in external_id:
                    return item

    raise RuntimeError("Could not find the target Saksmappe for case 2023/9221.")


def fetch_case_journalposts(case_id: str) -> List[Dict[str, Any]]:
    params = {
        "limit": 100,
        "sortBy": "publisertDato",
        "sortOrder": "desc",
    }

    data = get_json(f"{BASE_URL}/saksmappe/{case_id}/journalpost", params=params)

    results = data.get("results", [])

    return [normalize_item(item) for item in results]


def build_result() -> Dict[str, Any]:
    case = find_case()
    case_id = case["id"]

    journalposts = fetch_case_journalposts(case_id)

    return {
        "monitor": {
            "name": "eInnsyn NVE case monitor",
            "search_terms": SEARCH_TERMS,
            "administrativ_enhet": ADMINISTRATIV_ENHET,
            "target_case_number": "2023/9221",
        },
        "case": normalize_item(case),
        "total_journalposts": len(journalposts),
        "journalposts": journalposts,
    }


def load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)


def json_for_compare(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def create_diff(old: Dict[str, Any], new: Dict[str, Any]) -> str:
    old_text = json_for_compare(old).splitlines()
    new_text = json_for_compare(new).splitlines()

    diff = difflib.unified_diff(
        old_text,
        new_text,
        fromfile="previous",
        tofile="current",
        lineterm="",
    )

    diff_text = "\n".join(diff)

    if len(diff_text) > 15000:
        return diff_text[:15000] + "\n\n[Diff truncated]"

    return diff_text


def send_email_alert(subject: str, body: str) -> None:
    gmail_user = os.environ["GMAIL_USER"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    alert_to_raw = os.environ["ALERT_TO"]

    recipients = [
        email.strip()
        for email in alert_to_raw.split(",")
        if email.strip()
    ]

    if not recipients:
        raise RuntimeError("ALERT_TO is empty. Add at least one recipient email.")

    msg = EmailMessage()
    msg["From"] = gmail_user
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(gmail_user, gmail_app_password)
        smtp.send_message(msg)


def main() -> None:
    current_result = build_result()

    previous_result = load_json(PREVIOUS_FILE)

    changed = previous_result is not None and previous_result != current_result
    first_run = previous_result is None

    output = {
        "changed": changed,
        "first_run": first_run,
        "result": current_result,
    }

    save_json(CURRENT_FILE, output)

    if first_run:
        save_json(PREVIOUS_FILE, current_result)

    elif changed:
        diff = create_diff(previous_result, current_result)

        subject = "eInnsyn search changed: NVE case 2023/9221"

        body = f"""The monitored eInnsyn search has changed.

Case: 2023/9221
Organisation/Admin unit: Norwegian Water Resources and Energy Directorate / {ADMINISTRATIV_ENHET}

Summary:
Previous journalposts: {previous_result.get("total_journalposts")}
Current journalposts: {current_result.get("total_journalposts")}

Diff:
{diff}
"""

        send_email_alert(subject, body)

        save_json(PREVIOUS_FILE, current_result)

    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
