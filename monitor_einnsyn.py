import json
import os
import smtplib
from copy import deepcopy
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote, urljoin

import requests
from openai import OpenAI


BASE = "https://api.einnsyn.no"

KNOWN_CASE_ID = "sm_01j76gtv03f0vb4e6n6gfg45k9"
CASE_NUMBER = "2023/9221"
RME_M_ADMIN_ENHET = "enh_01j73r5z2cf6xvstqj4d6fteq4"

LATEST_FILE = Path("latest_result.json")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
ALERT_TO = os.getenv("ALERT_TO")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

REQUEST_TIMEOUT = 30
LIMIT = 100


VISIBLE_JOURNALPOST_FIELDS = [
    "id",
    "title",
    "published",
    "journal_date",
    "document_date",
    "journalpost_number",
    "journalpost_type",
    "externalId",
    "web_link",
    "shielding",
    "legal_basis",
]


def get_json(url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    print(f"GET {response.url} -> {response.status_code}")
    response.raise_for_status()
    return response.json()


def extract_items(data: Any) -> List[Dict[str, Any]]:
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

    encoded = quote(external_id, safe="")

    if entity == "Saksmappe":
        return f"https://einnsyn.no/saksmappe?id={encoded}"

    if entity == "Journalpost":
        return f"https://einnsyn.no/journalpost?id={encoded}"

    return None


def simplify_case(case: Dict[str, Any]) -> Dict[str, Any]:
    external_id = case.get("externalId")
    entity = case.get("entity") or "Saksmappe"

    return {
        "id": case.get("id"),
        "entity": entity,
        "case_number": case.get("saksnummer"),
        "title": case.get("offentligTittel") or case.get("title"),
        "published": case.get("publisertDato"),
        "updated": case.get("oppdatertDato"),
        "administrativ_enhet": case.get("administrativEnhet"),
        "administrativ_enhet_id": case.get("administrativEnhetObjekt") or case.get("journalenhet"),
        "externalId": external_id,
        "api_link": f"{BASE}/saksmappe/{case.get('id')}" if case.get("id") else None,
        "web_link": make_web_link(entity, external_id),
        "raw": case,
    }


def simplify_journalpost(jp: Dict[str, Any]) -> Dict[str, Any]:
    external_id = jp.get("externalId")
    entity = jp.get("entity") or "Journalpost"

    shielding = jp.get("skjerming")
    legal_basis = None

    if isinstance(shielding, dict):
        legal_basis = shielding.get("skjermingshjemmel")
        shielding_value = shielding.get("id")
    else:
        shielding_value = shielding

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
        "shielding": shielding_value,
        "legal_basis": legal_basis,
        "api_link": f"{BASE}/journalpost/{jp.get('id')}" if jp.get("id") else None,
        "web_link": make_web_link(entity, external_id),
        "raw": jp,
    }


def fetch_case_by_known_id() -> Dict[str, Any]:
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
    case_raw = fetch_case_by_known_id()
    case = simplify_case(case_raw)

    journalposts_raw = fetch_case_journalposts(KNOWN_CASE_ID)
    journalposts = [simplify_journalpost(jp) for jp in journalposts_raw]

    journalposts.sort(
        key=lambda x: (
            str(x.get("published") or ""),
            str(x.get("journalpost_number") or ""),
            str(x.get("id") or ""),
        ),
        reverse=True,
    )

    return {
        "monitor": {
            "name": "RME-M 2023/9221 eInnsyn monitor",
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "case_id": KNOWN_CASE_ID,
            "case_number": CASE_NUMBER,
            "rme_m_administrativ_enhet": RME_M_ADMIN_ENHET,
            "method": "Direct lookup by known Saksmappe ID, then fetch all journalposts for that case.",
            "filters_used": {
                "case_id": KNOWN_CASE_ID,
            },
            "filters_not_used": [
                "state",
                "stat0",
                "journalenhet",
                "searchTerm",
                "saved_search_id",
                "title",
                "CASE_TITLE",
            ],
        },
        "case": case,
        "total_journalposts": len(journalposts),
        "journalposts": journalposts,
    }


def canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_for_comparison(data: Dict[str, Any]) -> Dict[str, Any]:
    stable = deepcopy(data)

    if "monitor" in stable:
        stable["monitor"].pop("checked_at_utc", None)

    return stable


def load_previous_result() -> Optional[Dict[str, Any]]:
    if not LATEST_FILE.exists():
        return None

    with LATEST_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_latest_result(result: Dict[str, Any]) -> None:
    with LATEST_FILE.open("w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


def journalpost_ids(result: Dict[str, Any]) -> Set[str]:
    return {
        jp["id"]
        for jp in result.get("journalposts", [])
        if isinstance(jp, dict) and jp.get("id")
    }


def journalposts_by_id(result: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        jp["id"]: jp
        for jp in result.get("journalposts", [])
        if isinstance(jp, dict) and jp.get("id")
    }


def visible_journalpost_snapshot(jp: Dict[str, Any]) -> Dict[str, Any]:
    return {field: jp.get(field) for field in VISIBLE_JOURNALPOST_FIELDS}


def detect_changes(previous: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    previous_by_id = journalposts_by_id(previous)
    current_by_id = journalposts_by_id(current)

    previous_ids = set(previous_by_id)
    current_ids = set(current_by_id)

    added_ids = sorted(current_ids - previous_ids)
    removed_ids = sorted(previous_ids - current_ids)
    common_ids = sorted(previous_ids & current_ids)

    added = [visible_journalpost_snapshot(current_by_id[jp_id]) for jp_id in added_ids]
    removed = [visible_journalpost_snapshot(previous_by_id[jp_id]) for jp_id in removed_ids]

    changed_visible_fields = []

    for jp_id in common_ids:
        before = visible_journalpost_snapshot(previous_by_id[jp_id])
        after = visible_journalpost_snapshot(current_by_id[jp_id])

        field_changes = {}

        for field in VISIBLE_JOURNALPOST_FIELDS:
            if before.get(field) != after.get(field):
                field_changes[field] = {
                    "before": before.get(field),
                    "after": after.get(field),
                }

        if field_changes:
            changed_visible_fields.append(
                {
                    "id": jp_id,
                    "journalpost_number": after.get("journalpost_number"),
                    "title": after.get("title"),
                    "changes": field_changes,
                    "web_link": after.get("web_link"),
                }
            )

    previous_count = previous.get("total_journalposts")
    current_count = current.get("total_journalposts")

    material = bool(
        added
        or removed
        or changed_visible_fields
        or previous_count != current_count
    )

    return {
        "material_change_detected": material,
        "previous_journalpost_count": previous_count,
        "current_journalpost_count": current_count,
        "added_journalposts": added,
        "removed_journalposts": removed,
        "changed_visible_fields": changed_visible_fields,
    }


def make_non_llm_email_body(change_report: Dict[str, Any]) -> str:
    lines = []

    if change_report["material_change_detected"]:
        lines.append("A material change was detected in the monitored eInnsyn case.")
    else:
        lines.append("No material change was detected in the monitored eInnsyn case.")

    lines.extend(
        [
            "",
            f"Case ID: {KNOWN_CASE_ID}",
            f"Case number: {CASE_NUMBER}",
            f"Previous journalpost count: {change_report.get('previous_journalpost_count')}",
            f"Current journalpost count: {change_report.get('current_journalpost_count')}",
            "",
        ]
    )

    if change_report.get("added_journalposts"):
        lines.append("Added journalposts:")
        for jp in change_report["added_journalposts"]:
            lines.extend(
                [
                    f"- Journalpost number: {jp.get('journalpost_number')}",
                    f"  Title: {jp.get('title')}",
                    f"  Journal date: {jp.get('journal_date')}",
                    f"  Document date: {jp.get('document_date')}",
                    f"  Published: {jp.get('published')}",
                    f"  Type: {jp.get('journalpost_type')}",
                    f"  Link: {jp.get('web_link')}",
                    "",
                ]
            )

    if change_report.get("removed_journalposts"):
        lines.append("Removed journalposts:")
        for jp in change_report["removed_journalposts"]:
            lines.extend(
                [
                    f"- Journalpost ID: {jp.get('id')}",
                    f"  Journalpost number: {jp.get('journalpost_number')}",
                    f"  Title: {jp.get('title')}",
                    f"  Journal date: {jp.get('journal_date')}",
                    f"  Document date: {jp.get('document_date')}",
                    f"  Published: {jp.get('published')}",
                    f"  Type: {jp.get('journalpost_type')}",
                    f"  Link: {jp.get('web_link')}",
                    "",
                ]
            )

    if change_report.get("changed_visible_fields"):
        lines.append("Changed visible fields:")
        for change in change_report["changed_visible_fields"]:
            lines.extend(
                [
                    f"- Journalpost number: {change.get('journalpost_number')}",
                    f"  Title: {change.get('title')}",
                    f"  Link: {change.get('web_link')}",
                ]
            )

            for field, values in change.get("changes", {}).items():
                lines.append(f"  {field}: {values.get('before')} -> {values.get('after')}")

            lines.append("")

    if not change_report["material_change_detected"]:
        lines.append(
            "The stored JSON changed, but no added, removed, or visibly changed journalposts were detected."
        )

    return "\n".join(lines).strip()


def make_llm_prompt(change_report: Dict[str, Any]) -> str:
    return f"""
You are preparing an email alert for a monitored Norwegian eInnsyn case.

The recipient does not need a technical JSON diff.
The recipient needs to know whether there has been a material change.

Definition of material change:
- A new journalpost/document was added.
- A journalpost/document was removed.
- The total number of journalposts changed.
- A visible field changed in a way that affects the understanding of the case, such as title, journal date, document date, published date, document type, shielding/legal basis, or sender/recipient information.

Definition of non-material change:
- Only technical metadata changed.
- Only the checked timestamp changed.
- Raw API formatting changed without any visible change to the case or journalposts.
- API ordering changed without added, removed, or visibly changed journalposts.

Write the email body in clear plain English.

Rules:
- Start with one clear sentence saying whether a material change was detected.
- If new journalposts were added, explain what was added.
- If journalposts were removed, explain what was removed.
- If visible fields changed, summarize only the meaningful visible changes.
- Do not describe irrelevant technical JSON differences.
- Do not mention unchanged fields.
- Do not invent facts.
- Keep it concise but useful.
- Output only the email body text.

Case context:
Case ID: {KNOWN_CASE_ID}
Case number: {CASE_NUMBER}
Organisation/unit: RME-M - Seksjon for marked og systemdrift

Detected change report:
{json.dumps(change_report, indent=2, ensure_ascii=False)}
""".strip()


def generate_llm_email_body(change_report: Dict[str, Any]) -> str:
    if not OPENAI_API_KEY:
        print("OPENAI_API_KEY is not set. Using non-LLM email summary.")
        return make_non_llm_email_body(change_report)

    client = OpenAI(api_key=OPENAI_API_KEY)

    response = client.responses.create(
        model=OPENAI_MODEL,
        input=make_llm_prompt(change_report),
    )

    text = getattr(response, "output_text", None)

    if text and text.strip():
        return text.strip()

    print("OpenAI response did not contain output_text. Using non-LLM email summary.")
    return make_non_llm_email_body(change_report)


def parse_recipients(value: Optional[str]) -> List[str]:
    if not value:
        return []

    recipients = []

    for part in value.replace(";", ",").split(","):
        email = part.strip()
        if email:
            recipients.append(email)

    return recipients


def send_email(subject: str, body: str) -> None:
    if not GMAIL_USER:
        raise RuntimeError("GMAIL_USER environment variable is missing.")

    if not GMAIL_APP_PASSWORD:
        raise RuntimeError("GMAIL_APP_PASSWORD environment variable is missing.")

    recipients = parse_recipients(ALERT_TO)

    if not recipients:
        raise RuntimeError("ALERT_TO environment variable is missing or empty.")

    password = GMAIL_APP_PASSWORD.replace(" ", "")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = ", ".join(recipients)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, password)
        server.sendmail(GMAIL_USER, recipients, msg.as_string())

    print(f"Alert email sent to: {', '.join(recipients)}")


def main() -> None:
    current_result = build_result()
    previous_result = load_previous_result()

    if previous_result is None:
        print("No previous JSON found. Creating baseline only.")
        save_latest_result(current_result)
        print(json.dumps(current_result, indent=2, ensure_ascii=False))
        return

    previous_stable = stable_for_comparison(previous_result)
    current_stable = stable_for_comparison(current_result)

    if canonical_json(previous_stable) == canonical_json(current_stable):
        print("No stable change detected. latest_result.json will not be updated.")
        print(json.dumps(current_result, indent=2, ensure_ascii=False))
        return

    print("Stable change detected. Preparing material-change report.")

    change_report = detect_changes(previous_stable, current_stable)

    email_body = generate_llm_email_body(change_report)

    subject_prefix = (
        "Material eInnsyn change"
        if change_report.get("material_change_detected")
        else "Non-material eInnsyn change"
    )

    subject = f"{subject_prefix}: RME-M {CASE_NUMBER}"

    send_email(subject, email_body)

    save_latest_result(current_result)

    print("latest_result.json updated.")
    print(json.dumps(current_result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
