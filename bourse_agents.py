#!/usr/bin/env python3
"""
Système d'agents IA pour la bourse - Chasseur de Pépites (Europe + USA).
Méthode : 5 voyants stricts + Analyse fondamentale locale (Piotroski & Altman).
Sécurité : 100% autonome, aucun service externe (pas d'API Gemini/Groq).
"""
import argparse, json, logging, math, os, smtplib, sys, time, warnings
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
from yfinance import EquityQuery

warnings.filterwarnings("ignore")

DOSSIER = Path(__file__).resolve().parent
LOG = DOSSIER / "bourse.log"
MEMOIRE = DOSSIER / "memoire_alertes.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
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

# Gmail reste nécessaire pour t'envoyer le mail
DEST_EMAIL = os.getenv("DEST_EMAIL") or os.getenv("GMAIL_USER") or "charlybotton85600@gmail.com"
GMAIL_USER = os.getenv("GMAIL_USER") or DEST_EMAIL
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

PRIX_MAX = 80.0
SCORE_MIN = 70            # Le score minimum pour valider l'achat (sur 100)
MAX_RESULTATS = 1
FORMULE = (os.getenv("FORMULE_FORTUNEO") or "progress").lower()
MIN_ACTIONS = 10
GAIN_NET_PCT = 5.0
HORIZON_MAX_SEANCES = 40
TAUX_REUSSITE_MIN = 0.60
MAX_DOSSIERS = 25         
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
def lire_json(f, defaut):
    try: return json.loads(f.read_text(encoding="utf-8")) if f.exists() else defaut
    except: return defaut

def ecrire_json(f, data):
    f.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

def envoyer_mail(sujet, corps):
    if not GMAIL_APP_PASSWORD: raise RuntimeError("Mot de passe Gmail manquant")
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = sujet, GMAIL_USER, DEST_EMAIL
    m.set_content(corps)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.send_message(m)
    log.info("Mail envoyé : %s", sujet)

# ----------------------------------------------------------------- FRAIS
def frais(montant):
    if FORMULE == "starter": return montant * 0.0035
    if FORMULE == "trader": return 9.50 if montant <= 10000 else montant * 0.0010
    return 4.90 if montant <= 3000 else montant * 0.0015

# ----------------------------------------------------------------- YAHOO & TECHNIQUE
def cotation(ticker):
    try:
        fi = yf.Ticker(ticker).fast_info
        prix, prec = float(fi.last_price), float(fi.previous_close)
        if not prix or not prec: raise ValueError()
    except: raise RuntimeError(f"Cotation indisponible: {ticker}")
    return {"ticker": ticker, "prix": round(prix, 3), "var_pct": round((prix / prec - 1) * 100, 2)}

def est_pea(ticker):
    try:
        isin = yf.Ticker(ticker).isin
        if isin and len(isin) >= 2:
            return "Oui" if isin[:2] in PAYS_PEA else "Non"
    except: pass
    return "Oui" if ticker.endswith((".PA", ".AS", ".BR", ".LS")) else "Inconnu"

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
        "repli": ((var5 <= 0.01) & (repli >= -0.10)).fillna(False),
        "rsi": rsi.between(30, 55).fillna(False),
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

# ----------------------------------------------------------------- ANALYSE FONDAMENTALE LOCALE
def calculer_score_local(ticker):
    """Calcule un score financier local sans IA (basé sur le F-Score et le Z-Score)."""
    try:
        t = yf.Ticker(ticker)
        bilan = t.balance_sheet
        resultat = t.income_stmt
        tresorerie = t.cashflow
        
        # Sécurité : Si les données sont vides ou trop courtes, on rejette
        if bilan.empty or resultat.empty or tresorerie.empty or len(bilan.columns) < 2:
            return 0, "Données financières insuffisantes"
            
        # Sécurité : On vérifie que les lignes clés existent
        lignes_requises = ['Total Assets', 'Current Assets', 'Current Liabilities', 'Ordinary Shares Number', 'Long Term Debt']
        for l in lignes_requises:
            if l not in bilan.index: return 0, f"Donnée bilan manquante: {l}"
            
        lignes_res = ['Net Income', 'Gross Profit', 'Total Revenue', 'EBIT']
        for l in lignes_res:
            if l not in resultat.index: return 0, f"Donnée résultat manquante: {l}"

        # --- CALCUL DU PIOTROSKI F-SCORE (sur 9) ---
        score_f = 0
        # 1. ROA positif
        if resultat.loc['Net Income'].iloc[0] / bilan.loc['Total Assets'].iloc[0] > 0: score_f += 1
        # 2. Cash-flow opérationnel positif
        if 'Operating Cash Flow' in tresorerie.index and tresorerie.loc['Operating Cash Flow'].iloc[0] > 0: score_f += 1
        # 3. ROA en amélioration
        roa = resultat.loc['Net Income'].iloc[0] / bilan.loc['Total Assets'].iloc[0]
        roa_prec = resultat.loc['Net Income'].iloc[1] / bilan.loc['Total Assets'].iloc[1]
        if roa > roa_prec: score_f += 1
        # 4. Cash-flow > Bénéfice net
        if 'Operating Cash Flow' in tresorerie.index and tresorerie.loc['Operating Cash Flow'].iloc[0] > resultat.loc['Net Income'].iloc[0]: score_f += 1
        # 5. Baisse de la dette long terme
        if bilan.loc['Long Term Debt'].iloc[0] < bilan.loc['Long Term Debt'].iloc[1]: score_f += 1
        # 6. Ratio de liquidité en hausse
        cr = bilan.loc['Current Assets'].iloc[0] / bilan.loc['Current Liabilities'].iloc[0]
        cr_prec = bilan.loc['Current Assets'].iloc[1] / bilan.loc['Current Liabilities'].iloc[1]
        if cr > cr_prec: score_f += 1
        # 7. Pas de dilution
        if bilan.loc['Ordinary Shares Number'].iloc[0] <= bilan.loc['Ordinary Shares Number'].iloc[1]: score_f += 1
        # 8. Marge brute en hausse
        marge = resultat.loc['Gross Profit'].iloc[0] / resultat.loc['Total Revenue'].iloc[0]
        marge_prec = resultat.loc['Gross Profit'].iloc[1] / resultat.loc['Total Revenue'].iloc[1]
        if marge > marge_prec: score_f += 1
        # 9. Rotation des actifs en hausse
        rot = resultat.loc['Total Revenue'].iloc[0] / bilan.loc['Total Assets'].iloc[0]
        rot_prec = resultat.loc['Total Revenue'].iloc[1] / bilan.loc['Total Assets'].iloc[1]
        if rot > rot_prec: score_f += 1

        # --- CALCUL DE L'ALTMAN Z-SCORE ---
        fonds_roulement = bilan.loc['Current Assets'].iloc[0] - bilan.loc['Current Liabilities'].iloc[0]
        z_score = (
            6.56 * (fonds_roulement / bilan.loc['Total Assets'].iloc[0]) +
            3.26 * (bilan.loc['Retained Earnings'].iloc[0] / bilan.loc['Total Assets'].iloc[0]) +
            6.72 * (resultat.loc['EBIT'].iloc[0] / bilan.loc['Total Assets'].iloc[0]) +
            1.05 * (bilan.loc['Total Equity Gross Minority Interest'].iloc[0] / bilan.loc['Total Liabilities Net Minority Interest'].iloc[0])
        )

        # --- VERDICT (Note sur 100) ---
        if score_f >= 7 and z_score > 2.99:
            return 90, f"F-Score: {score_f}/9, Z-Score: {z_score:.2f} (Solide)"
        elif score_f >= 5 and z_score > 1.81:
            return 65, f"F-Score: {score_f}/9, Z-Score: {z_score:.2f} (Moyen)"
        else:
            return 30, f"F-Score: {score_f}/9, Z-Score: {z_score:.2f} (Risqué)"

    except Exception as e:
        return 0, f"Erreur de calcul : {str(e)[:50]}"

# ----------------------------------------------------------------- AGENTS
def agent_actualites():
    candidats = set()
    q = EquityQuery('and', [
        EquityQuery('lt', ['intradayprice', PRIX_MAX]),
        EquityQuery('lt', ['intradaymarketcap', 2000000000]),
        EquityQuery('gt', ['avgdailyvol3m', 50000]),
        EquityQuery('is-in', ['exchange', 'PAR', 'AMS', 'BRU', 'FRA', 'NYQ', 'NMS'])
    ])
    
    try:
        log.info("Agent 1 : Recherche des pépites sur les marchés (Europe + USA)...")
        res = yf.screen(q, size=50)
        for quote in res.get("quotes", []):
            sym = quote.get("symbol", "")
            if quote.get("quoteType") == "EQUITY": 
                candidats.add(sym)
        log.info(f"Agent 1 : {len(candidats)} pépites trouvées.")
    except Exception as e:
        log.error(f"Échec du screener : {e}. Bascule sur la liste de secours.")
    
    if not candidats:
        candidats.update(TICKERS_SECOURS)
        
    return list(candidats)

def agent_filtre(candidats):
    retenus = []
    for ticker in candidats[:MAX_DOSSIERS]:
        time.sleep(1.5) # Pause anti-blocage
        
        ana = voyants(ticker)
        if not ana or not ana["tous_verts"]: continue
        
        try:
            cot = cotation(ticker)
            if cot["prix"] > PRIX_MAX: continue
        except: continue
        
        ana.update(cot)
        ana["nom"] = ticker.split('.')[0]
        ana["pea"] = est_pea(ticker)
        retenus.append(ana)
        log.info(f"  -> {ticker} validé (5/5 voyants) ! Prix: {cot['prix']}")
        
    log.info("Agent 3 : %d titres passent le filtre technique", len(retenus))
    return retenus

def agent_analyse_fondamentale(titre):
    ticker = titre["ticker"]
    
    # VÉRIFICATION DE LA MÉMOIRE
    memoire = lire_json(MEMOIRE, {})
    if ticker in memoire:
        derniere_alerte = datetime.fromisoformat(memoire[ticker])
        if datetime.now() - derniere_alerte < timedelta(hours=DELAI_MEMOIRE_HEURES):
            return None

    taux, duree = historique_cible(titre["_h"], GAIN_NET_PCT)
    if taux < TAUX_REUSSITE_MIN: return None

    # REMPLACEMENT DE GEMINI PAR LE CALCUL LOCAL
    score, raison = calculer_score_local(ticker)
    log.info(f"  -> {ticker} analyse fondamentale : {raison}")
    
    if score < SCORE_MIN: return None

    return {**titre, "score_ia": score, "raison_ia": raison, "taux_hist": round(taux*100, 1), "duree_hist": duree}

def agent_mail(resultats):
    if not resultats: return False
    best = sorted(resultats, key=lambda r: r["score_ia"], reverse=True)[:MAX_RESULTATS]
    
    lignes = ["Bonjour Charly,\n", "Voici la meilleure pépite détectée aujourd'hui :\n"]
    for r in best:
        prix_achat = r["prix"]
        prix_vente_brut = prix_achat * 1.05
        prix_vente_net = prix_vente_brut * 1.01 
        
        lignes += [
            f"=== {r['nom']} ({r['ticker']}) ===",
            f"Éligible PEA : {r['pea']}",
            f"Prix d'entrée (Achat) : {prix_achat}",
            f"Objectif de sortie (Vente) : {round(prix_vente_net, 2)} (pour viser +5% net après frais)",
            f"RSI(14)    : {r['rsi']}",
            f"Score IA   : {r['score_ia']}/100",
            f"",
            f"💡 L'analyse financière (Piotroski / Altman) :",
            f"{r['raison_ia']}",
            f"",
            f"📊 Historique de l'action :",
            f"Cette action a atteint cet objectif de +5% dans {r['taux_hist']}% des cas sur l'année passée.",
            f"Délai estimé pour atteindre l'objectif : environ {r['duree_hist']} séances de bourse.",
            f"",
            f"💰 PLAN D'ORDRE (à toi d'adapter selon ton budget) :",
            f"Tu peux acheter un multiple de 10 actions.",
            f"Exemple pour 10 actions : {round(10 * prix_achat, 2)} (hors frais).",
            ""
        ]
    try:
        envoyer_mail("PÉPITE Bourse — Opportunité", "\n".join(lignes))
        memoire = lire_json(MEMOIRE, {})
        for r in best:
            memoire[r["ticker"]] = datetime.now().isoformat()
        ecrire_json(MEMOIRE, memoire)
        
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

# ----------------------------------------------------------------- MAIN
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--test", action="store_true")
    p.add_argument("--diagnostic", action="store_true")
    p.add_argument("--une-fois", action="store_true")
    a = p.parse_args()

    if a.diagnostic:
        print(f"Gmail: {'OK' if GMAIL_APP_PASSWORD else 'MANQUANT'}")
        return

    if a.test or a.une_fois:
        un_passage()
        return

    # Boucle principale
    while True:
        n = datetime.now(PARIS)
        if n.weekday() < 5 and (9, 0) <= (n.hour, n.minute) <= (17, 30):
            un_passage()
        time.sleep(15 * 60)

if __name__ == "__main__":
    main()
