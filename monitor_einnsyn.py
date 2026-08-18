import json
import os
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin

import requests
from openai import OpenAI


BASE = "https://api.einnsyn.no"

KNOWN_CASE_ID = "sm_01j76gtv03f0vb4e6n6gfg45k9"
CASE_NUMBER = "2023/9221"
RME_M_ADMIN_ENHET = "enh_01j73r5z2cf6xvstqj4d6fteq4"

CASE_WEB_LINK = (
    "https://einnsyn.no/saksmappe?id="
    "http%3A%2F%2Fdata.einnsyn.no%2Fnoark4%2FSaksmappe--970205039--9221--2023"
)

LATEST_FILE = Path("latest_result.json")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
ALERT_TO = os.getenv("ALERT_TO")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

REQUEST_TIMEOUT = 30
LIMIT = 100


# Only changes to these CASE fields are considered material.
# "updated" is deliberately excluded because backend metadata/timestamps can
# change without a meaningful visible change to the case.
MATERIAL_CASE_FIELDS = [
    "case_number",
    "title",
    "published",
    "administrativ_enhet",
    "administrativ_enhet_id",
    "externalId",
]


# Only changes to these JOURNALPOST fields are considered material.
# "updated" is deliberately excluded for the same reason as above.
# The journalpost ID itself is used to identify added/removed items and is
# therefore not compared as a mutable field.
MATERIAL_JOURNALPOST_FIELDS = [
    "title",
    "published",
    "journal_date",
    "document_date",
    "journal_year",
    "journalpost_number",
    "journalpost_type",
    "externalId",
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
        "administrativ_enhet_id": (
            case.get("administrativEnhetObjekt") or case.get("journalenhet")
        ),
        "externalId": external_id,
        "api_link": (
            f"{BASE}/saksmappe/{case.get('id')}"
            if case.get("id")
            else None
        ),
        "web_link": CASE_WEB_LINK,
        # Keep raw data for audit/debugging only.
        # It is NOT used to decide whether an alert should be sent.
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
        "api_link": (
            f"{BASE}/journalpost/{jp.get('id')}"
            if jp.get("id")
            else None
        ),
        "web_link": make_web_link(entity, external_id),
        # Keep raw data for audit/debugging only.
        # It is NOT used to decide whether an alert should be sent.
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
            "case_web_link": CASE_WEB_LINK,
            "rme_m_administrativ_enhet": RME_M_ADMIN_ENHET,
            "method": (
                "Direct lookup by known Saksmappe ID, then fetch all "
                "journalposts for that case."
            ),
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


def load_previous_result() -> Optional[Dict[str, Any]]:
    if not LATEST_FILE.exists():
        return None

    with LATEST_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_latest_result(result: Dict[str, Any]) -> None:
    with LATEST_FILE.open("w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


def journalposts_by_id(result: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        jp["id"]: jp
        for jp in result.get("journalposts", [])
        if isinstance(jp, dict) and jp.get("id")
    }


def snapshot_fields(data: Dict[str, Any], fields: List[str]) -> Dict[str, Any]:
    return {field: data.get(field) for field in fields}


def field_existed_before(data: Dict[str, Any], field: str) -> bool:
    return field in data and data.get(field) is not None


def compare_fields(
    before_full: Dict[str, Any],
    after_full: Dict[str, Any],
    fields: List[str],
) -> Dict[str, Any]:
    """
    Compare only explicitly material fields.

    If a field did not exist in the old saved JSON but exists now, treat that
    as a monitor/schema capture change rather than a confirmed eInnsyn change.
    """
    material_changes: Dict[str, Any] = {}
    monitor_capture_changes: Dict[str, Any] = {}

    before = snapshot_fields(before_full, fields)
    after = snapshot_fields(after_full, fields)

    for field in fields:
        before_value = before.get(field)
        after_value = after.get(field)

        if before_value == after_value:
            continue

        if not field_existed_before(before_full, field) and after_value is not None:
            monitor_capture_changes[field] = {
                "before": before_value,
                "after": after_value,
                "interpretation": (
                    "This field appears to be newly captured by the monitor. "
                    "It is not treated as a confirmed eInnsyn change."
                ),
            }
            continue

        material_changes[field] = {
            "before": before_value,
            "after": after_value,
        }

    return {
        "material_changes": material_changes,
        "monitor_capture_changes": monitor_capture_changes,
    }


def detect_changes(
    previous: Dict[str, Any],
    current: Dict[str, Any],
) -> Dict[str, Any]:
    previous_case = previous.get("case") or {}
    current_case = current.get("case") or {}

    case_comparison = compare_fields(
        previous_case,
        current_case,
        MATERIAL_CASE_FIELDS,
    )

    case_field_changes = case_comparison["material_changes"]
    case_monitor_capture_changes = case_comparison["monitor_capture_changes"]

    previous_by_id = journalposts_by_id(previous)
    current_by_id = journalposts_by_id(current)

    previous_ids = set(previous_by_id)
    current_ids = set(current_by_id)

    added_ids = sorted(current_ids - previous_ids)
    removed_ids = sorted(previous_ids - current_ids)
    common_ids = sorted(previous_ids & current_ids)

    added = [
        snapshot_fields(current_by_id[jp_id], MATERIAL_JOURNALPOST_FIELDS + ["id"])
        for jp_id in added_ids
    ]

    removed = [
        snapshot_fields(previous_by_id[jp_id], MATERIAL_JOURNALPOST_FIELDS + ["id"])
        for jp_id in removed_ids
    ]

    changed_journalposts: List[Dict[str, Any]] = []
    journalpost_monitor_capture_changes: List[Dict[str, Any]] = []

    for jp_id in common_ids:
        before_full = previous_by_id[jp_id]
        after_full = current_by_id[jp_id]

        comparison = compare_fields(
            before_full,
            after_full,
            MATERIAL_JOURNALPOST_FIELDS,
        )

        material_changes = comparison["material_changes"]
        monitor_capture_changes = comparison["monitor_capture_changes"]

        if material_changes:
            changed_journalposts.append(
                {
                    "id": jp_id,
                    "journalpost_number": after_full.get("journalpost_number"),
                    "title": after_full.get("title"),
                    "changes": material_changes,
                    "web_link": after_full.get("web_link"),
                }
            )

        if monitor_capture_changes:
            journalpost_monitor_capture_changes.append(
                {
                    "id": jp_id,
                    "journalpost_number": after_full.get("journalpost_number"),
                    "title": after_full.get("title"),
                    "newly_captured_fields": monitor_capture_changes,
                    "web_link": after_full.get("web_link"),
                }
            )

    previous_count = previous.get("total_journalposts")
    current_count = current.get("total_journalposts")

    # Material means only a user-relevant case/journalpost change.
    # Raw API differences, checked timestamps and excluded metadata do NOT
    # trigger an alert.
    material = bool(
        case_field_changes
        or added
        or removed
        or changed_journalposts
        or previous_count != current_count
    )

    return {
        "material_change_detected": material,
        "previous_journalpost_count": previous_count,
        "current_journalpost_count": current_count,
        "case_id": KNOWN_CASE_ID,
        "case_number": CASE_NUMBER,
        "case_web_link": CASE_WEB_LINK,
        "case_field_changes": case_field_changes,
        "case_newly_monitored_fields_not_treated_as_material": (
            case_monitor_capture_changes
        ),
        "added_journalposts": added,
        "removed_journalposts": removed,
        "changed_visible_fields": changed_journalposts,
        "newly_monitored_fields_not_treated_as_material": (
            journalpost_monitor_capture_changes
        ),
    }


def make_non_llm_email_body(change_report: Dict[str, Any]) -> str:
    lines: List[str] = [
        "A material change was detected in the monitored eInnsyn case.",
        "",
        f"Case ID: {KNOWN_CASE_ID}",
        f"Case number: {CASE_NUMBER}",
        f"Case link: {CASE_WEB_LINK}",
        (
            "Previous journalpost count: "
            f"{change_report.get('previous_journalpost_count')}"
        ),
        (
            "Current journalpost count: "
            f"{change_report.get('current_journalpost_count')}"
        ),
        "",
    ]

    case_changes = change_report.get("case_field_changes") or {}

    if case_changes:
        lines.append("Changed case fields:")

        for field, values in case_changes.items():
            lines.append(f"- {field}:")
            lines.append(f"    Before: {values.get('before')}")
            lines.append(f"    Now: {values.get('after')}")

        lines.append("")

    if change_report.get("added_journalposts"):
        lines.append("Added journalposts:")

        for jp in change_report["added_journalposts"]:
            lines.extend(
                [
                    "- Before: not present in the previous saved result.",
                    (
                        "  Now: journalpost number "
                        f"{jp.get('journalpost_number')} is present."
                    ),
                    f"  Title: {jp.get('title')}",
                    f"  Journal date: {jp.get('journal_date')}",
                    f"  Document date: {jp.get('document_date')}",
                    f"  Published: {jp.get('published')}",
                    f"  Type: {jp.get('journalpost_type')}",
                    "",
                ]
            )

    if change_report.get("removed_journalposts"):
        lines.append("Removed journalposts:")

        for jp in change_report["removed_journalposts"]:
            lines.extend(
                [
                    (
                        "- Before: journalpost number "
                        f"{jp.get('journalpost_number')} was present."
                    ),
                    "  Now: it is no longer present.",
                    f"  Title: {jp.get('title')}",
                    f"  Journal date: {jp.get('journal_date')}",
                    f"  Document date: {jp.get('document_date')}",
                    f"  Published: {jp.get('published')}",
                    f"  Type: {jp.get('journalpost_type')}",
                    "",
                ]
            )

    if change_report.get("changed_visible_fields"):
        lines.append("Changed journalpost fields:")

        for change in change_report["changed_visible_fields"]:
            lines.extend(
                [
                    (
                        "- Journalpost number: "
                        f"{change.get('journalpost_number')}"
                    ),
                    f"  Title: {change.get('title')}",
                    f"  Journalpost link: {change.get('web_link')}",
                ]
            )

            for field, values in change.get("changes", {}).items():
                lines.append(f"  {field}:")
                lines.append(f"    Before: {values.get('before')}")
                lines.append(f"    Now: {values.get('after')}")

            lines.append("")

    return "\n".join(lines).strip()


def make_llm_prompt(change_report: Dict[str, Any]) -> str:
    return f"""
You are preparing an email alert for a monitored Norwegian eInnsyn case.

A material change has already been confirmed by the monitoring code.
Your task is only to explain that confirmed change clearly.

Important rules:
- Do not invent additional changes.
- Explain changes as "Before" and "Now" where possible.
- If a journalpost is newly added, say that it was not present in the previous saved result and is now present.
- If a journalpost was removed, say that it was present before and is no longer present.
- If a case field or journalpost field changed, explain exactly what changed, including the before and current values.
- Fields described as newly captured by the monitor are NOT confirmed eInnsyn changes and must not be presented as material.
- Ignore raw API differences, technical metadata, ordering changes and checked timestamps.
- Translate Norwegian text to English where useful, but keep the original Norwegian in parentheses for titles, legal bases and document types.
- Do not translate IDs, case numbers, URLs or legal section references.
- Use the case link as the main link.
- Do not exaggerate the significance of the change.

Required structure:
1. One-sentence conclusion.
2. Case details.
3. What changed, using Before / Now.
4. Practical note, if relevant.

Case context:
Case ID: {KNOWN_CASE_ID}
Case number: {CASE_NUMBER}
Organisation/unit: RME-M - Section for Market and System Operation (RME-M - Seksjon for marked og systemdrift)
Main case link: {CASE_WEB_LINK}

Confirmed material change report:
{json.dumps(change_report, indent=2, ensure_ascii=False)}

Output only the email body text.
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

    print(
        "OpenAI response did not contain output_text. "
        "Using non-LLM email summary."
    )
    return make_non_llm_email_body(change_report)


def parse_recipients(value: Optional[str]) -> List[str]:
    if not value:
        return []

    recipients: List[str] = []

    for part in value.replace(";", ",").split(","):
        email = part.strip()

        if email:
            recipients.append(email)

    return recipients


def send_email(subject: str, body: str) -> None:
    if not GMAIL_USER:
        raise RuntimeError("GMAIL_USER environment variable is missing.")

    if not GMAIL_APP_PASSWORD:
        raise RuntimeError(
            "GMAIL_APP_PASSWORD environment variable is missing."
        )

    recipients = parse_recipients(ALERT_TO)

    if not recipients:
        raise RuntimeError(
            "ALERT_TO environment variable is missing or empty."
        )

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

    change_report = detect_changes(previous_result, current_result)

    if not change_report["material_change_detected"]:
        print("No material change detected. No email will be sent.")
        print(
            "The latest JSON will still be updated so technical/non-material "
            "API differences do not keep reappearing."
        )
        save_latest_result(current_result)
        print(json.dumps(change_report, indent=2, ensure_ascii=False))
        return

    print("Material change detected. Preparing alert email.")
    print(json.dumps(change_report, indent=2, ensure_ascii=False))

    email_body = generate_llm_email_body(change_report)
    subject = f"Material eInnsyn change: RME-M {CASE_NUMBER}"

    # Save only after successful email delivery.
    # If sending fails, the old baseline remains in place and the next run
    # can retry the same material alert.
    send_email(subject, email_body)
    save_latest_result(current_result)

    print("Material-change alert sent.")
    print("latest_result.json updated.")


if __name__ == "__main__":
    main()
