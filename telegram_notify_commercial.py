# -*- coding: utf-8 -*-
import os, json, urllib.request, urllib.parse
from datetime import datetime, timezone

def env(name, default=""):
    return os.getenv(name, default).strip()

def parse_dt(value):
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def headers(cfg, write=False):
    key = cfg["SUPABASE_SERVICE_ROLE_KEY"]
    h = {"apikey": key, "Authorization": "Bearer " + key, "Accept-Profile": "commercial"}
    if write:
        h.update({"Content-Type": "application/json", "Content-Profile": "commercial"})
    return h

def rest_get(cfg, path):
    url = cfg["SUPABASE_URL"].rstrip("/") + "/rest/v1/" + path.lstrip("/")
    req = urllib.request.Request(url, headers=headers(cfg), method="GET")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))

def rest_post(cfg, path, payload, prefer="return=minimal"):
    url = cfg["SUPABASE_URL"].rstrip("/") + "/rest/v1/" + path.lstrip("/")
    h = headers(cfg, write=True); h["Prefer"] = prefer
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()

def normalize_list(v):
    if v is None: return []
    if isinstance(v, list): return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        try:
            x = json.loads(v)
            if isinstance(x, list): return [str(i).strip() for i in x if str(i).strip()]
        except Exception: pass
        return [x.strip() for x in v.split(",") if x.strip()]
    return []

def money_text(amount, currency):
    if amount in (None, ""): return "—"
    try:
        v = float(amount)
        txt = f"{int(v):,}".replace(",", " ") if v.is_integer() else f"{v:,.2f}".replace(",", " ")
    except Exception:
        txt = str(amount)
    return f"{txt} {currency or 'KZT'}"

def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": str(chat_id), "text": text, "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        result = json.loads(r.read().decode())
    if not result.get("ok"): raise RuntimeError(str(result))
    return str(result.get("result", {}).get("message_id", ""))

def save_notification(cfg, destination, match, status, provider_message_id=None, error_text=None):
    row = {
        "client_id": match["client_id"], "profile_id": match["profile_id"], "tender_id": match["tender_id"],
        "destination_id": destination["id"], "channel": "telegram", "status": status,
        "provider_message_id": provider_message_id, "error_text": error_text,
        "sent_at": datetime.now(timezone.utc).isoformat() if status == "sent" else None,
    }
    rest_post(cfg, "notifications_sent?on_conflict=destination_id,tender_id,channel", [row], "resolution=merge-duplicates,return=minimal")

def main():
    print("TENDER RADAR KZ COMMERCIAL — TELEGRAM SAFE")
    cfg = {
        "SUPABASE_URL": env("SUPABASE_URL"),
        "SUPABASE_SERVICE_ROLE_KEY": env("SUPABASE_SERVICE_ROLE_KEY"),
        "TELEGRAM_BOT_TOKEN": env("TELEGRAM_BOT_TOKEN"),
        "RUN_STARTED_AT": env("RUN_STARTED_AT"),
    }

    if not cfg["TELEGRAM_BOT_TOKEN"]:
        print("TELEGRAM: token missing; nothing sent."); return 0
    if not cfg["RUN_STARTED_AT"]:
        print("TELEGRAM SAFE STOP: RUN_STARTED_AT missing; nothing sent."); return 0

    run_started = parse_dt(cfg["RUN_STARTED_AT"])
    print("RUN_STARTED_AT:", run_started.isoformat())

    destinations = rest_get(cfg, "telegram_destinations?select=id,client_id,chat_id,label,active,only_urgent&active=eq.true")
    all_matches = rest_get(cfg, "client_tender_matches?select=client_id,profile_id,tender_id,matched_keyword,matched_at&order=matched_at.desc&limit=2000")

    matches = []
    for m in all_matches:
        t = parse_dt(m.get("matched_at"))
        if t and t >= run_started:
            matches.append(m)

    print("NEW MATCHES THIS RUN:", len(matches))
    if not matches:
        print("No new matches in this run; nothing sent."); return 0

    existing = rest_get(cfg, "notifications_sent?select=destination_id,tender_id,channel,status&channel=eq.telegram")
    sent_pairs = {(int(r["destination_id"]), int(r["tender_id"])) for r in existing if r.get("status") == "sent"}

    tender_ids = sorted({int(m["tender_id"]) for m in matches})
    tenders = {}
    for start in range(0, len(tender_ids), 100):
        ids = ",".join(str(x) for x in tender_ids[start:start+100])
        rows = rest_get(cfg, "tenders?select=id,source_code,source_lot_id,title,customer_name,amount,currency,expires_at,public_url" + f"&id=in.({ids})")
        for row in rows:
            tenders[int(row["id"])] = row

    sent = skipped = errors = 0
    for d in destinations:
        if d.get("only_urgent"): continue
        for m in matches:
            if int(m["client_id"]) != int(d["client_id"]): continue
            pair = (int(d["id"]), int(m["tender_id"]))
            if pair in sent_pairs:
                skipped += 1; continue
            t = tenders.get(int(m["tender_id"]))
            if not t: continue

            matched = ", ".join(normalize_list(m.get("matched_keyword"))) or "—"
            text = "\n".join([
                "🔔 Tender Radar KZ Commercial", "",
                f"Клиент: {d.get('label') or 'Tender Radar'}", "",
                str(t.get("title") or "Тендер без названия"), "",
                f"Источник: {t.get('source_code') or '—'}",
                f"Лот: {t.get('source_lot_id') or '—'}",
                f"Заказчик: {t.get('customer_name') or '—'}",
                f"Сумма: {money_text(t.get('amount'), t.get('currency'))}",
                f"Срок: {t.get('expires_at') or '—'}", "",
                f"Совпадение: {matched}", "", "Открыть тендер:", str(t.get("public_url") or "—"),
            ])

            try:
                message_id = send_telegram(cfg["TELEGRAM_BOT_TOKEN"], d["chat_id"], text)
                save_notification(cfg, d, m, "sent", message_id, None)
                sent_pairs.add(pair); sent += 1
                print("SENT:", d["client_id"], m["tender_id"], message_id)
            except Exception as e:
                errors += 1; err = str(e)[:1000]
                print("ERROR:", d["client_id"], m["tender_id"], err)
                try: save_notification(cfg, d, m, "error", None, err)
                except Exception: pass

    print(f"DONE sent={sent} skipped={skipped} errors={errors}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
