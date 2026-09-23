# -*- coding: utf-8 -*-
"""
Tender Radar KZ Commercial — Telegram notifier
Отдельный безопасный модуль уведомлений.

Назначение:
commercial.client_tender_matches
    -> проверка, что уведомление еще не отправлялось
    -> Telegram Bot API
    -> commercial.notifications_sent

Переменные окружения:
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
TELEGRAM_BOT_TOKEN
"""

import os
import json
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def get_config():
    return {
        "SUPABASE_URL": env("SUPABASE_URL"),
        "SUPABASE_SERVICE_ROLE_KEY": env("SUPABASE_SERVICE_ROLE_KEY"),
        "TELEGRAM_BOT_TOKEN": env("TELEGRAM_BOT_TOKEN"),
    }


def headers(cfg, *, write=False):
    key = cfg["SUPABASE_SERVICE_ROLE_KEY"]
    h = {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Accept-Profile": "commercial",
    }
    if write:
        h.update({
            "Content-Type": "application/json",
            "Content-Profile": "commercial",
        })
    return h


def rest_get(cfg, path):
    url = cfg["SUPABASE_URL"].rstrip("/") + "/rest/v1/" + path.lstrip("/")
    req = urllib.request.Request(url, headers=headers(cfg), method="GET")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def rest_post(cfg, path, payload, prefer="return=minimal"):
    url = cfg["SUPABASE_URL"].rstrip("/") + "/rest/v1/" + path.lstrip("/")
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={**headers(cfg, write=True), "Prefer": prefer},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def normalize_keywords(v):
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass
        return [x.strip() for x in v.split(",") if x.strip()]
    return []


def money_text(amount, currency):
    if amount in (None, ""):
        return "—"
    try:
        value = float(amount)
        if value.is_integer():
            txt = f"{int(value):,}".replace(",", " ")
        else:
            txt = f"{value:,.2f}".replace(",", " ")
    except Exception:
        txt = str(amount)
    return f"{txt} {currency or 'KZT'}"


def build_message(destination, match, tender):
    matched = ", ".join(normalize_keywords(match.get("matched_keyword"))) or "—"
    lines = [
        "🔔 Tender Radar KZ Commercial",
        "",
        f"Клиент: {destination.get('label') or 'Tender Radar'}",
        "",
        str(tender.get("title") or "Тендер без названия"),
        "",
        f"Источник: {tender.get('source_code') or '—'}",
        f"Лот: {tender.get('source_lot_id') or '—'}",
        f"Заказчик: {tender.get('customer_name') or '—'}",
        f"Сумма: {money_text(tender.get('amount'), tender.get('currency'))}",
        f"Срок: {tender.get('expires_at') or '—'}",
        "",
        f"Совпадение: {matched}",
    ]
    if tender.get("public_url"):
        lines += ["", "Открыть тендер:", str(tender["public_url"])]
    return "\n".join(lines)


def telegram_send(bot_token, chat_id, text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": str(chat_id),
        "text": text,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(str(data))
    return str(data.get("result", {}).get("message_id", ""))


def save_notification(cfg, *, destination, match, status, provider_message_id=None, error_text=None):
    row = {
        "client_id": match["client_id"],
        "profile_id": match["profile_id"],
        "tender_id": match["tender_id"],
        "destination_id": destination["id"],
        "channel": "telegram",
        "status": status,
        "provider_message_id": provider_message_id,
        "error_text": error_text,
        "sent_at": datetime.now(timezone.utc).isoformat() if status == "sent" else None,
    }
    rest_post(
        cfg,
        "notifications_sent?on_conflict=destination_id,tender_id,channel",
        [row],
        prefer="resolution=merge-duplicates,return=minimal",
    )


def main():
    print("=" * 72)
    print("TENDER RADAR KZ COMMERCIAL — TELEGRAM NOTIFIER")
    print("=" * 72)

    cfg = get_config()

    missing_core = [
        k for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")
        if not cfg.get(k)
    ]
    if missing_core:
        raise RuntimeError("Missing configuration: " + ", ".join(missing_core))

    if not cfg["TELEGRAM_BOT_TOKEN"]:
        print("TELEGRAM: TELEGRAM_BOT_TOKEN не задан. Уведомления пропущены.")
        return 0

    destinations = rest_get(
        cfg,
        "telegram_destinations"
        "?select=id,client_id,chat_id,label,active,only_urgent"
        "&active=eq.true"
    )
    if not destinations:
        print("TELEGRAM: активных получателей нет.")
        return 0

    matches = rest_get(
        cfg,
        "client_tender_matches"
        "?select=client_id,profile_id,tender_id,matched_keyword,status,matched_at,relevance_score"
        "&order=matched_at.desc"
        "&limit=1000"
    )
    if not matches:
        print("TELEGRAM: совпадений нет.")
        return 0

    existing = rest_get(
        cfg,
        "notifications_sent"
        "?select=destination_id,tender_id,channel,status"
        "&channel=eq.telegram"
    )
    already_sent = {
        (int(r["destination_id"]), int(r["tender_id"]))
        for r in existing
        if r.get("status") == "sent"
    }

    tender_ids = sorted({int(m["tender_id"]) for m in matches})
    tenders_by_id = {}

    # Загружаем тендеры небольшими пачками.
    for start in range(0, len(tender_ids), 100):
        chunk = tender_ids[start:start + 100]
        ids = ",".join(str(x) for x in chunk)
        rows = rest_get(
            cfg,
            "tenders"
            "?select=id,source_code,source_lot_id,title,customer_name,amount,currency,expires_at,public_url"
            f"&id=in.({ids})"
        )
        for t in rows:
            tenders_by_id[int(t["id"])] = t

    sent_count = 0
    skipped_count = 0
    error_count = 0

    for destination in destinations:
        dest_id = int(destination["id"])
        client_id = int(destination["client_id"])

        # Пока режим only_urgent не автоматизируем: лучше не послать лишнего.
        if destination.get("only_urgent"):
            print(f"TELEGRAM: destination {dest_id} only_urgent=true — пропущен.")
            continue

        for match in matches:
            if int(match["client_id"]) != client_id:
                continue

            tender_id = int(match["tender_id"])
            pair = (dest_id, tender_id)

            if pair in already_sent:
                skipped_count += 1
                continue

            tender = tenders_by_id.get(tender_id)
            if not tender:
                print(f"TELEGRAM: tender_id={tender_id} не найден.")
                continue

            text = build_message(destination, match, tender)

            try:
                message_id = telegram_send(
                    cfg["TELEGRAM_BOT_TOKEN"],
                    destination["chat_id"],
                    text,
                )
                save_notification(
                    cfg,
                    destination=destination,
                    match=match,
                    status="sent",
                    provider_message_id=message_id,
                    error_text=None,
                )
                already_sent.add(pair)
                sent_count += 1
                print(
                    f"TELEGRAM SENT: client={client_id} "
                    f"tender={tender_id} message_id={message_id}"
                )
            except Exception as exc:
                error_count += 1
                err = str(exc)[:1500]
                print(
                    f"TELEGRAM ERROR: client={client_id} "
                    f"tender={tender_id}: {err}"
                )
                try:
                    save_notification(
                        cfg,
                        destination=destination,
                        match=match,
                        status="error",
                        provider_message_id=None,
                        error_text=err,
                    )
                except Exception as db_exc:
                    print("TELEGRAM ERROR LOG FAILED:", str(db_exc)[:500])

    print(
        f"TELEGRAM DONE: sent={sent_count}, "
        f"already_sent={skipped_count}, errors={error_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
