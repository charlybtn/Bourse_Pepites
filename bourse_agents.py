#!/usr/bin/env python3
"""
Système d'agents IA pour la bourse - Chasseur de Pépites (Europe, EUR).
Recherche ultra-large (milliers d'entreprises) + Filtrage Python + Marketaux.
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
MARKETAUX_API_KEY = os.getenv("MARKETAUX_API_KEY")

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
HORIZON_MAX_SEANCES = 30  
TAUX_REUSSITE_MIN = 0.60  
MAX_DOSSIERS = 3000       
DELAI_MEMOIRE_JOURS = 30  

PAYS_PEA = {"FR", "DE", "NL", "IT", "ES", "BE", "PT", "IE", "LU", "FI", "AT", "GR"}

TICKERS_SECOURS = [
    "GTT.PA", "SPIE.PA", "SW.PA", "TE.PA", "UBI.PA", "VAC.PA", "VK.PA", "AC.PA", 
    "ADP.PA", "AF.PA", "EDEN.PA", "TEP.PA", "ERF.PA", "WLN.PA", "ALO.PA", "ALT.PA", 
    "ALTA.PA", "ATO.PA", "BB.PA", "BOL.PA", "BVI.PA", "FR.PA", "ICAD.PA", "IPH.PA", 
    "KOF.PA", "LI.PA", "MAU.PA", "NEX.PA", "NK.PA", "RXL.PA", "SK.PA", "SO.PA",
    "ASML.AS", "AD.AS", "PHIA.AS", "HEIA.AS", "ABI.BR", "KBC.BR", "UCB.BR"
]

SUFFIXES = (".PA", ".AS", ".BR", ".DE", ".F", ".MI", ".MC", ".LS")
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
    
    maintenant = datetime.now()
    memoire = {
        k: v for k, v in memoire.items() 
        if maintenant - datetime.fromisoformat(v) < timedelta(days=DELAI_MEMOIRE_JOURS)
    }
    
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
        if fi.currency != "EUR":
            raise ValueError(f"Devise non EUR ({fi.currency})")
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
        elif ticker.endswith((".PA", ".AS", ".BR", ".LS", ".DE", ".F", ".MI", ".MC")):
            pea = "Oui"
            
        return nom, isin, pea
    except:
        return ticker, "Inconnu", "Inconnu"

def prix_minimum_pour_10_net(prix_achat):
    n = MIN_ACTIONS
    buy_value = n * prix_achat
    buy_fee = frais(buy_value)
    total_cost = buy_value + buy_fee
    
    net_target = total_cost * 0.10
    target_before_tax = net_target / (1 - 0.172)
    target_sell_value = buy_value + buy_fee + target_before_tax
    
    p_v = target_sell_value / n
    for _ in range(6):
        sell_fee = frais(n * p_v)
        p_v = (target_sell_value + sell_fee) / n
        
    return math.ceil(p_v * 100) / 100

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
        "repli": ((var5 <= 0.01) & (repli >= -0.15)).fillna(False),
        "rsi": rsi.between(30, 60).fillna(False),
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
    
    h['H-L'] = h['High'] - h['Low']
    h['H-PC'] = (h['High'] - h['Close'].shift(1)).abs()
    h['L-PC'] = (h['Low'] - h['Close'].shift(1)).abs()
    h['TR'] = h[['H-L', 'H-PC', 'L-PC']].max(axis=1)
    atr_14 = h['TR'].rolling(14).mean().iloc[-1]
    volatilite_pct = (atr_14 / h['Close'].iloc[-1]) * 100
    
    return {
        "flags": flags, "voyants_ok": sum(flags), "tous_verts": all(flags),
        "rsi": round(float(rsi.iloc[-1]), 1), "var5_pct": round(float(var5.iloc[-1])*100, 2),
        "repli_pct": round(float(repli.iloc[-1])*100, 2), "volatilite": round(volatilite_pct, 2), "_h": h
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

def analyser_actualites_marketaux(ticker):
    if not MARKETAUX_API_KEY:
        return None, "Clé Marketaux manquante."
    
    try:
        url = f"https://api.marketaux.com/v1/news/all?symbols={ticker}&filter_entities=true&language=fr,en,de,es,it&api_token={MARKETAUX_API_KEY}"
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        
        articles = data.get("data", [])
        if not articles:
            return None, "Aucun article trouvé."
            
        sentiments = []
        for art in articles:
            for entity in art.get("entities", []):
                if entity.get("symbol") == ticker.replace(".PA", "").replace(".AS", "") or entity.get("symbol") == ticker:
                    s = entity.get("sentiment_score")
                    if s is not None:
                        sentiments.append(s)
        
        if not sentiments:
            return None, "Pas de score de sentiment pour cette action."
            
        sentiment_moyen = sum(sentiments) / len(sentiments)
        
        meilleur_article = None
        for art in articles:
            if art.get("entities") and art["entities"][0].get("sentiment_score", 0) > 0.1:
                meilleur_article = art.get("title")
                break
                
        if sentiment_moyen > 0.1:
            return meilleur_article or "Sentiment positif détecté", f"Sentiment moyen : {sentiment_moyen:.2f}"
        else:
            return None, f"Sentiment trop faible ou négatif ({sentiment_moyen:.2f})"
            
    except Exception as e:
        return None, f"Erreur API Marketaux : {e}"

# ----------------------------------------------------------------- AGENTS
def agent_actualites():
    candidats = set()
    
    places = [
        ('PAR', 'France'), ('FRA', 'Allemagne'), ('GER', 'Allemagne'),
        ('AMS', 'Pays-Bas'), ('BRU', 'Belgique'), ('MIL', 'Italie'), 
        ('MCE', 'Espagne'), ('LIS', 'Portugal'), ('STO', 'Suède'),
        ('OSL', 'Norvège'), ('CPH', 'Danemark'), ('HEL', 'Finlande'),
        ('VIE', 'Autriche'), ('ATH', 'Grèce')
    ]
    
    for exchange, pays in places:
        q = EquityQuery('and', [
            EquityQuery('eq', ['exchange', exchange]),
            EquityQuery('lt', ['intradayprice', PRIX_MAX]),
            EquityQuery('gt', ['intradayprice', 1.0]),
            EquityQuery('gt', ['avgdailyvol3m', 50000]) 
        ])
        
        for page in range(10):
            offset = page * 100
            try:
                log.info(f"Agent 1 : Recherche en {pays} (page {page + 1}/10)...")
                res = yf.screen(q, size=100, offset=offset, sortField='intradaymarketcap', sortAsc=False)
                quotes = res.get("quotes", [])
                if not quotes: break 
                for quote in quotes:
                    sym = quote.get("symbol", "")
                    if quote.get("quoteType") == "EQUITY" and sym.endswith(SUFFIXES): 
                        candidats.add(sym)
            except Exception as e:
                log.error(f"Échec du screener ({pays}, page {page + 1}) : {e}")
            time.sleep(2)
    
    log.info(f"Agent 1 : {len(candidats)} pépites uniques trouvées en Europe (Toutes capitalisations).")
    if not candidats: 
        candidats.update(TICKERS_SECOURS)
    return list(candidats)

def agent_filtre(candidats):
    retenus = []
    
    for i, ticker in enumerate(candidats[:MAX_DOSSIERS]):
        time.sleep(0.3) 
        if i > 0 and i % 100 == 0:
            log.info(f"  ... {i} actions vérifiées sur {min(len(candidats), MAX_DOSSIERS)} ...")

        try:
            fi = yf.Ticker(ticker).fast_info
            if fi.currency != "EUR" or fi.last_price > PRIX_MAX or fi.last_price < 1.0:
                continue
            prix = round(float(fi.last_price), 3)
            prec = float(fi.previous_close)
            var_pct = round((prix / prec - 1) * 100, 2)
        except:
            continue

        ana = voyants(ticker)
        if not ana or not ana["tous_verts"]: continue
        
        if ana["volatilite"] < 1.5:
            continue

        ana["prix"] = prix
        ana["var_pct"] = var_pct
        ana["nom"] = ticker.split('.')[0]
        retenus.append(ana)
        log.info(f"  -> {ticker} validé (5/5 voyants) ! Prix: {prix} €")
        
    log.info("Agent 3 : %d titres passent le filtre technique", len(retenus))
    return retenus

def agent_analyse_fondamentale(titre):
    ticker = titre["ticker"]
    memoire = lire_memoire_github()
    
    if ticker in memoire:
        derniere_alerte = datetime.fromisoformat(memoire[ticker])
        if datetime.now() - derniere_alerte < timedelta(days=DELAI_MEMOIRE_JOURS):
            log.info(f"  -> {ticker} ignoré : déjà alerté il y a moins de {DELAI_MEMOIRE_JOURS} jours.")
            return None

    fait, msg_sentiment = analyser_actualites_marketaux(ticker)
    log.info(f"  -> {ticker} Marketaux : {msg_sentiment}")
    if not fait:
        return None 

    prix_cible = prix_minimum_pour_10_net(titre["prix"])
    hausse_requise_pct = ((prix_cible - titre["prix"]) / titre["prix"]) * 100
    
    taux, duree = historique_cible(titre["_h"], hausse_requise_pct)
    if taux < TAUX_REUSSITE_MIN: return None

    score, raison = calculer_score_local(ticker)
    if score < SCORE_MIN: return None

    return {
        **titre, "score_fin": score, "raison_fin": raison, 
        "taux_hist": round(taux*100, 1), "duree_hist": duree, 
        "prix_cible": prix_cible, "fait": fait
    }

def agent_mail(resultats):
    if not resultats: return False
    
    best = sorted(resultats, key=lambda r: (r["score_fin"], r["pea"] == "Oui"), reverse=True)[:MAX_RESULTATS]
    
    lignes = ["Bonjour Charly,\n", "Voici la meilleure pépite européenne détectée aujourd'hui :\n"]
    for r in best:
        nom_complet, isin, pea = infos_entreprise(r['ticker'])
        prix_achat = r["prix"]
        
        lignes += [
            f"🏢 Entreprise : {nom_complet} ({r['ticker']})",
            f"🆔 ISIN : {isin} (Éligible PEA : {pea})",
            f"💰 Achat : {prix_achat} € | Vente (+10% net) : {r['prix_cible']} €",
            f"⏳ Délai estimé : environ {r['duree_hist']} séances (max 30)",
            f"📰 Fait : {r['fait'][:80]}... | 💡 {r['raison_fin']}"
        ]
        
    try:
        envoyer_mail("PÉPITE Bourse — Opportunité Européenne", "\n".join(lignes))
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
        log.info(f"DIAGNOSTIC -> Marketaux Key: {'OK' if MARKETAUX_API_KEY else 'MANQUANT'}")
        return

    # LA CORRECTION EST ICI (plus de 'une_fonce')
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
