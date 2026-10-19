#!/usr/bin/env python3
"""Genera i dati statici del widget per GitHub Pages.

Per ogni materia legge la pagina da Notion (una sola GET) e scrive
site/data/<slug>.json con SOLO: slug, nome, esame, inizio, colore, aggiornato.
Copia anche widget.html in site/.

Dentro GitHub Actions legge:
  - NOTION_TOKEN   (Secret)  token dell'integrazione Notion
  - MATERIE_JSON   (Secret)  contenuto di materie.json: {"slug": {"page_id": "..."}, ...}
  - NOTION_VERSION (opzionale, Variable) default 2022-06-28

In locale: se MATERIE_JSON manca usa il file materie.json; se python-dotenv e'
installato e ENV_FILE e' impostato, carica quel .env (cosi' trovi NOTION_TOKEN).

Esce con codice 1 se anche una sola materia non si legge: in quel caso l'Action
NON pubblica e resta online l'ultima versione buona.
Il token non viene mai stampato.
"""
import datetime
import json
import os
import re
import shutil
import sys
import time
import unicodedata

import requests

try:  # facoltativo: serve solo per provare in locale con un .env
    from dotenv import load_dotenv
    if os.environ.get("ENV_FILE"):
        load_dotenv(os.environ["ENV_FILE"])
except ImportError:
    pass

# ---- Configurazione (tieni allineati i nomi delle proprieta' con server.py) ----
NOTION_VERSION = os.environ.get("NOTION_VERSION") or "2022-06-28"
PROP_ESAME = "Data Esame Next"      # tipo date
PROP_INIZIO = "Inizio studio"       # tipo date, opzionale
PROP_COLORE = "HEX Color"           # testo con un esadecimale (es. #A4E7D6)

NOTION_API = "https://api.notion.com/v1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "site")
PAUSA_TRA_RICHIESTE = 0.4   # secondi: resta sotto i ~3 richieste/s di Notion
TENTATIVI = 4

COLORI_NOTION = ("default", "gray", "brown", "orange", "yellow",
                 "green", "blue", "purple", "pink", "red")
SLUG_RE = re.compile(r"[a-z0-9-]{1,40}")
PAGE_ID_RE = re.compile(r"[0-9a-fA-F]{8}(-?[0-9a-fA-F]{4}){3}-?[0-9a-fA-F]{12}")
DATA_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
HEX_RE = re.compile(r"#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})")
HEX_IN_TESTO_RE = re.compile(r"#([0-9a-fA-F]{6})(?![0-9a-fA-F])")


# ---- Lettura delle proprieta' (stesse regole di server.py) ----
def _solo_data(valore):
    if not isinstance(valore, str):
        return None
    s = valore[:10]
    if not DATA_RE.fullmatch(s):
        return None
    try:
        datetime.date.fromisoformat(s)
    except ValueError:
        return None
    return s


def _data_da_prop(prop):
    if not isinstance(prop, dict) or prop.get("type") != "date":
        return None
    d = prop.get("date")
    if not isinstance(d, dict):
        return None
    return _solo_data(d.get("start"))


def _hex_valido(testo):
    """Accetta 'RRGGBB', '#RRGGBB' o '#RGB'; tollera spazi e caratteri invisibili."""
    if not isinstance(testo, str):
        return None
    pulito = "".join(c for c in testo if unicodedata.category(c) != "Cf" and c != "\u2800").strip()
    m = HEX_RE.fullmatch(pulito)
    if m:
        h = m.group(1)
    else:
        m = HEX_IN_TESTO_RE.search(pulito)
        if not m:
            return None
        h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h.upper()


def _colore_da_prop(prop):
    if not isinstance(prop, dict):
        return "default"
    tipo = prop.get("type")
    if tipo == "rich_text":
        testo = "".join(t.get("plain_text", "") for t in (prop.get("rich_text") or []) if isinstance(t, dict))
        return _hex_valido(testo) or "default"
    if tipo == "formula":
        f = prop.get("formula")
        return (_hex_valido(f.get("string")) if isinstance(f, dict) else None) or "default"
    if tipo == "select":
        sel = prop.get("select")
        if isinstance(sel, dict):
            nome = sel.get("name")
            if isinstance(nome, str) and nome.strip().lower() in COLORI_NOTION:
                return nome.strip().lower()
            esa = _hex_valido(nome)
            if esa:
                return esa
            col = sel.get("color")
            if isinstance(col, str) and col in COLORI_NOTION:
                return col
    return "default"


def _titolo(props):
    for p in props.values():
        if isinstance(p, dict) and p.get("type") == "title":
            t = "".join(x.get("plain_text", "") for x in (p.get("title") or []) if isinstance(x, dict))
            return "".join(c for c in t if c != "\u2800" and unicodedata.category(c) != "Cf").strip()
    return ""


def _norm_nome(s):
    s = "".join(c for c in str(s) if unicodedata.category(c) != "Cf")
    return " ".join(s.replace("\u00a0", " ").replace("\u2800", " ").split()).casefold()


def _prop(props, nome):
    if nome in props:
        return props[nome]
    voluto = _norm_nome(nome)
    for k, v in props.items():
        if _norm_nome(k) == voluto:
            return v
    return None


def estrai(slug, page, aggiornato):
    props = page.get("properties") or {}
    return {
        "slug": slug,
        "nome": _titolo(props) or slug,
        "esame": _data_da_prop(_prop(props, PROP_ESAME)),
        "inizio": _data_da_prop(_prop(props, PROP_INIZIO)),
        "colore": _colore_da_prop(_prop(props, PROP_COLORE)),
        "aggiornato": aggiornato,
    }


# ---- Notion ----
class ErroreMateria(Exception):
    pass


def scarica_pagina(page_id, token):
    url = f"{NOTION_API}/pages/{page_id}"
    headers = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION}
    ultimo = "nessuna risposta"
    for tentativo in range(TENTATIVI):
        try:
            r = requests.get(url, headers=headers, timeout=20)
        except requests.RequestException as e:
            ultimo = type(e).__name__          # solo il tipo: niente dettagli che potrebbero contenere URL
            time.sleep(2 * (tentativo + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            ultimo = f"http {r.status_code}"
            try:
                attesa = float(r.headers.get("Retry-After", ""))
            except ValueError:
                attesa = 2 * (tentativo + 1)
            time.sleep(min(max(attesa, 1.0), 15.0))
            continue
        if r.status_code == 404:
            raise ErroreMateria("pagina non trovata o non condivisa con l'integrazione (404)")
        if r.status_code != 200:
            raise ErroreMateria(f"http {r.status_code}")
        try:
            page = r.json()
        except ValueError:
            raise ErroreMateria("risposta non JSON") from None
        if not isinstance(page, dict):
            raise ErroreMateria("risposta inattesa")
        if page.get("archived") or page.get("in_trash"):
            raise ErroreMateria("pagina archiviata o nel cestino")
        return page
    raise ErroreMateria(f"Notion non disponibile ({ultimo})")


def carica_materie():
    testo = os.environ.get("MATERIE_JSON")
    if testo and testo.strip():
        origine = "secret MATERIE_JSON"
    else:
        percorso = os.path.join(BASE_DIR, "materie.json")
        if not os.path.exists(percorso):
            sys.exit("ERRORE: manca MATERIE_JSON (Secret) e non esiste materie.json")
        with open(percorso, encoding="utf-8") as f:
            testo = f.read()
        origine = "file materie.json"
    try:
        dati = json.loads(testo)
    except ValueError:
        sys.exit(f"ERRORE: {origine} non e' un JSON valido")
    if not isinstance(dati, dict) or not dati:
        sys.exit(f"ERRORE: {origine} deve essere un oggetto {{slug: {{page_id: ...}}}}")
    pulite = {}
    for slug, cfg in dati.items():
        pid = cfg.get("page_id") if isinstance(cfg, dict) else None
        if not SLUG_RE.fullmatch(str(slug)):
            sys.exit(f"ERRORE: slug non valido in {origine}: {slug!r} (ammessi a-z, 0-9 e '-')")
        if not isinstance(pid, str) or not PAGE_ID_RE.fullmatch(pid.strip()):
            sys.exit(f"ERRORE: page_id non valido per la materia {slug!r}")
        pulite[slug] = pid.strip()
    return pulite


def main():
    token = (os.environ.get("NOTION_TOKEN") or "").strip()
    if not token:
        sys.exit("ERRORE: variabile NOTION_TOKEN non definita")
    materie = carica_materie()

    dati_dir = os.path.join(OUT_DIR, "data")
    if os.path.isdir(OUT_DIR):
        shutil.rmtree(OUT_DIR)
    os.makedirs(dati_dir)

    aggiornato = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    falliti = []
    for i, (slug, page_id) in enumerate(materie.items()):
        if i:
            time.sleep(PAUSA_TRA_RICHIESTE)
        try:
            dati = estrai(slug, scarica_pagina(page_id, token), aggiornato)
        except ErroreMateria as e:
            falliti.append(slug)
            print(f"ERRORE  {slug}: {e}")
            continue
        with open(os.path.join(dati_dir, slug + ".json"), "w", encoding="utf-8") as f:
            json.dump(dati, f, ensure_ascii=False, separators=(",", ":"))
        print(f"ok      {slug}: esame={dati['esame']} colore={dati['colore']}")

    widget = os.path.join(BASE_DIR, "widget.html")
    if not os.path.exists(widget):
        sys.exit("ERRORE: manca widget.html accanto a genera_dati.py")
    shutil.copy(widget, os.path.join(OUT_DIR, "widget.html"))

    if falliti:
        sys.exit(f"\nNon pubblico: {len(falliti)} materia/e non lette ({', '.join(falliti)}). "
                 "Resta online l'ultima versione buona.")
    print(f"\nFatto: {len(materie)} materie scritte in {OUT_DIR}")


if __name__ == "__main__":
    main()
