import os
import json
import smtplib
import hashlib
import requests
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.parse import urljoin, quote


BASE = "https://api.einnsyn.no"

# RME-M - Seksjon for marked og systemdrift
RME_M_ADMIN_ENHET = "enh_01j73r5z2cf6xvstqj4d6fteq4"

# Known Saksmappe ID for case 2023/9221.
# This is NOT RME-M. This is the specific case folder.
KNOWN_CASE_ID = "sm_01j76gtv03f0vb4e6n6gfg45k9"

CASE_NUMBER = "2023/9221"

SEARCH_TERMS = [
    '"2023/9221"',
    '"2023/09221"',
    "2023/9221",
    "2023/09221",
]

LIMIT = 100
STATE_FILE = "einnsyn_last.json"


def get_json(url, params=None):
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def extract_items(data):
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


def normalize_entity(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def make_web_link(entity, external_id):
    if not external_id:
        return None

    if entity == "Saksmappe":
        return "https://einnsyn.no/saksmappe?id=" + quote(external_id, safe="")

    if entity == "Journalpost":
        return "https://einnsyn.no/journalpost?id=" + quote(external_id, safe="")

    return None


def simplify_case(case):
    entity = normalize_entity(case.get("entity") or case.get("type"))

    return {
        "id": case.get("id"),
        "entity": entity,
        "title": case.get("offentligTittel") or case.get("title"),
        "case_number": case.get("saksnummer"),
        "published": case.get("publisertDato"),
        "updated": case.get("oppdatertDato"),
        "administrativ_enhet": case.get("administrativEnhet"),
        "administrativ_enhet_object": case.get("administrativEnhetObjekt"),
        "externalId": case.get("externalId"),
        "api_link": f"{BASE}/saksmappe/{case.get('id')}" if case.get("id") else None,
        "web_link": make_web_link("Saksmappe", case.get("externalId")),
        "raw": case,
    }


def simplify_journalpost(jp):
    entity = normalize_entity(jp.get("entity") or jp.get("type"))

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
        "externalId": jp.get("externalId"),
        "api_link": f"{BASE}/journalpost/{jp.get('id')}" if jp.get("id") else None,
        "web_link": make_web_link("Journalpost", jp.get("externalId")),
        "raw": jp,
    }


def find_case_by_search():
    for term in SEARCH_TERMS:
        params = {
            "query": term,
            "limit": LIMIT,
            "sortBy": "publisertDato",
            "sortOrder": "desc",
            "administrativEnhet": RME_M_ADMIN_ENHET,
        }

        data = get_json(f"{BASE}/search", params=params)
        items = extract_items(data)

        for item in items:
            entity = normalize_entity(item.get("entity") or item.get("type"))
            case_number = item.get("saksnummer")
            admin_enhet = item.get("administrativEnhetObjekt") or item.get("journalenhet")

            if (
                entity == "Saksmappe"
                and case_number == CASE_NUMBER
                and admin_enhet == RME_M_ADMIN_ENHET
            ):
                return item

    return None


def fetch_known_case():
    case = get_json(f"{BASE}/saksmappe/{KNOWN_CASE_ID}")

    case_number = case.get("saksnummer")
    admin_enhet = case.get("administrativEnhetObjekt") or case.get("journalenhet")

    if case_number != CASE_NUMBER:
        raise RuntimeError(
            f"Known case ID resolved, but case number was {case_number}, expected {CASE_NUMBER}."
        )

    if admin_enhet != RME_M_ADMIN_ENHET:
        raise RuntimeError(
            f"Known case ID resolved, but admin unit was {admin_enhet}, expected {RME_M_ADMIN_ENHET}."
        )

    return case


def find_case():
    case = find_case_by_search()

    if case:
        return case

    return fetch_known_case()


def fetch_case_journalposts(case_id):
    all_items = []
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

        next_url = data.get("next") if isinstance(data, dict) else None
        if next_url and next_url.startswith("/"):
            next_url = urljoin(BASE, next_url)

    return all_items


def build_result():
    case = find_case()
    case_id = case["id"]

    journalposts = fetch_case_journalposts(case_id)
    simplified_journalposts = [simplify_journalpost(jp) for jp in journalposts]

    simplified_journalposts.sort(
        key=lambda x: x.get("published") or "",
        reverse=True,
    )

    return {
        "monitor": {
            "name": "RME-M 2023/9221 eInnsyn monitor",
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "case_number": CASE_NUMBER,
            "case_id": case_id,
            "rme_m_administrativ_enhet": RME_M_ADMIN_ENHET,
            "filters_used": {
                "query": CASE_NUMBER,
                "administrativEnhet": RME_M_ADMIN_ENHET,
            },
            "fallback_case_id": KNOWN_CASE_ID,
        },
        "case": simplify_case(case),
        "total_journalposts": len(simplified_journalposts),
        "journalposts": simplified_journalposts,
    }


def comparable_result(result):
    clean = json.loads(json.dumps(result, ensure_ascii=False))

    if "monitor" in clean:
        clean["monitor"].pop("checked_at_utc", None)

    return clean


def stable_hash(data):
    dumped = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


def load_previous():
    if not os.path.exists(STATE_FILE):
        return None

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_current(result):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def summarize_changes(previous, current):
    previous_posts = {
        jp.get("id"): jp
        for jp in previous.get("journalposts", [])
        if jp.get("id")
    }

    current_posts = {
        jp.get("id"): jp
        for jp in current.get("journalposts", [])
        if jp.get("id")
    }

    added_ids = sorted(set(current_posts) - set(previous_posts))
    removed_ids = sorted(set(previous_posts) - set(current_posts))

    changed_ids = []
    for jp_id in sorted(set(previous_posts) & set(current_posts)):
        if stable_hash(previous_posts[jp_id]) != stable_hash(current_posts[jp_id]):
            changed_ids.append(jp_id)

    return {
        "added_count": len(added_ids),
        "removed_count": len(removed_ids),
        "changed_count": len(changed_ids),
        "added": [current_posts[jp_id] for jp_id in added_ids],
        "removed": [previous_posts[jp_id] for jp_id in removed_ids],
        "changed_ids": changed_ids,
        "previous_total_journalposts": previous.get("total_journalposts"),
        "current_total_journalposts": current.get("total_journalposts"),
    }


def send_email(subject, body):
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD")
    alert_to = os.environ.get("ALERT_TO")

    if not gmail_user:
        raise RuntimeError("Missing environment variable: GMAIL_USER")

    if not gmail_app_password:
        raise RuntimeError("Missing environment variable: GMAIL_APP_PASSWORD")

    if not alert_to:
        raise RuntimeError("Missing environment variable: ALERT_TO")

    recipients = [
        x.strip()
        for x in alert_to.replace(";", ",").split(",")
        if x.strip()
    ]

    if not recipients:
        raise RuntimeError("ALERT_TO does not contain any valid recipient.")

    msg = EmailMessage()
    msg["From"] = gmail_user
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(gmail_user, gmail_app_password.replace(" ", ""))
        smtp.send_message(msg)


def main():
    current_result = build_result()
    previous_result = load_previous()

    if previous_result is None:
        current_result["change_status"] = {
            "changed": False,
            "reason": "No previous JSON found. Baseline created only.",
            "email_sent": False,
        }

        save_current(current_result)
        print(json.dumps(current_result, ensure_ascii=False, indent=2, sort_keys=True))
        return

    previous_comparable = comparable_result(previous_result)
    current_comparable = comparable_result(current_result)

    changed = stable_hash(previous_comparable) != stable_hash(current_comparable)

    if changed:
        change_summary = summarize_changes(previous_result, current_result)

        current_result["change_status"] = {
            "changed": True,
            "reason": "Current eInnsyn result differs from previous saved JSON.",
            "email_sent": False,
            "change_summary": change_summary,
        }

        email_body = (
            "The monitored eInnsyn case has changed.\n\n"
            f"Case: {CASE_NUMBER}\n"
            f"Case ID: {current_result['monitor']['case_id']}\n"
            f"Checked at UTC: {current_result['monitor']['checked_at_utc']}\n\n"
            "Change summary:\n"
            f"- Added journalposts: {change_summary['added_count']}\n"
            f"- Removed journalposts: {change_summary['removed_count']}\n"
            f"- Changed journalposts: {change_summary['changed_count']}\n"
            f"- Previous total: {change_summary['previous_total_journalposts']}\n"
            f"- Current total: {change_summary['current_total_journalposts']}\n\n"
            "Current JSON:\n"
            + json.dumps(current_result, ensure_ascii=False, indent=2, sort_keys=True)
        )

        send_email(
            subject=f"eInnsyn changed: RME-M {CASE_NUMBER}",
            body=email_body,
        )

        current_result["change_status"]["email_sent"] = True

    else:
        current_result["change_status"] = {
            "changed": False,
            "reason": "No content change compared with previous saved JSON.",
            "email_sent": False,
        }

    save_current(current_result)

    print(json.dumps(current_result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
