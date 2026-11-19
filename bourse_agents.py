#!/usr/bin/env python3
"""
Système d'agents IA pour la bourse - Chasseur de Pépites (Europe + USA).
Version 300 dossiers : Filtres stricts + Anti-sommet (exclut les actions déjà montées).
"""
import argparse, json, logging, math, os, re, smtplib, sys, time, warnings, base64
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
from yfinance import EquityQuery

warnings.filterwarnings("ignore")

DOSSIER = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("bourse")

# ----------------------------------------------------------------- CONFIG
def charger_env():
    f = DOSSIER / ".env"
    if not f.exists(): return
    for ligne in f.read_text(encoding="utf-8").splitlines():
        if "=" in ligne and not ligne.strip().startswith("#"):
            k, v = ligne.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

charger_env()

DEST_EMAIL = os.getenv("DEST_EMAIL") or os.getenv("GMAIL_USER") or "charlybotton85600@gmail.com"
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")
GITHUB_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json"
}

PRIX_MAX = 80.0
SCORE_MIN = 65            
MAX_RESULTATS = 1         
FORMULE = (os.getenv("FORMULE_FORTUNEO") or "progress").lower()
MIN_ACTIONS = 10
GAIN_NET_PCT = 5.0
HORIZON_MAX_SEANCES = 40
TAUX_REUSSITE_MIN = 0.50  
MAX_DOSSIERS = 300        
DELAI_MEMOIRE_HEURES = 24 

PAYS_PEA = {"FR", "DE", "NL", "IT", "ES", "BE", "PT", "IE", "LU", "FI", "AT", "GR"}

TICKERS_SECOURS = [
    "GTT.PA", "SPIE.PA", "SW.PA", "TE.PA", "UBI.PA", "VAC.PA", "VK.PA", "AC.PA", 
    "ADP.PA", "AF.PA", "EDEN.PA", "TEP.PA", "ERF.PA", "WLN.PA", "ALO.PA", "ALT.PA", 
    "ALTA.PA", "AMS.PA", "ATO.PA", "BB.PA", "BOL.PA", "BVI.PA", "CGG.PA", "FDJ.PA", 
    "FR.PA", "ICAD.PA", "IPH.PA", "KOF.PA", "LI.PA", "MAU.PA", "NEX.PA", "NK.PA"
]

SUFFIXES = (".PA", ".AS", ".BR", ".LS", ".L", ".DE", ".F")
PARIS = ZoneInfo("Europe/Paris")

# ----------------------------------------------------------------- OUTILS
def envoyer_mail(sujet, corps):
    if not GMAIL_USER or not GMAIL_APP_PASSWORD: 
        raise RuntimeError(f"Identifiants manquants (User: {bool(GMAIL_USER)}, Pwd: {bool(GMAIL_APP_PASSWORD)})")
    
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = sujet, GMAIL_USER, DEST_EMAIL
    m.set_content(corps)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.send_message(m)
    log.info("Mail envoyé : %s", sujet)

# ----------------------------------------------------------------- MÉMOIRE GITHUB
def lire_memoire_github():
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY: return {}
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/memoire_alertes.json"
    r = requests.get(url, headers=GITHUB_HEADERS)
    if r.status_code == 404: return {}
    if r.status_code == 200:
        contenu = base64.b64decode(r.json()["content"]).decode("utf-8")
        return json.loads(contenu)
    return {}

def mettre_a_jour_memoire_github(ticker):
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY: return
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/memoire_alertes.json"
    memoire = lire_memoire_github()
    memoire[ticker] = datetime.now().isoformat()
    
    contenu_encode = base64.b64encode(json.dumps(memoire, indent=1).encode("utf-8")).decode("utf-8")
    
    r = requests.get(url, headers=GITHUB_HEADERS)
    sha = r.json().get("sha") if r.status_code == 200 else None
    
    payload = {"message": f"Mémoire: {ticker}", "content": contenu_encode, "branch": "main"}
    if sha: payload["sha"] = sha
        
    r = requests.put(url, headers=GITHUB_HEADERS, json=payload)
    if r.status_code in (200, 201):
        log.info(f"Mémoire GitHub mise à jour pour {ticker}.")
    else:
        log.error(f"Erreur mise à jour mémoire GitHub : {r.status_code}")

# ----------------------------------------------------------------- FRAIS & TECHNIQUE
def frais(montant):
    if FORMULE == "starter": return montant * 0.0035
    if FORMULE == "trader": return 9.50 if montant <= 10000 else montant * 0.0010
    return 4.90 if montant <= 3000 else montant * 0.0015

def cotation(ticker):
    try:
        fi = yf.Ticker(ticker).fast_info
        prix, prec = float(fi.last_price), float(fi.previous_close)
        if not prix or not prec: raise ValueError()
    except: raise RuntimeError(f"Cotation indisponible: {ticker}")
    return {"ticker": ticker, "prix": round(prix, 3), "var_pct": round((prix / prec - 1) * 100, 2)}

def infos_entreprise(ticker):
    try:
        t = yf.Ticker(ticker)
        nom = t.info.get("longName") or t.info.get("shortName") or ticker
        isin = t.isin if hasattr(t, 'isin') else "Inconnu"
        
        pea = "Inconnu"
        if isin and len(isin) >= 2:
            pea = "Oui" if isin[:2] in PAYS_PEA else "Non"
        elif ticker.endswith((".PA", ".AS", ".BR", ".LS")):
            pea = "Oui"
            
        return nom, isin, pea
    except:
        return ticker, "Inconnu", "Inconnu"

def flags_serie(h):
    c, vol = h["Close"], h["Volume"]
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    perte = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rsi = 100 - 100 / (1 + gain / perte)
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    hist = macd - macd.ewm(span=9, adjust=False).mean()
    var5 = c / c.shift(5) - 1
    repli = c / c.rolling(20).max() - 1
    f = {
        "tendance": (c > c.rolling(200).mean()).fillna(False),
        "repli": ((var5 <= 0.01) & (repli >= -0.15)).fillna(False),  # STRICT : -15%
        "rsi": rsi.between(30, 60).fillna(False),                    # STRICT : 60
        "macd": ((hist > hist.shift(1)) & (hist.shift(1) > hist.shift(2))).fillna(False),
        "volume": (vol.rolling(20).mean() >= 100000).fillna(False),
    }
    return f, rsi, var5, repli

def voyants(ticker):
    try: h = yf.Ticker(ticker).history(period="1y", interval="1d", auto_adjust=True)
    except: return None
    if len(h) < 210: return None
    f, rsi, var5, repli = flags_serie(h)
    flags = [bool(s.iloc[-1]) for s in f.values()]
    return {
        "flags": flags, "voyants_ok": sum(flags), "tous_verts": all(flags),
        "rsi": round(float(rsi.iloc[-1]), 1), "var5_pct": round(float(var5.iloc[-1])*100, 2),
        "repli_pct": round(float(repli.iloc[-1])*100, 2), "_h": h
    }

def historique_cible(h, hausse_pct):
    hi, cl = h["High"].values, h["Close"].values
    ok, durees, total = 0, [], 0
    for i in range(len(cl) - HORIZON_MAX_SEANCES):
        total += 1
        cible = cl[i] * (1 + hausse_pct / 100)
        for j in range(i + 1, i + 1 + HORIZON_MAX_SEANCES):
            if hi[j] >= cible:
                ok += 1; durees.append(j - i); break
    if not total or not durees: return 0.0, 0
    return ok / total, sorted(durees)[len(durees)//2]

def calculer_score_local(ticker):
    try:
        t = yf.Ticker(ticker)
        bilan, resultat, tresorerie = t.balance_sheet, t.income_stmt, t.cashflow
        
        if bilan.empty or resultat.empty or tresorerie.empty or len(bilan.columns) < 2:
            return 50, "Données financières insuffisantes (neutre)"
            
        lignes_bilan = ['Total Assets', 'Current Assets', 'Current Liabilities', 'Ordinary Shares Number', 'Long Term Debt']
        if any(l not in bilan.index for l in lignes_bilan): return 50, "Bilan incomplet (neutre)"
        lignes_res = ['Net Income', 'Gross Profit', 'Total Revenue', 'EBIT']
        if any(l not in resultat.index for l in lignes_res): return 50, "Résultats incomplets (neutre)"

        score_f = 0
        if resultat.loc['Net Income'].iloc[0] / bilan.loc['Total Assets'].iloc[0] > 0: score_f += 1
        if 'Operating Cash Flow' in tresorerie.index and tresorerie.loc['Operating Cash Flow'].iloc[0] > 0: score_f += 1
        roa = resultat.loc['Net Income'].iloc[0] / bilan.loc['Total Assets'].iloc[0]
        roa_prec = resultat.loc['Net Income'].iloc[1] / bilan.loc['Total Assets'].iloc[1]
        if roa > roa_prec: score_f += 1
        if 'Operating Cash Flow' in tresorerie.index and tresorerie.loc['Operating Cash Flow'].iloc[0] > resultat.loc['Net Income'].iloc[0]: score_f += 1
        if bilan.loc['Long Term Debt'].iloc[0] < bilan.loc['Long Term Debt'].iloc[1]: score_f += 1
        cr = bilan.loc['Current Assets'].iloc[0] / bilan.loc['Current Liabilities'].iloc[0]
        cr_prec = bilan.loc['Current Assets'].iloc[1] / bilan.loc['Current Liabilities'].iloc[1]
        if cr > cr_prec: score_f += 1
        if bilan.loc['Ordinary Shares Number'].iloc[0] <= bilan.loc['Ordinary Shares Number'].iloc[1]: score_f += 1
        marge = resultat.loc['Gross Profit'].iloc[0] / resultat.loc['Total Revenue'].iloc[0]
        marge_prec = resultat.loc['Gross Profit'].iloc[1] / resultat.loc['Total Revenue'].iloc[1]
        if marge > marge_prec: score_f += 1
        rot = resultat.loc['Total Revenue'].iloc[0] / bilan.loc['Total Assets'].iloc[0]
        rot_prec = resultat.loc['Total Revenue'].iloc[1] / bilan.loc['Total Assets'].iloc[1]
        if rot > rot_prec: score_f += 1

        fonds_roulement = bilan.loc['Current Assets'].iloc[0] - bilan.loc['Current Liabilities'].iloc[0]
        z_score = (6.56 * (fonds_roulement / bilan.loc['Total Assets'].iloc[0]) +
                   3.26 * (bilan.loc['Retained Earnings'].iloc[0] / bilan.loc['Total Assets'].iloc[0]) +
                   6.72 * (resultat.loc['EBIT'].iloc[0] / bilan.loc['Total Assets'].iloc[0]) +
                   1.05 * (bilan.loc['Total Equity Gross Minority Interest'].iloc[0] / bilan.loc['Total Liabilities Net Minority Interest'].iloc[0]))

        if score_f >= 7 and z_score > 2.99: return 90, f"Très solide (F-Score {score_f}/9)."
        elif score_f >= 5 and z_score > 1.81: return 65, f"Rentable et saine (F-Score {score_f}/9)."
        else: return 30, f"Risqué (F-Score {score_f}/9)."
    except: return 50, "Analyse indisponible."

# ----------------------------------------------------------------- AGENTS
def agent_actualites():
    candidats = set()
    
    # FILTRES YAHOO (SANS profitmargins qui fait planter l'API)
    q = EquityQuery('and', [
        EquityQuery('is-in', ['exchange', 'PAR', 'AMS', 'BRU', 'FRA', 'NYQ', 'NMS']),
        EquityQuery('lt', ['intradayprice', PRIX_MAX]),
        EquityQuery('gt', ['intradayprice', 1.0]),
        EquityQuery('lt', ['intradaymarketcap', 3000000000]),
        EquityQuery('gt', ['intradaymarketcap', 50000000]),
        EquityQuery('gt', ['avgdailyvol3m', 100000]),
        EquityQuery('gt', ['epsgrowth.lasttwelvemonths', 10]),
        EquityQuery('gt', ['totalrevenues1yrgrowth.lasttwelvemonths', 10]),
        
        # FILTRES ANTI-SOMMET
        EquityQuery('lt', ['fiftytwowkpercentchange', 50]),           
        EquityQuery('lt', ['percentchange', 5]),                      
        EquityQuery('lt', ['lastclose52weekhigh.lasttwelvemonths', 0.90]) 
    ])
    
    # 6 PAGES DE 50 = 300 ENTREPRISES
    for page in range(6):
        offset = page * 50
        try:
            log.info(f"Agent 1 : Recherche des pépites (page {page + 1}/6)...")
            res = yf.screen(q, size=50, offset=offset, sortField='intradaymarketcap', sortAsc=False)
            for quote in res.get("quotes", []):
                sym = quote.get("symbol", "")
                if quote.get("quoteType") == "EQUITY": candidats.add(sym)
        except Exception as e:
            log.error(f"Échec du screener (page {page + 1}, offset {offset}) : {e}")
        
        if page < 5:
            time.sleep(4)
    
    log.info(f"Agent 1 : {len(candidats)} pépites uniques trouvées (anti-sommet).")
    
    if not candidats: 
        log.warning("Aucune pépite trouvée avec les filtres stricts, utilisation de la liste de secours.")
        candidats.update(TICKERS_SECOURS)
    return list(candidats)

def agent_filtre(candidats):
    retenus = []
    for i, ticker in enumerate(candidats[:MAX_DOSSIERS]):
        time.sleep(0.8) 
        if i > 0 and i % 25 == 0:
            log.info(f"  ... {i} actions analysées sur {min(len(candidats), MAX_DOSSIERS)} ...")

        ana = voyants(ticker)
        if not ana or not ana["tous_verts"]: continue
        try:
            cot = cotation(ticker)
            if cot["prix"] > PRIX_MAX: continue
        except: continue
        ana.update(cot)
        ana["nom"] = ticker.split('.')[0]
        retenus.append(ana)
        log.info(f"  -> {ticker} validé (5/5 voyants) ! Prix: {cot['prix']}")
    log.info("Agent 3 : %d titres passent le filtre technique", len(retenus))
    return retenus

def agent_analyse_fondamentale(titre):
    ticker = titre["ticker"]
    memoire = lire_memoire_github()
    if ticker in memoire:
        derniere_alerte = datetime.fromisoformat(memoire[ticker])
        if datetime.now() - derniere_alerte < timedelta(hours=DELAI_MEMOIRE_HEURES):
            log.info(f"  -> {ticker} ignoré : déjà alerté récemment.")
            return None

    taux, duree = historique_cible(titre["_h"], GAIN_NET_PCT)
    log.info(f"  -> {ticker} taux de réussite historique : {taux*100:.1f}%")
    if taux < TAUX_REUSSITE_MIN: return None

    score, raison = calculer_score_local(ticker)
    log.info(f"  -> {ticker} score financier : {score}/100 ({raison})")
    if score < SCORE_MIN: return None

    return {**titre, "score_fin": score, "raison_fin": raison, "taux_hist": round(taux*100, 1), "duree_hist": duree}

def agent_mail(resultats):
    if not resultats: return False
    best = sorted(resultats, key=lambda r: r["score_fin"], reverse=True)[:MAX_RESULTATS]
    
    lignes = ["Bonjour Charly,\n", "Voici la meilleure pépite détectée aujourd'hui :\n"]
    for r in best:
        nom_complet, isin, pea = infos_entreprise(r['ticker'])
        prix_achat = r["prix"]
        prix_vente_net = (prix_achat * 1.05) * 1.01 
        lignes += [
            f"🏢 Entreprise : {nom_complet} ({r['ticker']})",
            f"🆔 ISIN : {isin} (Éligible PEA : {pea})",
            f"💰 Achat : {prix_achat} € | Vente (+5% net) : {round(prix_vente_net, 2)} €",
            f"⏳ Délai estimé : environ {r['duree_hist']} séances de bourse",
            f"💡 Analyse : {r['raison_fin']}"
        ]
    try:
        envoyer_mail("PÉPITE Bourse — Opportunité", "\n".join(lignes))
        for r in best: mettre_a_jour_memoire_github(r["ticker"])
        return True
    except Exception as e:
        log.error(f"Erreur mail: {e}")
        return False

def un_passage():
    cands = agent_actualites()
    retenus = agent_filtre(cands)
    resultats = []
    for t in retenus:
        r = agent_analyse_fondamentale(t)
        if r: resultats.append(r)
    if resultats: agent_mail(resultats)
    log.info("Passage terminé — %d propositions", len(resultats))

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--test", action="store_true")
    p.add_argument("--diagnostic", action="store_true")
    p.add_argument("--une-fois", action="store_true")
    a = p.parse_args()

    if a.diagnostic:
        log.info(f"DIAGNOSTIC -> Gmail User: {'OK' if GMAIL_USER else 'MANQUANT'}")
        log.info(f"DIAGNOSTIC -> Gmail Pwd: {'OK' if GMAIL_APP_PASSWORD else 'MANQUANT'}")
        return

    if a.test or a.une_fois:
        un_passage()
        return

    while True:
        n = datetime.now(PARIS)
        if n.weekday() < 5 and (9, 0) <= (n.hour, n.minute) <= (17, 30):
            un_passage()
        time.sleep(15 * 60)

if __name__ == "__main__":
    main()
