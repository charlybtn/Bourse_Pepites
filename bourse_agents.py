#!/usr/bin/env python3
"""
Système d'agents IA pour la bourse - Chasseur de Pépites (Europe + USA).
"""
import argparse, json, logging, math, os, re, smtplib, sys, time, warnings
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

DEST_EMAIL = os.getenv("DEST_EMAIL") or os.getenv("GMAIL_USER") or "charlybotton85600@gmail.com"
GMAIL_USER = os.getenv("GMAIL_USER") or DEST_EMAIL
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

PRIX_MAX = 80.0
SCORE_MIN = 70            
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

# ----------------------------------------------------------------- LLM (Gemini Auto-Detect)
def msg(role, texte): return {"role": role, "parts": [{"text": texte}]}

def gemini_analyse(ticker, nom, prix, rsi):
    if not GEMINI_API_KEY: raise RuntimeError("Clé Gemini manquante")
    
    # 1. On demande à Google la liste des modèles disponibles
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                         headers={"x-goog-api-key": GEMINI_API_KEY}, timeout=30)
        r.raise_for_status()
        noms = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])]
        # On ne garde que les modèles Flash (les plus rapides et gratuits)
        modeles = [n for n in noms if "flash" in n][:5] 
    except Exception:
        # Si la liste échoue, on tente les classiques
        modeles = ["gemini-1.5-flash", "gemini-2.0-flash"]
        
    prompt = (
        f"Tu es un analyste financier expert. Analyse l'entreprise {nom} (ticker {ticker}).\n"
        f"Le cours est actuellement de {prix} et le RSI est de {rsi}.\n"
        "1. Fais une recherche sur l'actualité récente de cette entreprise (moins d'un mois) : "
        "y a-t-il de bonnes nouvelles (contrat, résultats) ou de mauvaises (faillite, scandale) ?\n"
        "2. Donne une note fondamentale sur 100 (où 70 signifie 'solide' et 30 signifie 'dangereux').\n"
        "Réponds STRICTEMENT en JSON : {\"score\": int, \"raison\": \"ton analyse en 2 phrases\"}"
    )
    
    # 2. On teste les modèles jusqu'à ce qu'un fonctionne
    for modele in modeles:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{modele}:generateContent"
        try:
            r = requests.post(url, headers={"x-goog-api-key": GEMINI_API_KEY}, json={"contents": [msg("user", prompt)], "generationConfig": {"responseMimeType": "application/json"}}, timeout=45)
            r.raise_for_status()
            texte = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(re.sub(r"^```(?:json)?|```$", "", texte.strip(), flags=re.M).strip())
        except Exception as e:
            log.debug(f"Gemini {modele} fail: {e}")
            continue
    raise RuntimeError("Tous les modèles Gemini ont échoué")

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
    
    memoire = lire_json(MEMOIRE, {})
    if ticker in memoire:
        derniere_alerte = datetime.fromisoformat(memoire[ticker])
        if datetime.now() - derniere_alerte < timedelta(hours=DELAI_MEMOIRE_HEURES):
            return None

    taux, duree = historique_cible(titre["_h"], GAIN_NET_PCT)
    if taux < TAUX_REUSSITE_MIN: return None

    score = 0
    raison = "Analyse IA non disponible."
    if GEMINI_API_KEY:
        try:
            g = gemini_analyse(ticker, titre["nom"], titre["prix"], titre["rsi"])
            score = int(g.get("score", 0))
            raison = g.get("raison", "")
        except Exception as e:
            log.warning(f"Gemini a échoué pour {ticker} : {e}")
            return None
            
    if score < SCORE_MIN: return None

    return {**titre, "score_gemini": score, "raison_gemini": raison, "taux_hist": round(taux*100, 1), "duree_hist": duree}

def agent_mail(resultats):
    if not resultats: return False
    best = sorted(resultats, key=lambda r: r["score_gemini"], reverse=True)[:MAX_RESULTATS]
    
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
            f"Score IA   : {r['score_gemini']}/100",
            f"",
            f"💡 L'avis de Gemini :",
            f"{r['raison_gemini']}",
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
        print(f"Gemini: {'OK' if GEMINI_API_KEY else 'MANQUANT'}")
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
