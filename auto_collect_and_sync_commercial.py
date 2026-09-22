# -*- coding: utf-8 -*-
"""
Tender Radar KZ Commercial — Goszakup sync
PROTOCOL 13B

Назначение:
1) читает активные профили commercial.profiles;
2) собирает уникальные ключевые слова только для профилей, где выбран goszakup;
3) запускает существующий стабильный collector_goszakup.py;
4) читает output/tenders.json;
5) нормализует данные под commercial.tenders;
6) деактивирует прежние активные goszakup-записи;
7) делает UPSERT свежего набора в схему commercial.

Важно:
- collector_goszakup.py берём БЕЗ ИЗМЕНЕНИЙ из стабильного проекта Айдара.
- Проект Айдара этот файл не трогает.
- Запись идёт только в Commercial Supabase через Content-Profile: commercial.
"""

import os
import sys
import json
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)

KZ_TZ = timezone(timedelta(hours=5))


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def get_config():
    cfg = {
        "SUPABASE_URL": env("SUPABASE_URL"),
        "SUPABASE_SERVICE_ROLE_KEY": env("SUPABASE_SERVICE_ROLE_KEY"),
        "GOSZAKUP_TOKEN": env("GOSZAKUP_TOKEN"),
    }
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        raise RuntimeError("Missing configuration: " + ", ".join(missing))
    return cfg


def headers(cfg, *, write=False):
    h = {
        "apikey": cfg["SUPABASE_SERVICE_ROLE_KEY"],
        "Authorization": "Bearer " + cfg["SUPABASE_SERVICE_ROLE_KEY"],
        "Accept-Profile": "commercial",
    }
    if write:
        h.update({
            "Content-Type": "application/json",
            "Content-Profile": "commercial",
        })
    return h


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def nempty(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return v if v else None
    return v


def normalize_goszakup_datetime(v):
    s = nempty(v)
    if not s:
        return None
    raw = str(s).strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KZ_TZ)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return raw


def goszakup_is_active(status, expires_at):
    s = str(status or "").lower()
    if "прием" in s or "приём" in s:
        return True
    if expires_at:
        try:
            exp = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return exp > datetime.now(exp.tzinfo)
        except Exception:
            pass
    return False


def rest_get(cfg, path):
    url = cfg["SUPABASE_URL"].rstrip("/") + "/rest/v1/" + path.lstrip("/")
    req = urllib.request.Request(url, headers=headers(cfg), method="GET")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_commercial_keywords(cfg):
    rows = rest_get(
        cfg,
        "profiles?select=include_keywords,sources,active&active=eq.true"
    )

    found = []
    seen = set()

    for row in rows:
        sources = row.get("sources") or []
        if isinstance(sources, str):
            try:
                sources = json.loads(sources)
            except Exception:
                sources = [sources]

        if "goszakup" not in sources:
            continue

        words = row.get("include_keywords") or []
        if isinstance(words, str):
            try:
                parsed = json.loads(words)
                words = parsed if isinstance(parsed, list) else [words]
            except Exception:
                words = [x.strip() for x in words.split(",") if x.strip()]

        for word in words:
            w = str(word or "").strip()
            key = w.lower()
            if w and key not in seen:
                seen.add(key)
                found.append(w)

    if not found:
        raise RuntimeError(
            "Нет активных ключевых слов для профилей, где выбран источник goszakup."
        )

    return found


def run_collector(cfg, keywords):
    collector = ROOT / "collector_goszakup.py"
    if not collector.exists():
        raise RuntimeError(
            "Не найден collector_goszakup.py. "
            "Скопируйте стабильную версию из проекта tender-radar-kz."
        )

    child_env = os.environ.copy()
    child_env["GOSZAKUP_TOKEN"] = cfg["GOSZAKUP_TOKEN"]
    child_env["GOSZAKUP_KEYWORDS"] = ",".join(keywords)

    # Для первого коммерческого запуска PDF не отключаем:
    # стабильный коллектор сам применит свой circuit breaker.
    print("COMMERCIAL KEYWORDS:", ", ".join(keywords))
    print("RUN:", collector.name)

    proc = subprocess.run(
        [sys.executable, str(collector)],
        cwd=str(ROOT),
        env=child_env,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"collector_goszakup.py завершился с кодом {proc.returncode}"
        )


def make_goszakup():
    rows = load_json(OUT / "tenders.json")
    out = []

    for r in rows:
        if not isinstance(r, dict):
            continue

        ext = str(r.get("external_id") or "").strip()
        lotnum = str(r.get("lot_number") or "").strip()
        ann = str(r.get("announcement_number") or "").strip()
        sid = ext or lotnum or ann
        if not sid:
            continue

        status = str(r.get("status") or "")
        published_at = normalize_goszakup_datetime(r.get("publish_date"))
        started_at = normalize_goszakup_datetime(r.get("start_date"))
        expires_at = normalize_goszakup_datetime(r.get("end_date"))
        is_active = goszakup_is_active(status, expires_at)

        # В commercial.tenders НЕТ status_code — намеренно не отправляем его.
        out.append({
            "source_code": "goszakup",
            "source_tender_id": ann or None,
            "source_lot_id": sid,
            "public_url": nempty(r.get("public_url")),
            "title": nempty(r.get("title")),
            "description": nempty(r.get("description")),
            "customer_name": nempty(r.get("customer_name")),
            "customer_bin": nempty(r.get("customer_bin")),
            "region": nempty(r.get("region")),
            "procurement_method": nempty(r.get("trade_method")),
            "status_name": nempty(status),
            "amount": r.get("amount_kzt"),
            "currency": "KZT",
            "quantity": r.get("quantity"),
            "unit": nempty(r.get("unit")),
            "category": nempty(r.get("category")),
            "published_at": published_at,
            "started_at": started_at,
            "expires_at": expires_at,
            "is_active": is_active,
            "raw": r,
        })

    return out


def deactivate_existing_goszakup(cfg):
    endpoint = (
        cfg["SUPABASE_URL"].rstrip("/")
        + "/rest/v1/tenders?source_code=eq.goszakup&is_active=eq.true"
    )
    req = urllib.request.Request(
        endpoint,
        data=json.dumps({"is_active": False}).encode("utf-8"),
        headers={
            **headers(cfg, write=True),
            "Prefer": "return=minimal",
        },
        method="PATCH",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        resp.read()
    print("STALE goszakup: previous active rows -> inactive")


def upload_rows(cfg, rows):
    if not rows:
        print("WARNING: Goszakup returned 0 rows.")
        print("Existing Commercial rows are NOT changed.")
        return 0

    endpoint = (
        cfg["SUPABASE_URL"].rstrip("/")
        + "/rest/v1/tenders?on_conflict=source_code,source_lot_id"
    )

    h = {
        **headers(cfg, write=True),
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }

    sent = 0
    for start in range(0, len(rows), 100):
        batch = rows[start:start + 100]
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(batch, ensure_ascii=False).encode("utf-8"),
            headers=h,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                resp.read()
            sent += len(batch)
            print("SYNC goszakup:", sent, "/", len(rows))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            print("SUPABASE HTTP ERROR:", exc.code)
            print(body)
            raise

    return sent


def main():
    print("=" * 72)
    print("TENDER RADAR KZ COMMERCIAL — PROTOCOL 13B — GOSZAKUP")
    print("Target schema: commercial")
    print("=" * 72)

    cfg = get_config()
    keywords = load_commercial_keywords(cfg)

    run_collector(cfg, keywords)
    rows = make_goszakup()

    print("Prepared rows:", len(rows))
    if not rows:
        print("STOP: свежий набор пустой; старые записи не деактивируем.")
        return 0

    deactivate_existing_goszakup(cfg)
    sent = upload_rows(cfg, rows)

    print("DONE. Synced:", sent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
