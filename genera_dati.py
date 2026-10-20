#!/usr/bin/env python3
"""Genera i dati statici del widget per GitHub Pages.

Per ogni materia legge la pagina da Notion (una sola GET) e scrive
site/data/<slug>.json con SOLO: slug, nome, esame, esame2, itinere1, itinere2,
inizio, colore, conseguita, anno, aggiornato. Copia anche widget.html in site/.

Proprieta' Notion lette (tutte opzionali tranne il titolo; se mancano o sono
vuote il valore e' null, oppure False per "Conseguita"):
  - "Data Esame Next"  (date)      1° appello         -> "esame"
  - "Data Esame 2"     (date)      2° appello         -> "esame2"
  - "Itinere 1"        (date)      prova in itinere   -> "itinere1"
  - "Itinere 2"        (date)      prova in itinere   -> "itinere2"
  - "Inizio studio"    (date)                         -> "inizio"
  - "HEX Color"        (testo)                        -> "colore"
  - "Conseguita"       (checkbox)                     -> "conseguita"
  - "Anno"             (numero/select/testo, 1..3)    -> "anno"

Dalle STESSE pagine (nessuna richiesta in piu') calcola anche tre aggregati
site/data/anno-1.json, anno-2.json, anno-3.json (scritti sempre tutti e tre):
finestra temporale dell'anno accademico e materie da mostrare. Il calendario
dell'anno N contiene le materie con anno N e, come "recupero", quelle
dell'anno precedente non ancora conseguite. Le materie senza anno non compaiono
in nessun aggregato (viene stampato un AVVISO, non e' un errore).

Dentro GitHub Actions legge:
  - NOTION_TOKEN   (Secret)  token dell'integrazione Notion
  - MATERIE_JSON   (Secret)  contenuto di materie.json:
                             {"slug": {"page_id": "...", "anno": 1}, ...}
                             ("anno", 1..3, e' facoltativo: ripiego se Notion
                             non fornisce l'anno)
  - NOTION_VERSION (opzionale, Variable) default 2022-06-28
  - ANNO_INIZIO_CORSO (opzionale, Variable) default 2026: anno in cui inizia
                             il primo anno accademico (a ottobre), da 2000 a 2100

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
PROP_ESAME2 = "Data Esame 2"        # tipo date, opzionale: 2° appello
PROP_ITINERE1 = "Itinere 1"         # tipo date, opzionale
PROP_ITINERE2 = "Itinere 2"         # tipo date, opzionale
PROP_CONSEGUITA = "Conseguita"      # checkbox (o formula booleana), opzionale
PROP_ANNO = "Anno"                  # numero/select/testo con 1, 2 o 3, opzionale

# Calendari per anno: l'anno 1 inizia a ottobre di ANNO_INIZIO_CORSO (variabile d'ambiente)
ANNO_INIZIO_CORSO_DEFAULT = 2026
MESE_INIZIO_AA = 10
# True: il calendario N recupera solo le non conseguite dell'anno N-1;
# False: recupera le non conseguite di tutti gli anni < N.
RECUPERO_SOLO_ANNO_PRECEDENTE = True

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
ANNO_RE = re.compile(r"(?<!\d)[1-3](?!\d)")   # prima cifra 1, 2 o 3 non attaccata ad altre cifre


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


def conseguita_da_prop(prop):
    """True/False da una checkbox o da una formula booleana; altrimenti False."""
    if not isinstance(prop, dict):
        return False
    tipo = prop.get("type")
    if tipo == "checkbox":
        return bool(prop.get("checkbox"))
    if tipo == "formula":
        f = prop.get("formula")
        if isinstance(f, dict) and f.get("type") == "boolean":
            return bool(f.get("boolean"))
    return False


def _anno_valido(valore):
    return isinstance(valore, int) and not isinstance(valore, bool) and 1 <= valore <= 3


def _anno_da_testo(testo):
    if not isinstance(testo, str):
        return None
    m = ANNO_RE.search(testo)
    return int(m.group()) if m else None


def _anno_da_numero(n):
    if isinstance(n, bool) or not isinstance(n, (int, float)):
        return None
    if isinstance(n, float) and not n.is_integer():   # esclude anche nan e inf
        return None
    n = int(n)
    return n if 1 <= n <= 3 else None


def anno_da_prop(prop):
    """Anno 1..3 da number, select, multi_select (primo), rich_text o formula; altrimenti None."""
    if not isinstance(prop, dict):
        return None
    tipo = prop.get("type")
    if tipo == "number":
        return _anno_da_numero(prop.get("number"))
    if tipo == "select":
        sel = prop.get("select")
        return _anno_da_testo(sel.get("name")) if isinstance(sel, dict) else None
    if tipo == "multi_select":
        elenco = prop.get("multi_select")
        if isinstance(elenco, list) and elenco and isinstance(elenco[0], dict):
            return _anno_da_testo(elenco[0].get("name"))
        return None
    if tipo == "rich_text":
        testo = "".join(t.get("plain_text", "") for t in (prop.get("rich_text") or []) if isinstance(t, dict))
        return _anno_da_testo(testo)
    if tipo == "formula":
        f = prop.get("formula")
        if isinstance(f, dict):
            if isinstance(f.get("string"), str):
                return _anno_da_testo(f["string"])
            return _anno_da_numero(f.get("number"))
    return None


def estrai(slug, page, aggiornato, anno_ripiego=None):
    props = page.get("properties") or {}
    anno = anno_da_prop(_prop(props, PROP_ANNO))
    if anno is None:   # priorita': Notion, poi materie.json, poi nessun anno
        anno = anno_ripiego if _anno_valido(anno_ripiego) else None
    return {
        "slug": slug,
        "nome": _titolo(props) or slug,
        "esame": _data_da_prop(_prop(props, PROP_ESAME)),
        "esame2": _data_da_prop(_prop(props, PROP_ESAME2)),
        "itinere1": _data_da_prop(_prop(props, PROP_ITINERE1)),
        "itinere2": _data_da_prop(_prop(props, PROP_ITINERE2)),
        "inizio": _data_da_prop(_prop(props, PROP_INIZIO)),
        "colore": _colore_da_prop(_prop(props, PROP_COLORE)),
        "conseguita": conseguita_da_prop(_prop(props, PROP_CONSEGUITA)),
        "anno": anno,
        "aggiornato": aggiornato,
    }


# ---- Calendari per anno (funzioni pure) ----
def leggi_anno_inizio_corso(valore):
    """Valida il valore di ANNO_INIZIO_CORSO (stringa o None): vuoto -> default; fuori 2000..2100 -> esce."""
    if valore is None or not str(valore).strip():
        return ANNO_INIZIO_CORSO_DEFAULT
    testo = str(valore).strip()
    if not re.fullmatch(r"\d{4}", testo) or not 2000 <= int(testo) <= 2100:
        sys.exit("ERRORE: ANNO_INIZIO_CORSO deve essere un anno tra 2000 e 2100")
    return int(testo)


def finestra_anno(n, anno_inizio):
    """Finestra del calendario dell'anno n (1..3): dal 1 ottobre di anno_inizio+n-1
    al 30 settembre di anno_inizio+n+1, estremi inclusi (due anni accademici)."""
    if n not in (1, 2, 3):
        raise ValueError("n deve essere 1, 2 o 3")
    da = datetime.date(anno_inizio + n - 1, MESE_INIZIO_AA, 1)
    a = datetime.date(anno_inizio + n + 1, MESE_INIZIO_AA, 1) - datetime.timedelta(days=1)
    return {"da": da.isoformat(), "a": a.isoformat()}


def costruisci_aggregati(materie, aggiornato, anno_inizio, recupero_solo_anno_precedente=None):
    """Da una lista di dict (output di estrai) costruisce {1: ..., 2: ..., 3: ...}, lo schema di anno-N.json.
    Prima le materie dell'anno, poi quelle di recupero; in ogni gruppo per nome (casefold) e poi slug."""
    if recupero_solo_anno_precedente is None:
        recupero_solo_anno_precedente = RECUPERO_SOLO_ANNO_PRECEDENTE

    def voce(m, recupero):
        return {
            "slug": m["slug"],
            "nome": m.get("nome") or m["slug"],
            "colore": m.get("colore") or "default",
            "conseguita": bool(m.get("conseguita")),
            "anno": m["anno"],
            "recupero": recupero,
            "esame": m.get("esame"),
            "esame2": m.get("esame2"),
            "itinere1": m.get("itinere1"),
            "itinere2": m.get("itinere2"),
        }

    def ordine(m):
        return ((m.get("nome") or "").casefold(), m["slug"])

    risultato = {}
    for n in (1, 2, 3):
        dell_anno, da_recuperare = [], []
        for m in materie:
            a = m.get("anno")
            if not _anno_valido(a):
                continue
            if a == n:
                dell_anno.append(m)
            elif a < n and not m.get("conseguita"):
                if not recupero_solo_anno_precedente or a == n - 1:
                    da_recuperare.append(m)
        risultato[n] = {
            "anno": n,
            "aggiornato": aggiornato,
            "finestra": finestra_anno(n, anno_inizio),
            "materie": ([voce(m, False) for m in sorted(dell_anno, key=ordine)]
                        + [voce(m, True) for m in sorted(da_recuperare, key=ordine)]),
        }
    return risultato


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
        sys.exit(f"ERRORE: {origine} deve essere un oggetto {{slug: {{page_id: ..., anno: ...}}}}")
    pulite = {}
    for slug, cfg in dati.items():
        pid = cfg.get("page_id") if isinstance(cfg, dict) else None
        if not SLUG_RE.fullmatch(str(slug)):
            sys.exit(f"ERRORE: slug non valido in {origine}: {slug!r} (ammessi a-z, 0-9 e '-')")
        if not isinstance(pid, str) or not PAGE_ID_RE.fullmatch(pid.strip()):
            sys.exit(f"ERRORE: page_id non valido per la materia {slug!r}")
        anno = cfg.get("anno")   # facoltativo: ripiego se Notion non fornisce l'anno
        if anno is not None and not _anno_valido(anno):
            sys.exit(f"ERRORE: anno non valido per la materia {slug!r} in {origine} (atteso 1, 2 o 3)")
        pulite[slug] = {"page_id": pid.strip(), "anno": anno}
    return pulite


def main():
    token = (os.environ.get("NOTION_TOKEN") or "").strip()
    if not token:
        sys.exit("ERRORE: variabile NOTION_TOKEN non definita")
    materie = carica_materie()
    anno_inizio = leggi_anno_inizio_corso(os.environ.get("ANNO_INIZIO_CORSO"))   # prima di toccare site/

    dati_dir = os.path.join(OUT_DIR, "data")
    if os.path.isdir(OUT_DIR):
        shutil.rmtree(OUT_DIR)
    os.makedirs(dati_dir)

    aggiornato = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    falliti = []
    lette = []
    for i, (slug, cfg) in enumerate(materie.items()):
        if i:
            time.sleep(PAUSA_TRA_RICHIESTE)
        try:
            dati = estrai(slug, scarica_pagina(cfg["page_id"], token), aggiornato, cfg["anno"])
        except ErroreMateria as e:
            falliti.append(slug)
            print(f"ERRORE  {slug}: {e}")
            continue
        with open(os.path.join(dati_dir, slug + ".json"), "w", encoding="utf-8") as f:
            json.dump(dati, f, ensure_ascii=False, separators=(",", ":"))
        lette.append(dati)
        n_date = sum(1 for k in ("esame", "esame2", "itinere1", "itinere2", "inizio") if dati[k])
        print(f"ok      {slug}: esame={dati['esame']} colore={dati['colore']} anno={dati['anno']} date={n_date}")

    widget = os.path.join(BASE_DIR, "widget.html")
    if not os.path.exists(widget):
        sys.exit("ERRORE: manca widget.html accanto a genera_dati.py")
    shutil.copy(widget, os.path.join(OUT_DIR, "widget.html"))

    if falliti:
        sys.exit(f"\nNon pubblico: {len(falliti)} materia/e non lette ({', '.join(falliti)}). "
                 "Resta online l'ultima versione buona.")

    # Aggregati per anno: dalle stesse pagine gia' lette, scritti sempre tutti e tre
    for n, aggregato in costruisci_aggregati(lette, aggiornato, anno_inizio).items():
        with open(os.path.join(dati_dir, f"anno-{n}.json"), "w", encoding="utf-8") as f:
            json.dump(aggregato, f, ensure_ascii=False, separators=(",", ":"))
    senza_anno = sorted(d["slug"] for d in lette if d["anno"] is None)
    if senza_anno:
        print(f"AVVISO: {len(senza_anno)} materia/e senza anno, assenti dai calendari per anno: "
              f"{', '.join(senza_anno)}")
    print(f"\nFatto: {len(materie)} materie scritte in {OUT_DIR}")


if __name__ == "__main__":
    main()
