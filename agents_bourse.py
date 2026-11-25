#!/usr/bin/env python3
"""
Agents IA pour la bourse - Chasseur de Pépites (Europe, EUR) - VERSION COMPLÈTE CALIBRÉE (révisée).

  python agents_bourse.py --diagnostic   vérifie les identifiants
  python agents_bourse.py --test-mail    envoie un mail de test
  python agents_bourse.py --une-fois     un passage (sur GitHub Actions « schedule » : seulement aux créneaux CRENEAUX_HEURES)
  python agents_bourse.py --forcer       ignore la garde horaire
  python agents_bourse.py                boucle locale (toutes les FREQUENCE_MINUTES, heures de bourse)
"""
import argparse, base64, csv, json, logging, math, os, re, smtplib, sys, time, warnings
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf
import suivi
from yfinance import EquityQuery

warnings.filterwarnings("ignore")

DOSSIER = Path(__file__).resolve().parent
PARIS = ZoneInfo("Europe/Paris")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("bourse")


def maintenant():
    return datetime.now(PARIS)


# ----------------------------------------------------------------- CONFIG
def charger_env():
    f = DOSSIER / ".env"
    if not f.exists():
        return
    for ligne in f.read_text(encoding="utf-8").splitlines():
        if "=" in ligne and not ligne.strip().startswith("#"):
            k, v = ligne.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


charger_env()


def env_bool(nom, defaut):
    return os.getenv(nom, "1" if defaut else "0").strip().lower() in ("1", "true", "oui", "yes")


GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
DEST_EMAIL = os.getenv("DEST_EMAIL") or GMAIL_USER
MARKETAUX_API_KEY = os.getenv("MARKETAUX_API_KEY")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")
GITHUB_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}

# --- Critères de l'utilisateur
PRIX_MAX = 80.0
PRIX_MIN = 1.0
SCORE_MIN = 65
FORMULE = (os.getenv("FORMULE_FORTUNEO") or "starter").lower()
BUDGET_PAR_TRADE = float(os.getenv("BUDGET_PAR_TRADE", "250"))
OBJECTIF_NET = float(os.getenv("OBJECTIF_NET", "0.06"))           # +6 % net de frais et de prélèvements
TAUX_PRELEVEMENTS = float(os.getenv("TAUX_PRELEVEMENTS", "0.172"))
HORIZON_MAX_SEANCES = int(os.getenv("HORIZON_SEANCES", "30"))
TAUX_REUSSITE_MIN = float(os.getenv("TAUX_REUSSITE_MIN", "0.45"))
VOLATILITE_MIN = 1.5
SEUIL_ANALYSTES = 0.60
MIN_ANALYSTES = 3
EVENEMENT_JOURS = 7

# --- Recherche
MAX_DOSSIERS = 3000
PAGES_PAR_PLACE = 10
TAILLE_LOT_TELECHARGEMENT = 50
DELAI_MEMOIRE_JOURS = 90
FREQUENCE_MINUTES = 120                                           # boucle locale uniquement
FENETRE_DEBUT = (8, 0)
FENETRE_FIN = (17, 30)
CRENEAUX_HEURES = (8, 10, 12, 14, 16)                             # GitHub Actions : heures de Paris des passages
CACHE_CANDIDATS_MIN = 60
CACHE_ACTUS_MIN = 60
SOURCE_CANDIDATS = (os.getenv("SOURCE_CANDIDATS") or "auto").lower()
FICHIER_CRIBLE = Path(os.getenv("FICHIER_CRIBLE") or (DOSSIER / "crible_output.csv"))
FONDAMENTAL_LOCAL = env_bool("FONDAMENTAL_LOCAL", True)

# --- Options
ACTUS_OBLIGATOIRES = env_bool("ACTUS_OBLIGATOIRES", False)
SEUIL_SENTIMENT_NEGATIF = -0.10
CIBLE_MAX_PCT = float(os.getenv("CIBLE_MAX_PCT", "30"))
VALEUR_JOUR_MIN = float(os.getenv("VALEUR_JOUR_MIN", "200000"))
SPREAD_MAX_PCT = float(os.getenv("SPREAD_MAX_PCT", "0.8"))
ECART_ACHAT_MAX_PCT = float(os.getenv("ECART_ACHAT_MAX_PCT", "1.0"))
SCANNER_EXPLOSIF = env_bool("SCANNER_EXPLOSIF", True)
ATR_EXPLOSIF_MIN = float(os.getenv("ATR_EXPLOSIF_MIN", "3.0"))
VOLUME_RATIO_MIN = float(os.getenv("VOLUME_RATIO_MIN", "2.0"))
GAP_EPISODE = 5
MIN_EPISODES = int(os.getenv("MIN_EPISODES", "7"))
ANALYSTES_STRICT = env_bool("ANALYSTES_STRICT", False)
FILTRE_MARCHE = env_bool("FILTRE_MARCHE", False)

MARCHE_INDICE = "^STOXX50E"
CHUTE_MARCHE_MAX_PCT = -3.0
MARKETAUX_MAX_REQUETES = 90
PAYS_ENTITES_EUROPE = {"fr", "de", "nl", "be", "it", "es", "pt", "fi", "at", "gr", "ie", "lu"}

PAYS_PEA = {
    "FR", "DE", "NL", "IT", "ES", "BE", "PT", "IE", "LU", "FI", "AT", "GR",
    "SE", "DK", "PL", "CZ", "HU", "RO", "BG", "HR", "SK", "SI", "EE", "LV", "LT",
    "MT", "CY", "NO", "IS", "LI",
}
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
SUFFIXES = (".PA", ".AS", ".BR", ".DE", ".MI", ".MC", ".LS", ".HE", ".VI", ".AT")

PLACES = [
    ("PAR", "France"), ("GER", "Allemagne"), ("AMS", "Pays-Bas"), ("BRU", "Belgique"),
    ("MIL", "Italie"), ("MCE", "Espagne"), ("LIS", "Portugal"), ("HEL", "Finlande"),
    ("VIE", "Autriche"), ("ATH", "Grèce"),
]

TICKERS_SECOURS = [
    "GTT.PA", "SPIE.PA", "SW.PA", "TE.PA", "UBI.PA", "VAC.PA", "VK.PA", "AC.PA",
    "ADP.PA", "AF.PA", "EDEN.PA", "TEP.PA", "ERF.PA", "WLN.PA", "ALO.PA", "ALT.PA",
    "ALTA.PA", "ATO.PA", "BB.PA", "BOL.PA", "BVI.PA", "FR.PA", "ICAD.PA", "IPH.PA",
    "KOF.PA", "LI.PA", "MAU.PA", "NEX.PA", "NK.PA", "RXL.PA", "SK.PA", "SO.PA",
    "ASML.AS", "AD.AS", "PHIA.AS", "HEIA.AS", "ABI.BR", "KBC.BR", "UCB.BR",
]

# ----------------------------------------------------------------- SUIVI
STATS = {}
ANOMALIES = []
_anomalies_envoyees = set()
_cache_candidats = {"heure": None, "donnees": None}


def compter(cle, n=1):
    STATS[cle] = STATS.get(cle, 0) + n


def anomalie(msg):
    log.warning("ANOMALIE : %s", msg)
    ANOMALIES.append(msg)


# ----------------------------------------------------------------- MAIL
def envoyer_mail(sujet, corps):
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        raise RuntimeError(
            f"Identifiants manquants (User: {bool(GMAIL_USER)}, Pwd: {bool(GMAIL_APP_PASSWORD)})"
        )
    if not DEST_EMAIL:
        raise RuntimeError("Aucune adresse de destination (DEST_EMAIL ou GMAIL_USER).")
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = sujet, GMAIL_USER, DEST_EMAIL
    m.set_content(corps)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.send_message(m)
    log.info("Mail envoyé : %s", sujet)


# ----------------------------------------------------------------- MÉMOIRE DES ALERTES
FICHIER_MEMOIRE_LOCAL = DOSSIER / "memoire_alertes.json"
_MEMOIRE_SESSION = {}


def _github_actif():
    return bool(GITHUB_TOKEN and GITHUB_REPOSITORY)


def _url_memoire():
    return f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/memoire_alertes.json"


def _requete_github(methode, url, **kw):
    """Appel GitHub avec 3 essais (erreurs réseau ou serveur)."""
    derniere = None
    for essai in range(3):
        try:
            r = requests.request(methode, url, headers=GITHUB_HEADERS, timeout=20, **kw)
            if r.status_code < 500:
                return r
            derniere = RuntimeError(f"code {r.status_code}")
        except requests.RequestException as e:
            derniere = e
        time.sleep(2 * (essai + 1))
    raise derniere


def _date_alerte(valeur):
    d = datetime.fromisoformat(valeur)
    return d if d.tzinfo else d.replace(tzinfo=PARIS)


def _purger(memoire):
    limite = maintenant() - timedelta(days=DELAI_MEMOIRE_JOURS)
    propre = {}
    for k, v in memoire.items():
        try:
            if _date_alerte(v) >= limite:
                propre[k] = v
        except Exception:
            continue
    return propre


def _lire_memoire_et_sha():
    try:
        r = _requete_github("GET", _url_memoire())
        if r.status_code == 404:
            return {}, None
        r.raise_for_status()
        j = r.json()
        contenu = base64.b64decode(j["content"]).decode("utf-8")
        return json.loads(contenu), j.get("sha")
    except Exception as e:
        anomalie(f"Lecture de la mémoire GitHub impossible ({type(e).__name__}).")
        return None, None


def mettre_a_jour_memoire_github(tickers):
    memoire, sha = _lire_memoire_et_sha()
    if memoire is None:
        return
    for t in tickers:
        memoire[t] = maintenant().isoformat()
    memoire = _purger(memoire)
    contenu = base64.b64encode(json.dumps(memoire, indent=1).encode("utf-8")).decode("utf-8")
    payload = {"message": f"Mémoire: {', '.join(tickers)}", "content": contenu}
    if sha:
        payload["sha"] = sha
    try:
        r = _requete_github("PUT", _url_memoire(), json=payload)
        if r.status_code in (200, 201):
            log.info("Mémoire GitHub mise à jour (%s).", ", ".join(tickers))
        else:
            anomalie(f"Écriture de la mémoire GitHub refusée (code {r.status_code}).")
    except Exception as e:
        anomalie(f"Écriture de la mémoire GitHub impossible ({type(e).__name__}).")


def _lire_memoire_locale():
    try:
        if FICHIER_MEMOIRE_LOCAL.exists():
            return json.loads(FICHIER_MEMOIRE_LOCAL.read_text(encoding="utf-8"))
    except Exception as e:
        anomalie(f"Lecture de la mémoire locale impossible ({type(e).__name__}).")
    return {}


def _ecrire_memoire_locale(memoire):
    try:
        FICHIER_MEMOIRE_LOCAL.write_text(json.dumps(memoire, indent=1), encoding="utf-8")
    except Exception as e:
        anomalie(f"Écriture de la mémoire locale impossible ({type(e).__name__}).")


def lire_memoire():
    if _github_actif():
        memoire, _ = _lire_memoire_et_sha()
    else:
        memoire = _lire_memoire_locale()
    memoire = _purger(memoire or {})
    memoire.update(_purger(_MEMOIRE_SESSION))
    return memoire


def memoriser_alerte(ticker):
    _MEMOIRE_SESSION[ticker] = maintenant().isoformat()
    if _github_actif():
        mettre_a_jour_memoire_github([ticker])
    else:
        memoire = _lire_memoire_locale()
        memoire[ticker] = maintenant().isoformat()
        _ecrire_memoire_locale(_purger(memoire))


# ----------------------------------------------------------------- FRAIS & PRIX CIBLE
def frais(montant):
    if FORMULE == "perso":
        return max(float(os.getenv("FRAIS_MIN", "0")), montant * float(os.getenv("FRAIS_POURCENT", "0")) / 100)
    if FORMULE == "starter":
        return montant * 0.0035
    if FORMULE == "trader":
        return 9.50 if montant <= 10000 else montant * 0.0010
    return 4.90 if montant <= 3000 else montant * 0.0015


def quantite_pour(prix):
    return max(1, int(BUDGET_PAR_TRADE // prix))


def prix_minimum_pour_objectif_net(prix_achat):
    n = quantite_pour(prix_achat)
    valeur_achat = n * prix_achat
    cout_total = valeur_achat + frais(valeur_achat)
    gain_brut = cout_total * OBJECTIF_NET / (1 - TAUX_PRELEVEMENTS)
    a_encaisser = cout_total + gain_brut
    p = a_encaisser / n
    for _ in range(6):
        p = (a_encaisser + frais(n * p)) / n
    return math.ceil(p * 100) / 100


# ----------------------------------------------------------------- TECHNIQUE
def seance_en_cours(h):
    """Vrai si la dernière ligne de l'historique est la séance du jour, encore ouverte (donc incomplète)."""
    n = maintenant()
    return n.weekday() < 5 and (9, 0) <= (n.hour, n.minute) < (17, 35) and h.index[-1].date() == n.date()


def flags_serie(h):
    c, vol = h["Close"], h["Volume"]
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    perte = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + gain / perte)
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    hist = macd - macd.ewm(span=9, adjust=False).mean()
    var5 = c / c.shift(5) - 1
    repli = c / c.rolling(20).max() - 1
    f = {
        "tendance": (c > c.rolling(200).mean()).fillna(False),
        "repli": ((var5 <= 0.01) & (repli >= -0.15)).fillna(False),
        "rsi": rsi.between(28, 62).fillna(False),
        "macd": ((hist > hist.shift(1)) & (hist.shift(1) > hist.shift(2))).fillna(False),
        "volume": (vol.rolling(20).mean() >= 50000).fillna(False),
    }
    return f, rsi, var5, repli


def voyants(h):
    if h is None or len(h) < 210:
        return None
    f, rsi, var5, repli = flags_serie(h)
    flags = {k: bool(s.iloc[-1]) for k, s in f.items()}
    signal = pd.concat(list(f.values()), axis=1).all(axis=1)

    c = h["Close"]
    tr = pd.concat(
        [h["High"] - h["Low"], (h["High"] - c.shift(1)).abs(), (h["Low"] - c.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(14).mean().iloc[-1]
    volatilite_pct = float(atr / c.iloc[-1] * 100)

    return {
        "flags": flags, "voyants_ok": sum(flags.values()), "tous_verts": all(flags.values()),
        "rsi": round(float(rsi.iloc[-1]), 1), "var5_pct": round(float(var5.iloc[-1]) * 100, 2),
        "repli_pct": round(float(repli.iloc[-1]) * 100, 2), "volatilite": round(volatilite_pct, 2),
        "signal": signal, "h": h,
    }


def signal_explosif(h):
    """Cassure du plus haut des 20 séances précédentes + volume + volatilité + au-dessus de la MM50."""
    c, hi, bas, vol = h["Close"], h["High"], h["Low"], h["Volume"]
    cassure = c > hi.shift(1).rolling(20).max()
    volume = vol >= VOLUME_RATIO_MIN * vol.shift(1).rolling(20).mean()
    tr = pd.concat([hi - bas, (hi - c.shift(1)).abs(), (bas - c.shift(1)).abs()], axis=1).max(axis=1)
    atr_pct = tr.rolling(14).mean() / c * 100
    return (cassure & volume & (atr_pct >= ATR_EXPLOSIF_MIN) & (c > c.rolling(50).mean())).fillna(False)


def indices_cas(h, signal=None):
    """Un seul cas par épisode de signal ; repli sur tous les jours s'il y a moins de MIN_EPISODES épisodes."""
    n = len(h) - HORIZON_MAX_SEANCES
    if n <= 0:
        return [], "aucun"
    if signal is not None:
        debuts, dernier = [], -10 ** 9
        for i in range(n):
            if bool(signal.iloc[i]):
                if i - dernier > GAP_EPISODE:
                    debuts.append(i)
                dernier = i
        if len(debuts) >= MIN_EPISODES:
            return debuts, "épisodes"
    return list(range(n)), "global"


def borne_basse(k, n, z=1.28):
    if n <= 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    return (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d


def historique_cible(h, hausse_pct, signal=None):
    hi, cl = h["High"].values, h["Close"].values
    idx, mode = indices_cas(h, signal)
    if not idx:
        return 0.0, 0, 0, "aucun", 0.0
    ok, durees = 0, []
    max_len = len(hi)
    for i in idx:
        cible = cl[i] * (1 + hausse_pct / 100)
        fin_fenetre = min(i + 1 + HORIZON_MAX_SEANCES, max_len)
        for j in range(i + 1, fin_fenetre):
            if hi[j] >= cible:
                ok += 1
                durees.append(j - i)
                break
    mediane = sorted(durees)[len(durees) // 2] if durees else 0
    return ok / len(idx), mediane, len(idx), mode, borne_basse(ok, len(idx))


def niveau_probable(h, signal=None):
    hi, cl = h["High"].values, h["Close"].values
    idx, mode = indices_cas(h, signal)
    if not idx:
        return None, 0, "aucun"
    max_len = len(hi)
    exc = []
    for i in idx:
        fin_fenetre = min(i + 1 + HORIZON_MAX_SEANCES, max_len)
        if i + 1 < fin_fenetre:
            exc.append((hi[i + 1:fin_fenetre].max() / cl[i] - 1) * 100)
    if not exc:
        return None, 0, "aucun"
    return float(np.percentile(exc, (1 - TAUX_REUSSITE_MIN) * 100)), len(exc), mode


def gain_net_vise(prix, prix_cible, q):
    achat, vente = prix * q, prix_cible * q
    cout = achat + frais(achat)
    brut = (vente - frais(vente)) - cout
    net = brut * (1 - TAUX_PRELEVEMENTS) if brut > 0 else brut
    return net, net / cout * 100


def actualites_yahoo(t, n=3, jours=7):
    """Derniers titres Yahoo Finance (moins de `jours` jours) sur l'entreprise."""
    try:
        titres = []
        for item in (t.news or [])[:15]:
            c = item.get("content", item) or {}
            titre = c.get("title")
            if not titre:
                continue
            try:
                if c.get("pubDate") or c.get("displayTime"):
                    d = datetime.fromisoformat(str(c.get("pubDate") or c.get("displayTime")).replace("Z", "+00:00"))
                elif item.get("providerPublishTime"):
                    d = datetime.fromtimestamp(item["providerPublishTime"], PARIS)
                else:
                    d = None
                if d is not None and maintenant() - d > timedelta(days=jours):
                    continue
            except (ValueError, TypeError, OSError):
                pass
            titres.append(str(titre)[:140])
            if len(titres) >= n:
                break
        return titres
    except Exception:
        return []


def telecharger_historiques(tickers):
    resultat = {}
    for i in range(0, len(tickers), TAILLE_LOT_TELECHARGEMENT):
        lot = tickers[i:i + TAILLE_LOT_TELECHARGEMENT]
        if i > 0 and (i // TAILLE_LOT_TELECHARGEMENT) % 10 == 0:
            log.info("  ... %d/%d historiques téléchargés ...", i, len(tickers))
        df = None
        for essai in range(3):
            try:
                df = yf.download(lot, period="3y", interval="1d", auto_adjust=True,
                                 group_by="ticker", threads=True, progress=False)
                break
            except Exception as e:
                log.warning("Téléchargement raté (essai %d) : %s", essai + 1, type(e).__name__)
                time.sleep(5 * (essai + 1))
        if df is None or df.empty:
            compter("lots_telechargement_vides")
            continue
        for t in lot:
            try:
                h = df[t] if isinstance(df.columns, pd.MultiIndex) else df
                h = h.dropna(subset=["Close"])
                if not h.empty:
                    resultat[t] = h
            except KeyError:
                continue
        time.sleep(1)
    return resultat


# ----------------------------------------------------------------- MARCHÉ, CALENDRIER, ANALYSTES
def marche_ok():
    try:
        c = yf.Ticker(MARCHE_INDICE).history(period="1y", interval="1d", auto_adjust=True)["Close"].dropna()
        if len(c) < 200:
            return True, "Indice : données insuffisantes (non bloquant)."
        chute = (c.iloc[-1] / c.iloc[-2] - 1) * 100
        if chute <= CHUTE_MARCHE_MAX_PCT:
            return False, f"Disjoncteur : l'indice perd {chute:.1f} % aujourd'hui."
        if FILTRE_MARCHE and c.iloc[-1] < c.rolling(200).mean().iloc[-1]:
            return False, "Le marché est sous sa moyenne mobile 200 jours."
        return True, "Marché favorable."
    except Exception as e:
        return True, f"Indice indisponible (non bloquant) : {type(e).__name__}"


def evenement_proche(t):
    try:
        cal = t.calendar
    except Exception:
        return None
    if not isinstance(cal, dict):
        return None
    aujourdhui = maintenant().date()
    limite = aujourdhui + timedelta(days=EVENEMENT_JOURS)
    for cle, libelle in (("Earnings Date", "résultats"), ("Ex-Dividend Date", "détachement de dividende")):
        valeurs = cal.get(cle)
        if valeurs is None:
            continue
        if not isinstance(valeurs, (list, tuple)):
            valeurs = [valeurs]
        for v in valeurs:
            d = v.date() if isinstance(v, datetime) else v
            if isinstance(d, date) and aujourdhui <= d <= limite:
                return f"{libelle} le {d}"
    return None


def avis_analystes(t):
    try:
        rs = t.recommendations_summary
        if rs is None or rs.empty:
            return None, 0
        ligne = rs.iloc[0]
        achat = float(ligne.get("strongBuy", 0)) + float(ligne.get("buy", 0))
        total = achat + sum(float(ligne.get(k, 0)) for k in ("hold", "sell", "strongSell"))
        if total <= 0:
            return None, 0
        return achat / total, int(total)
    except Exception:
        return None, 0


# ----------------------------------------------------------------- FONDAMENTAL
def val(df, nom, i=0):
    try:
        v = df.loc[nom].iloc[i]
        return None if v != v else float(v)
    except Exception:
        return None


def ratio(a, b):
    return None if a is None or b in (None, 0) else a / b


def gt(a, b):
    return None if a is None or b is None else a > b


def le(a, b):
    return None if a is None or b is None else a <= b


def calculer_score_local(t, info):
    try:
        bil, res, tre = t.balance_sheet, t.income_stmt, t.cashflow
    except Exception:
        return None, "Données financières indisponibles."
    if any(x is None or x.empty for x in (bil, res, tre)) or len(bil.columns) < 2 or len(res.columns) < 2:
        return None, "Données financières insuffisantes."

    ni0, ni1 = val(res, "Net Income", 0), val(res, "Net Income", 1)
    ta0, ta1 = val(bil, "Total Assets", 0), val(bil, "Total Assets", 1)
    roa0, roa1 = ratio(ni0, ta0), ratio(ni1, ta1)
    ocf0 = val(tre, "Operating Cash Flow", 0)
    dette0, dette1 = val(bil, "Long Term Debt", 0), val(bil, "Long Term Debt", 1)
    cr0 = ratio(val(bil, "Current Assets", 0), val(bil, "Current Liabilities", 0))
    cr1 = ratio(val(bil, "Current Assets", 1), val(bil, "Current Liabilities", 1))
    sh0, sh1 = val(bil, "Ordinary Shares Number", 0), val(bil, "Ordinary Shares Number", 1)
    marge0 = ratio(val(res, "Gross Profit", 0), val(res, "Total Revenue", 0))
    marge1 = ratio(val(res, "Gross Profit", 1), val(res, "Total Revenue", 1))
    rot0 = ratio(val(res, "Total Revenue", 0), ta0)
    rot1 = ratio(val(res, "Total Revenue", 1), ta1)

    criteres = [
        gt(roa0, 0), gt(ocf0, 0), gt(roa0, roa1), gt(ocf0, ni0), le(dette0, dette1),
        gt(cr0, cr1), le(sh0, sh1), gt(marge0, marge1), gt(rot0, rot1),
    ]
    dispo = [c for c in criteres if c is not None]
    if len(dispo) < 6:
        return None, f"Piotroski incomplet ({len(dispo)}/9 critères calculables)."
    f = sum(dispo) * 9 / len(dispo)

    z = None
    if (info.get("sector") or "") != "Financial Services":
        ca, cl = val(bil, "Current Assets", 0), val(bil, "Current Liabilities", 0)
        fonds_roulement = None if ca is None or cl is None else ca - cl
        x1 = ratio(fonds_roulement, ta0)
        x2 = ratio(val(bil, "Retained Earnings", 0), ta0)
        x3 = ratio(val(res, "EBIT", 0), ta0)
        x4 = ratio(val(bil, "Total Equity Gross Minority Interest", 0),
                   val(bil, "Total Liabilities Net Minority Interest", 0))
        if None not in (x1, x2, x3, x4):
            z = 3.25 + 6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4
    z_txt = f"Z'' {z:.1f}" if z is not None else "Z'' non applicable"

    if f >= 7 and (z is None or z > 2.6):
        return 90, f"Très solide (F-Score {f:.0f}/9, {z_txt})."
    if f >= 5 and (z is None or z > 1.1):
        return 65, f"Rentable et saine (F-Score {f:.0f}/9, {z_txt})."
    return 30, f"Risqué (F-Score {f:.0f}/9, {z_txt})."


def fiche(t):
    try:
        return t.info or {}
    except Exception:
        return {}


PAYS_PEA_NOMS = {
    "france", "germany", "netherlands", "italy", "spain", "belgium", "portugal", "ireland", "luxembourg",
    "finland", "austria", "greece", "sweden", "denmark", "poland", "czech republic", "czechia", "hungary",
    "romania", "bulgaria", "croatia", "slovakia", "slovenia", "estonia", "latvia", "lithuania", "malta",
    "cyprus", "norway", "iceland", "liechtenstein",
}


def infos_entreprise(t, info, ticker):
    """(nom, ISIN ou 'Inconnu', éligibilité PEA, source de l'éligibilité).
    L'ISIN de yfinance est capricieux : 3 essais. S'il reste introuvable, l'éligibilité PEA est déduite du pays de
    l'entreprise (source « pays », à confirmer) au lieu d'écarter le titre."""
    nom = info.get("longName") or info.get("shortName") or ticker
    isin = None
    for essai in range(3):
        try:
            v = t.isin
            if v and ISIN_RE.match(str(v)):
                isin = str(v)
                break
        except Exception:
            pass
        time.sleep(1.5 * (essai + 1))
    if isin:
        return nom, isin, ("Oui" if isin[:2] in PAYS_PEA else "Non"), "isin"
    if str(info.get("country") or "").strip().lower() in PAYS_PEA_NOMS:
        return nom, "Inconnu", "Oui", "pays"
    return nom, "Inconnu", "Inconnu", "aucune"


# ----------------------------------------------------------------- ACTUALITÉS (Marketaux)
_marketaux_jour = {"date": None, "n": 0}
_cache_actus = {}


def _quota_marketaux_restant():
    auj = maintenant().date()
    if _marketaux_jour["date"] != auj:
        _marketaux_jour["date"], _marketaux_jour["n"] = auj, 0
    return MARKETAUX_MAX_REQUETES - _marketaux_jour["n"]


def _entite_correspond(ent, ticker):
    """Symbole exact, ou racine du symbole seulement si l'entité est européenne
    (évite de confondre SW.PA avec « SW » ou AAT.MI avec « AAT », des titres américains)."""
    sym = str(ent.get("symbol", "")).upper()
    if sym == ticker.upper():
        return True
    return sym == ticker.split(".")[0].upper() and str(ent.get("country", "")).lower() in PAYS_ENTITES_EUROPE


def _interroger_marketaux(ticker):
    if not MARKETAUX_API_KEY:
        return None, "Clé Marketaux manquante.", False
    if _quota_marketaux_restant() <= 0:
        return None, "Quota Marketaux du jour atteint.", False
    _marketaux_jour["n"] += 1

    depuis = (maintenant() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M")
    params = {
        "symbols": ticker, "filter_entities": "true", "language": "fr,en,de,es,it",
        "published_after": depuis, "api_token": MARKETAUX_API_KEY,
    }
    try:
        r = requests.get("https://api.marketaux.com/v1/news/all", params=params, timeout=15)
    except requests.RequestException as e:
        return None, f"Erreur réseau Marketaux ({type(e).__name__})", False
    if r.status_code == 429:
        _marketaux_jour["n"] = MARKETAUX_MAX_REQUETES
        anomalie("Quota Marketaux dépassé (429).")
        return None, "Quota Marketaux dépassé.", False
    if r.status_code != 200:
        return None, f"Erreur Marketaux (code {r.status_code})", False

    try:
        articles = r.json().get("data", [])
    except ValueError:
        return None, "Réponse Marketaux illisible.", False
    if not articles:
        return None, "Aucun article trouvé.", True

    notes = []
    for art in articles:
        for ent in art.get("entities", []):
            if _entite_correspond(ent, ticker) and ent.get("sentiment_score") is not None:
                try:
                    notes.append((float(ent["sentiment_score"]), art.get("title")))
                except (ValueError, TypeError):
                    continue
    if not notes:
        return None, "Pas de score de sentiment pour cette action.", True

    moyen = sum(s for s, _ in notes) / len(notes)
    return {"moyen": moyen, "titres": sorted(notes, key=lambda x: x[0])}, f"Sentiment moyen : {moyen:.2f}", True


def analyser_actualites_marketaux(ticker):
    maint = maintenant()
    en_cache = _cache_actus.get(ticker)
    if en_cache and maint - en_cache[0] < timedelta(minutes=CACHE_ACTUS_MIN):
        return en_cache[1], en_cache[2]
    info, msg, definitif = _interroger_marketaux(ticker)
    if definitif:
        _cache_actus[ticker] = (maint, info, msg)
    return info, msg


# ----------------------------------------------------------------- AGENTS
def agent_screener_yahoo():
    candidats = {}
    for exchange, pays in PLACES:
        q = EquityQuery("and", [
            EquityQuery("eq", ["exchange", exchange]),
            EquityQuery("lt", ["intradayprice", PRIX_MAX]),
            EquityQuery("gt", ["intradayprice", PRIX_MIN]),
            EquityQuery("gt", ["avgdailyvol3m", 30000]),
        ])
        for page in range(PAGES_PAR_PLACE):
            try:
                log.info("Agent 1 : %s (page %d/%d)...", pays, page + 1, PAGES_PAR_PLACE)
                res = yf.screen(q, size=100, offset=page * 100,
                                sortField="intradaymarketcap", sortAsc=False)
            except Exception as e:
                log.error("Échec du screener (%s, page %d) : %s", pays, page + 1, type(e).__name__)
                compter("erreurs_screener")
                break
            quotes = res.get("quotes", [])
            for quote in quotes:
                sym = quote.get("symbol", "")
                if quote.get("quoteType") != "EQUITY" or not sym.endswith(SUFFIXES):
                    compter("rejet_suffixe")
                    continue
                if quote.get("currency") not in (None, "EUR"):
                    compter("rejet_devise")
                    continue
                prix = quote.get("regularMarketPrice")
                volume = quote.get("averageDailyVolume3Month") or 0
                candidats[sym] = {"prix": prix, "liquidite": (prix or 0) * volume}
            if len(quotes) < 100:
                break
            time.sleep(2)

    if not candidats:
        anomalie("Le screener Yahoo n'a rien renvoyé : utilisation de la liste de secours.")
        return {t: {"prix": None, "liquidite": 0} for t in TICKERS_SECOURS}, True

    tries = sorted(candidats, key=lambda s: candidats[s]["liquidite"], reverse=True)
    garde = {s: candidats[s] for s in tries[:MAX_DOSSIERS]}
    log.info("Agent 1 : %d titres en EUR trouvés, %d conservés.", len(candidats), len(garde))
    return garde, False


def source_candidats():
    if SOURCE_CANDIDATS == "auto":
        return "crible" if FICHIER_CRIBLE.exists() else "yahoo"
    return SOURCE_CANDIDATS


def lire_crible():
    if not FICHIER_CRIBLE.exists():
        return {}
    try:
        with open(FICHIER_CRIBLE, newline="", encoding="utf-8-sig") as f:
            echantillon = f.read(4096)
            f.seek(0)
            try:
                dialecte = csv.Sniffer().sniff(echantillon, delimiters=",;\t")
            except csv.Error:
                dialecte = csv.excel
            lecteur = csv.DictReader(f, dialect=dialecte)
            colonnes = {(c or "").strip().lower(): c for c in (lecteur.fieldnames or [])}
            col = next((colonnes[n] for n in ("symbol", "ticker", "symbole", "code") if n in colonnes), None)
            if col is None:
                anomalie("Crible : aucune colonne « symbol » ou « ticker » dans le fichier CSV.")
                return {}
            symboles = [(row.get(col) or "").strip().upper() for row in lecteur]
    except Exception as e:
        anomalie(f"Lecture du fichier Crible impossible ({type(e).__name__}).")
        return {}

    candidats, rejetes = {}, []
    for sym in symboles:
        if not sym:
            continue
        if sym.endswith(SUFFIXES):
            candidats[sym] = {"prix": None, "liquidite": 0}
        else:
            rejetes.append(sym)
    compter("rejet_suffixe", len(rejetes))
    if rejetes:
        log.warning("Crible : %d symboles sans suffixe Yahoo d'une place en EUR (ex. %s).",
                    len(rejetes), ", ".join(rejetes[:3]))
    if len(rejetes) > len(candidats):
        anomalie("Crible : plus de la moitié des symboles n'ont pas de suffixe Yahoo reconnu (ex. GTT au lieu de GTT.PA).")
    return dict(list(candidats.items())[:MAX_DOSSIERS])


def agent_actualites():
    if source_candidats() == "yahoo":
        return agent_screener_yahoo()
    candidats = lire_crible()
    if candidats:
        log.info("Agent 1 : %d candidats lus dans %s.", len(candidats), FICHIER_CRIBLE.name)
        return candidats, False
    anomalie(f"{FICHIER_CRIBLE.name} introuvable, vide ou illisible : liste de secours utilisée.")
    return {t: {"prix": None, "liquidite": 0} for t in TICKERS_SECOURS}, True


def agent_filtre(candidats):
    """Signaux calculés sur les SÉANCES TERMINÉES. Si la séance du jour est en cours, ils sont aussi confirmés
    sur elle : 5 voyants encore tous verts, ou, pour une cassure, cours qui tient (pas de recul de plus de 2 %
    sur la veille, toujours au-dessus de la MM50). Le prix d'entrée est le cours du moment."""
    historiques = telecharger_historiques(list(candidats))
    compter("historiques_recus", len(historiques))
    retenus = []
    for ticker, h_full in historiques.items():
        live = len(h_full) > 211 and seance_en_cours(h_full)
        h = h_full.iloc[:-1] if live else h_full
        ana = voyants(h)
        if ana is None:
            compter("rejet_historique_court")
            continue
        ana["strategie"] = "5 voyants"
        if not ana["tous_verts"]:
            sig_exp = signal_explosif(h) if SCANNER_EXPLOSIF else None
            if sig_exp is None or not bool(sig_exp.iloc[-1]):
                compter("rejet_voyants")
                continue
            ana.update({"strategie": "explosif", "signal": sig_exp})
            compter("candidats_explosifs")
        if live:
            if ana["strategie"] == "5 voyants":
                a_live = voyants(h_full)
                confirme = a_live is not None and a_live["tous_verts"]
            else:
                c_fait, c_live = float(h["Close"].iloc[-1]), float(h_full["Close"].iloc[-1])
                confirme = c_live >= 0.98 * c_fait and c_live > float(h_full["Close"].rolling(50).mean().iloc[-1])
            if not confirme:
                compter("rejet_confirmation_live")
                continue
        ana["live"] = live
        prix = round(float(h_full["Close"].iloc[-1]), 3)
        if not (PRIX_MIN <= prix <= PRIX_MAX):
            compter("rejet_prix")
            continue
        if float((h["Close"] * h["Volume"]).rolling(20).mean().iloc[-1]) < VALEUR_JOUR_MIN:
            compter("rejet_liquidite")
            continue
        if ana["volatilite"] < VOLATILITE_MIN:
            compter("rejet_volatilite")
            continue
        prec = float(h_full["Close"].iloc[-2])
        ana.update({"ticker": ticker, "prix": prix, "var_pct": round((prix / prec - 1) * 100, 2)})
        retenus.append(ana)
        log.info("  -> %s validé (%s%s) ! Prix: %s €", ticker, ana["strategie"],
                 ", confirmé en séance" if live else "", prix)
    compter("passent_filtre_technique", len(retenus))
    return retenus


def agent_analyse_fondamentale(titre, memoire):
    ticker = titre["ticker"]

    if ticker in memoire:
        compter("rejet_deja_alerte")
        return None

    prix_min = prix_minimum_pour_objectif_net(titre["prix"])
    hausse_min_pct = (prix_min / titre["prix"] - 1) * 100
    niveau, nb_cas, mode = niveau_probable(titre["h"], titre["signal"])
    log.info("  -> %s backtest %s (%d cas) : hausse probable +%s %% (minimum requis +%.1f %%)",
             ticker, mode, nb_cas, "n/a" if niveau is None else f"{niveau:.1f}", hausse_min_pct)
    if niveau is None or niveau < hausse_min_pct:
        compter("rejet_backtest")
        return None
    niveau = math.floor(min(niveau, CIBLE_MAX_PCT) * 2) / 2
    prix_cible = max(prix_min, round(titre["prix"] * (1 + niveau / 100), 2))
    hausse_pct = (prix_cible / titre["prix"] - 1) * 100
    taux, duree, _, _, borne = historique_cible(titre["h"], hausse_pct, titre["signal"])
    quantite = quantite_pour(titre["prix"])
    gain_eur, gain_pct = gain_net_vise(titre["prix"], prix_cible, quantite)

    t = yf.Ticker(ticker)

    evt = evenement_proche(t)
    if evt:
        log.info("  -> %s écarté : %s", ticker, evt)
        compter("rejet_calendrier")
        return None

    info = fiche(t)
    if FONDAMENTAL_LOCAL:
        score, raison = calculer_score_local(t, info)
        if score is None:
            log.info("  -> %s écarté : %s", ticker, raison)
            compter("rejet_fondamentaux_indisponibles")
            return None
        if score < SCORE_MIN:
            compter("rejet_fondamentaux")
            return None
    else:
        score, raison = 100, "Fondamentaux validés par Crible"

    bid, ask = info.get("bid"), info.get("ask")
    spread_pct = None
    if bid and ask and bid > 0 and ask > 0:
        spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
        if spread_pct > SPREAD_MAX_PCT:
            compter("rejet_spread")
            return None
    prix_achat_max = round(titre["prix"] * (1 + ECART_ACHAT_MAX_PCT / 100), 2)
    gain_max_eur, _ = gain_net_vise(prix_achat_max, prix_cible, quantite)

    nom, isin, pea, source_pea = infos_entreprise(t, info, ticker)
    if pea == "Inconnu":
        compter("rejet_pea_indetermine")
        return None
    if pea != "Oui":
        compter("rejet_non_pea")
        return None

    pct, nb_analystes = avis_analystes(t)
    if pct is None or nb_analystes < MIN_ANALYSTES:
        if ANALYSTES_STRICT:
            compter("rejet_sans_analystes")
            return None
        txt_analystes = "pas de couverture analyste"
    else:
        if pct < SEUIL_ANALYSTES:
            compter("rejet_analystes")
            return None
        txt_analystes = f"{pct * 100:.0f} % d'avis à l'achat ({nb_analystes} analystes)"

    info_actu, msg_actu = analyser_actualites_marketaux(ticker)
    titres_y = actualites_yahoo(t)
    log.info("  -> %s actualités : %s | %d titre(s) Yahoo", ticker, msg_actu, len(titres_y))
    if info_actu and (info_actu["moyen"] <= SEUIL_SENTIMENT_NEGATIF or info_actu["titres"][0][0] <= -0.4):
        compter("rejet_actualites_negatives")
        return None
    if not info_actu and ACTUS_OBLIGATOIRES:
        compter("rejet_actualites")
        return None
    lignes_actus = []
    if info_actu:
        lignes_actus.append(f"Marketaux (7 jours) : sentiment moyen {info_actu['moyen']:+.2f} sur {len(info_actu['titres'])} article(s)")
        lignes_actus += [f"  - [{s:+.1f}] {tt or '(sans titre)'}" for s, tt in info_actu["titres"][:3]]
    else:
        lignes_actus.append(f"Marketaux : aucune actualité exploitable ({msg_actu}). Vérifie les actualités avant d'acheter.")
    if titres_y:
        lignes_actus += ["Yahoo Finance :"] + [f"  - {tt}" for tt in titres_y]
    fait = "\n".join(lignes_actus)

    return {
        "ticker": ticker, "nom": nom, "isin": isin, "pea": pea, "source_pea": source_pea, "prix": titre["prix"],
        "quantite": quantite,
        "score_fin": score, "raison_fin": raison, "taux_hist": round(taux * 100, 1),
        "duree_hist": duree, "mode_backtest": mode, "prix_cible": prix_cible,
        "hausse_pct": hausse_pct, "gain_net_eur": gain_eur, "gain_net_pct": gain_pct,
        "strategie": titre.get("strategie", "5 voyants"), "signal_live": titre.get("live", False),
        "prix_achat_max": prix_achat_max, "gain_net_max_eur": gain_max_eur,
        "spread_pct": spread_pct, "episodes": nb_cas, "borne_basse": borne * 100,
        "fait": fait, "analystes": txt_analystes,
    }


def verifier_resultat(r):
    problemes = []
    if not r.get("prix") or r["prix"] <= 0 or r["prix"] > PRIX_MAX:
        problemes.append("prix d'achat invalide")
    if r["prix_cible"] <= (r.get("prix") or 0):
        problemes.append("prix de vente <= prix d'achat")
    if r["isin"] != "Inconnu" and not ISIN_RE.match(r["isin"]):
        problemes.append("ISIN invalide")
    if r["pea"] != "Oui":
        problemes.append("non éligible PEA")
    if not r["duree_hist"] or r["duree_hist"] > HORIZON_MAX_SEANCES:
        problemes.append("durée estimée invalide")
    return problemes


def formater_duree(seances):
    jours = max(1, round(seances * 7 / 5))
    if jours < 30:
        return f"{jours} jour{'s' if jours > 1 else ''}"
    return f"{max(1, round(jours / 30))} mois"


def envoyer_pepite(r, memoire):
    pb = verifier_resultat(r)
    if pb:
        anomalie(f"Résultat {r['ticker']} bloqué avant envoi : {', '.join(pb)}.")
        return False
    txt_cout = "frais et impôts compris" if TAUX_PRELEVEMENTS > 0 else "frais compris"
    if r["mode_backtest"] == "épisodes":
        txt_ep = f"{r['episodes']} épisodes indépendants"
    else:
        txt_ep = f"{r['episodes']} séances, peu d'épisodes : statistique indicative"
    if r["spread_pct"] is not None:
        txt_spread = f"{r['spread_pct']:.2f} %"
    else:
        txt_spread = "non disponible (à vérifier dans le carnet d'ordres)"
    txt_isin = r["isin"] if r["isin"] != "Inconnu" else "(ISIN non trouvé : à vérifier avant l'ordre)"
    txt_pea = "éligible (d'après l'ISIN)" if r.get("source_pea") == "isin" else "éligible d'après le pays de l'entreprise : à confirmer chez Fortuneo"
    txt_signal = "dernière séance terminée" + (" + confirmé sur la séance en cours" if r.get("signal_live") else "")
    corps = (
        f"{r['nom']} {txt_isin}\n"
        f"Achat {r['quantite']} a {r['prix']:.2f} Euros et vente a {r['prix_cible']:.2f} Euros "
        f"dans environ {formater_duree(r['duree_hist'])}\n\n"
        f"STRATÉGIE : {r['strategie']} (signal : {txt_signal})\n"
        f"PRIX D'ENTRÉE : {r['prix']:.2f} €\n"
        f"PRIX D'ACHAT MAXIMUM : {r['prix_achat_max']:.2f} € (au-delà, ne pas acheter ; gain net à ce prix : {r['gain_net_max_eur']:+.2f} €)\n"
        f"PRIX DE SORTIE : {r['prix_cible']:.2f} € (+{r['hausse_pct']:.1f} %) -> gain net estimé "
        f"{r['gain_net_eur']:+.2f} € ({r['gain_net_pct']:+.1f} %), {txt_cout}\n"
        f"DURÉE ESTIMÉE ENTRE LES DEUX : environ {r['duree_hist']} séances de bourse "
        f"({formater_duree(r['duree_hist'])}), maximum {HORIZON_MAX_SEANCES} séances\n"
        f"FIABILITÉ : cible atteinte dans {r['taux_hist']:.0f} % des cas passés similaires "
        f"({txt_ep}, fourchette basse ≈ {r['borne_basse']:.0f} %)\n"
        f"ORDRES : achat à cours limité, max {r['prix_achat_max']:.2f} € ; vente à cours limité à {r['prix_cible']:.2f} €\n"
        f"ÉCART ACHETEUR/VENDEUR : {txt_spread}\n"
        f"PEA : {txt_pea}\n\n"
        f"ACTUALITÉS :\n{r['fait']}"
    )
    log.info("Pépite retenue : %s | %s | %s | news : %s", r["ticker"], r["raison_fin"],
             r["analystes"], str(r["fait"])[:80])
    try:
        envoyer_mail("ACHAT Bourse", corps)
    except Exception as e:
        anomalie(f"Envoi du mail impossible : {type(e).__name__}.")
        return False
    memoire[r["ticker"]] = maintenant().isoformat()
    memoriser_alerte(r["ticker"])
    try:
        suivi.enregistrer_alerte(r["ticker"], r["nom"], r["isin"], r["prix"], r["prix_cible"], r["duree_hist"],
                                  quantite=r["quantite"], strategie=r["strategie"],
                                  prix_achat_max=r["prix_achat_max"], horizon=HORIZON_MAX_SEANCES)
    except Exception as e:
        anomalie(f"Suivi : enregistrement impossible ({type(e).__name__}).")
    return True


def resume_entonnoir():
    return " | ".join(f"{k}={v}" for k, v in STATS.items())


def diagnostiquer_entonnoir():
    rejets = {k: v for k, v in STATS.items() if k.startswith("rejet_")}
    if not rejets:
        return "Aucun rejet enregistré."
    top_cle = max(rejets, key=rejets.get)
    top_val = rejets[top_cle]
    total_cands = STATS.get("candidats", 0)
    tech_ok = STATS.get("passent_filtre_technique", 0)
    return f"Candidats: {total_cands} -> Tech OK: {tech_ok} | Principal filtre bloquant : {top_cle} ({top_val} rejets)"


def signaler_au_superviseur(sujet, lignes):
    auj = maintenant().date()
    _anomalies_envoyees.difference_update({k for k in _anomalies_envoyees if k[0] != auj})
    nouvelles = [m for m in dict.fromkeys(lignes) if (auj, m) not in _anomalies_envoyees]
    if not nouvelles:
        return
    corps = ["Anomalies détectées :", ""] + [f"- {m}" for m in nouvelles]
    if STATS:
        corps += ["", "Entonnoir : " + resume_entonnoir(), "Diagnostic : " + diagnostiquer_entonnoir()]
    try:
        envoyer_mail(sujet, "\n".join(corps))
        _anomalies_envoyees.update((auj, m) for m in nouvelles)
    except Exception as e:
        log.error("Mail du superviseur impossible : %s", type(e).__name__)


def agent_superviseur():
    if not MARKETAUX_API_KEY:
        anomalie("Clé Marketaux manquante : les actualités ne sont pas consultées.")
    cands = STATS.get("candidats", 0)
    if cands and STATS.get("historiques_recus", 0) < 0.5 * cands:
        anomalie("Historiques Yahoo incomplets (moins de la moitié reçus).")
    if ANOMALIES:
        signaler_au_superviseur("SUPERVISEUR Bourse — anomalies", ANOMALIES)


def candidats_du_moment():
    if source_candidats() != "yahoo":
        return agent_actualites()[0]
    heure, donnees = _cache_candidats["heure"], _cache_candidats["donnees"]
    if donnees and heure and maintenant() - heure < timedelta(minutes=CACHE_CANDIDATS_MIN):
        log.info("Agent 1 : liste de candidats en cache (%d titres).", len(donnees))
        return donnees
    donnees, secours = agent_actualites()
    if not secours:
        _cache_candidats["heure"], _cache_candidats["donnees"] = maintenant(), donnees
    return donnees


def un_passage():
    STATS.clear()
    ANOMALIES.clear()

    ok, raison = marche_ok()
    log.info("Marché : %s", raison)
    if not ok:
        log.info("Passage annulé (filtre de marché).")
        return

    candidats = candidats_du_moment()
    compter("candidats", len(candidats))
    retenus = agent_filtre(candidats)
    memoire = lire_memoire()

    nb_mails = 0
    for titre in retenus:
        try:
            r = agent_analyse_fondamentale(titre, memoire)
            if r and envoyer_pepite(r, memoire):
                nb_mails += 1
        except Exception as e:
            anomalie(f"Erreur d'analyse sur {titre['ticker']} : {type(e).__name__}")

    log.info("Entonnoir : %s", resume_entonnoir())
    log.info("Bilan Entonnoir : %s", diagnostiquer_entonnoir())
    log.info("Passage terminé — %d mail(s) envoyé(s)", nb_mails)
    try:
        suivi.mettre_a_jour()
    except Exception as e:
        anomalie(f"Suivi : mise à jour impossible ({type(e).__name__}).")
    agent_superviseur()


def passage_securise():
    try:
        un_passage()
        return True
    except Exception as e:
        log.exception("Passage en échec")
        signaler_au_superviseur("SUPERVISEUR Bourse — erreur critique",
                                [f"Erreur critique : {type(e).__name__} {str(e)[:200]}"])
        return False


def garde_horaire_ok(forcer=False):
    """Sur GitHub Actions, seul un déclenchement automatique (« schedule ») est limité aux créneaux
    CRENEAUX_HEURES (heure de Paris, jours ouvrés). Un lancement manuel ou local n'est jamais bloqué."""
    if forcer or os.getenv("GITHUB_EVENT_NAME", "") != "schedule":
        return True
    n = maintenant()
    if n.weekday() >= 5 or n.hour not in CRENEAUX_HEURES:
        log.info("Hors créneau (%02dh%02d à Paris) : passage ignoré.", n.hour, n.minute)
        return False
    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--test", action="store_true", help="un passage (peut envoyer un vrai mail)")
    p.add_argument("--une-fois", action="store_true", help="un seul passage puis arrêt")
    p.add_argument("--forcer", action="store_true", help="ignore la garde horaire")
    p.add_argument("--diagnostic", action="store_true", help="vérifie les identifiants")
    p.add_argument("--test-mail", action="store_true", help="envoie un mail de test")
    a = p.parse_args()

    if a.diagnostic:
        log.info("DIAGNOSTIC -> Gmail User: %s", "OK" if GMAIL_USER else "MANQUANT")
        log.info("DIAGNOSTIC -> Gmail Pwd: %s", "OK" if GMAIL_APP_PASSWORD else "MANQUANT")
        log.info("DIAGNOSTIC -> Destinataire: %s", "OK" if DEST_EMAIL else "MANQUANT")
        log.info("DIAGNOSTIC -> Marketaux Key: %s", "OK" if MARKETAUX_API_KEY else "MANQUANT")
        log.info("DIAGNOSTIC -> Source des candidats: %s", source_candidats())
        if source_candidats() != "yahoo":
            log.info("DIAGNOSTIC -> %s: %s", FICHIER_CRIBLE.name,
                     f"{len(lire_crible())} symboles utilisables" if FICHIER_CRIBLE.exists() else "INTROUVABLE")
        log.info("DIAGNOSTIC -> Mémoire des alertes: %s",
                 "GitHub" if _github_actif() else f"fichier local ({FICHIER_MEMOIRE_LOCAL.name})")
        return

    if a.test_mail:
        envoyer_mail("TEST Bourse", "Si tu lis ce message, l'envoi de mail fonctionne.")
        return

    if a.test or a.une_fois:
        if not garde_horaire_ok(a.forcer):
            sys.exit(0)
        sys.exit(0 if passage_securise() else 1)

    while True:
        debut = time.time()
        n = maintenant()
        if n.weekday() < 5 and FENETRE_DEBUT <= (n.hour, n.minute) <= FENETRE_FIN:
            passage_securise()
        else:
            log.info("Hors horaires de bourse : en attente.")
        time.sleep(max(0, FREQUENCE_MINUTES * 60 - (time.time() - debut)))


if __name__ == "__main__":
    main()
