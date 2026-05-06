import json
import os
import smtplib
import ssl
import sys
from copy import deepcopy
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.parse import quote, urljoin

import requests


BASE = "https://api.einnsyn.no"

# This is the eInnsyn internal Saksmappe ID for case 2023/9221.
KNOWN_CASE_ID = "sm_01j76gtv03f0vb4e6n6gfg45k9"

# This is the administrative unit object for RME-M:
# RME-M - Seksjon for marked og systemdrift
RME_M_ADMIN_ENHET = "enh_01j73r5z2cf6xvstqj4d6fteq4"

CASE_NUMBER = "2023/9221"
OUTPUT_FILE = "latest_result.json"
LIMIT = 100

# You can override this in GitHub Actions with env OPENAI_MODEL.
# Note: if this model is not available in your OpenAI account, the API call will fail.
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")


def log(message: str) -> None:
    print(message, flush=True)


def get_json(url: str, params: dict | None = None) -> dict | list:
    response = requests.get(url, params=params, timeout=30)
    log(f"GET {response.url} -> {response.status_code}")
    response.raise_for_status()
    return response.json()


def extract_items(data):
    """
    eInnsyn responses may use different wrappers depending on endpoint.
    This makes the script tolerant.
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


def make_web_link(entity: str | None, external_id: str | None) -> str | None:
    if not entity or not external_id:
        return None

    if entity == "Saksmappe":
        path = "saksmappe"
    elif entity == "Journalpost":
        path = "journalpost"
    else:
        return None

    return f"https://einnsyn.no/{path}?id=" + quote(external_id, safe="")


def simplify_case(case: dict) -> dict:
    external_id = case.get("externalId")
    entity = case.get("entity")

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
        "web_link": make_web_link(entity, external_id),
        "raw": case,
    }


def simplify_journalpost(jp: dict) -> dict:
    external_id = jp.get("externalId")
    entity = jp.get("entity")

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
        "web_link": make_web_link(entity, external_id),
        "raw": jp,
    }


def fetch_case_by_known_id() -> dict:
    url = f"{BASE}/saksmappe/{KNOWN_CASE_ID}"
    case = get_json(url)

    if not isinstance(case, dict):
        raise RuntimeError("Unexpected case response from eInnsyn.")

    actual_case_number = case.get("saksnummer")
    actual_admin = case.get("administrativEnhetObjekt") or case.get("journalenhet")

    if actual_case_number != CASE_NUMBER:
        raise RuntimeError(
            f"Known case ID returned unexpected case number: {actual_case_number}"
        )

    if actual_admin != RME_M_ADMIN_ENHET:
        raise RuntimeError(
            f"Known case ID returned unexpected administrative unit: {actual_admin}"
        )

    return case


def fetch_case_journalposts(case_id: str) -> list[dict]:
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


def build_result() -> dict:
    case_raw = fetch_case_by_known_id()
    case_id = case_raw["id"]

    journalposts_raw = fetch_case_journalposts(case_id)
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
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "case_id": case_id,
            "case_number": CASE_NUMBER,
            "rme_m_administrativ_enhet": RME_M_ADMIN_ENHET,
            "method": "known_case_id",
            "api_urls_used": {
                "case": f"{BASE}/saksmappe/{case_id}",
                "journalposts": f"{BASE}/saksmappe/{case_id}/journalpost?limit={LIMIT}&sortBy=publisertDato&sortOrder=desc",
            },
        },
        "case": simplify_case(case_raw),
        "total_journalposts": len(journalposts),
        "journalposts": journalposts,
    }

    return result


def load_previous_result() -> dict | None:
    if not os.path.exists(OUTPUT_FILE):
        return None

    with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_current_result(result: dict) -> None:
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def comparable_result(result: dict) -> dict:
    """
    Remove fields that naturally change on every run, so they do not trigger false alerts.
    """
    cleaned = deepcopy(result)

    if isinstance(cleaned, dict):
        cleaned.get("monitor", {}).pop("checked_at_utc", None)

    return cleaned


def journalpost_map(result: dict | None) -> dict:
    if not result:
        return {}

    posts = result.get("journalposts", [])
    if not isinstance(posts, list):
        return {}

    return {
        post.get("id"): post
        for post in posts
        if isinstance(post, dict) and post.get("id")
    }


def detect_basic_changes(previous: dict | None, current: dict) -> dict:
    previous_posts = journalpost_map(previous)
    current_posts = journalpost_map(current)

    previous_ids = set(previous_posts)
    current_ids = set(current_posts)

    added_ids = sorted(current_ids - previous_ids)
    removed_ids = sorted(previous_ids - current_ids)
    common_ids = sorted(previous_ids & current_ids)

    modified_ids = []
    for post_id in common_ids:
        if previous_posts[post_id] != current_posts[post_id]:
            modified_ids.append(post_id)

    return {
        "previous_journalpost_count": len(previous_posts),
        "current_journalpost_count": len(current_posts),
        "added_ids": added_ids,
        "removed_ids": removed_ids,
        "modified_ids": modified_ids,
    }


def make_llm_payload(previous: dict | None, current: dict, basic_changes: dict) -> dict:
    """
    Keep payload focused. The raw eInnsyn data can be large, so we send the relevant comparison structure.
    """
    previous_posts = journalpost_map(previous)
    current_posts = journalpost_map(current)

    added = [current_posts[x] for x in basic_changes["added_ids"]]
    removed = [previous_posts[x] for x in basic_changes["removed_ids"]]

    modified = []
    for post_id in basic_changes["modified_ids"]:
        modified.append(
            {
                "id": post_id,
                "before": previous_posts.get(post_id),
                "after": current_posts.get(post_id),
            }
        )

    return {
        "case_before": previous.get("case") if previous else None,
        "case_after": current.get("case"),
        "basic_changes": basic_changes,
        "added_journalposts": added,
        "removed_journalposts": removed,
        "modified_journalposts": modified,
    }


def summarize_change_with_openai(previous: dict | None, current: dict, basic_changes: dict) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return (
            "OpenAI summary was not generated because OPENAI_API_KEY is not set.\n\n"
            + fallback_summary(basic_changes)
        )

    payload = make_llm_payload(previous, current, basic_changes)

    prompt = f"""
You are summarizing changes in a monitored Norwegian eInnsyn case for a compliance/legal recipient.

Write a clear, factual email-ready summary in English.

Context:
- The monitored case is eInnsyn Saksmappe {CASE_NUMBER}.
- Case ID: {KNOWN_CASE_ID}.
- Administrative unit: RME-M - Seksjon for marked og systemdrift.
- Some titles or parties may be shielded as "Avskjermet"; do not guess hidden content.
- Do not invent legal conclusions.
- Focus only on what changed between the previous JSON snapshot and the current JSON snapshot.

Required structure:
1. One-sentence headline.
2. Short explanation of whether the journalpost count changed.
3. Added journalposts, if any.
4. Removed journalposts, if any.
5. Modified journalposts, if any, explaining which fields changed in plain language.
6. If only metadata changed, say that no new or removed journalpost IDs were detected.
7. End with a short practical note on what the recipient may want to check.

Data to compare:
{json.dumps(payload, ensure_ascii=False, indent=2)}
""".strip()

    body = {
        "model": OPENAI_MODEL,
        "input": prompt,
        "max_output_tokens": 1200,
    }

    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=60,
    )

    if response.status_code >= 400:
        return (
            f"OpenAI summary was not generated. API returned {response.status_code}: "
            f"{response.text[:1000]}\n\n"
            + fallback_summary(basic_changes)
        )

    data = response.json()

    # Responses API usually exposes a convenient output_text field in SDKs,
    # but raw HTTP responses can contain nested output content.
    text_parts = []

    for output_item in data.get("output", []):
        for content_item in output_item.get("content", []):
            if content_item.get("type") in ("output_text", "text"):
                text = content_item.get("text")
                if text:
                    text_parts.append(text)

    summary = "\n".join(text_parts).strip()

    if not summary:
        return (
            "OpenAI summary was not generated because no text was returned.\n\n"
            + fallback_summary(basic_changes)
        )

    return summary


def fallback_summary(basic_changes: dict) -> str:
    lines = [
        "The monitored eInnsyn result has changed.",
        "",
        f"Previous journalpost count: {basic_changes['previous_journalpost_count']}",
        f"Current journalpost count: {basic_changes['current_journalpost_count']}",
    ]

    if basic_changes["added_ids"]:
        lines.append("")
        lines.append("Added journalpost IDs:")
        lines.extend(f"- {x}" for x in basic_changes["added_ids"])

    if basic_changes["removed_ids"]:
        lines.append("")
        lines.append("Removed journalpost IDs:")
        lines.extend(f"- {x}" for x in basic_changes["removed_ids"])

    if basic_changes["modified_ids"]:
        lines.append("")
        lines.append("Modified journalpost IDs:")
        lines.extend(f"- {x}" for x in basic_changes["modified_ids"])

    if not basic_changes["added_ids"] and not basic_changes["removed_ids"]:
        lines.append("")
        lines.append(
            "No added or removed journalpost IDs were detected. "
            "The change is likely in metadata, titles, dates, shielding information, or raw fields."
        )

    return "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    gmail_user = os.getenv("GMAIL_USER")
    gmail_app_password = os.getenv("GMAIL_APP_PASSWORD")
    alert_to = os.getenv("ALERT_TO")

    if not gmail_user or not gmail_app_password or not alert_to:
        log("Email not sent because GMAIL_USER, GMAIL_APP_PASSWORD, or ALERT_TO is missing.")
        return

    recipients = [x.strip() for x in alert_to.split(",") if x.strip()]
    if not recipients:
        log("Email not sent because ALERT_TO does not contain valid recipients.")
        return

    msg = EmailMessage()
    msg["From"] = gmail_user
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)

    context = ssl.create_default_context()

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(gmail_user, gmail_app_password)
        server.send_message(msg)

    log(f"Email sent to: {', '.join(recipients)}")


def build_email_body(previous: dict | None, current: dict, basic_changes: dict, llm_summary: str) -> str:
    case = current.get("case", {})
    monitor = current.get("monitor", {})

    body = f"""
The monitored eInnsyn result has changed.

Case ID: {monitor.get("case_id")}
Case number: {monitor.get("case_number")}
Administrative unit: RME-M - Seksjon for marked og systemdrift
Checked at UTC: {monitor.get("checked_at_utc")}

Previous journalpost count: {basic_changes["previous_journalpost_count"]}
Current journalpost count: {basic_changes["current_journalpost_count"]}

Summary of change:
{llm_summary}

Case link:
{case.get("web_link")}

API link:
{case.get("api_link")}
""".strip()

    return body


def main() -> None:
    try:
        previous_result = load_previous_result()
        current_result = build_result()

        previous_comparable = comparable_result(previous_result) if previous_result else None
        current_comparable = comparable_result(current_result)

        if previous_comparable is None:
            log("No previous JSON found. Creating baseline only.")
            save_current_result(current_result)
            print(json.dumps(current_result, ensure_ascii=False, indent=2, sort_keys=True))
            return

        if previous_comparable == current_comparable:
            log("No change detected.")
            save_current_result(current_result)
            print(json.dumps(current_result, ensure_ascii=False, indent=2, sort_keys=True))
            return

        log("Change detected.")

        basic_changes = detect_basic_changes(previous_result, current_result)
        llm_summary = summarize_change_with_openai(previous_result, current_result, basic_changes)

        email_body = build_email_body(
            previous=previous_result,
            current=current_result,
            basic_changes=basic_changes,
            llm_summary=llm_summary,
        )

        send_email(
            subject=f"eInnsyn change detected: {CASE_NUMBER}",
            body=email_body,
        )

        save_current_result(current_result)
        print(json.dumps(current_result, ensure_ascii=False, indent=2, sort_keys=True))

    except Exception as exc:
        log(f"ERROR: {exc}")
        raise


if __name__ == "__main__":
    main()
