#!/usr/bin/env python3
"""
Agents IA pour la bourse - Chasseur de Pépites (Europe, EUR) - VERSION CORRIGÉE.

Chaîne des agents :
  1. Liste de candidats : fichier crible_output.csv (filtrage fondamental Crible), ou recherche Yahoo
  2. Filtre technique (5 voyants verts, téléchargement groupé de l'historique)
  3. Analyse : mémoire, backtest conditionnel, calendrier, fondamentaux, PEA,
     avis d'analystes, actualités (Marketaux)
  4. Mail envoyé dès qu'un titre passe tous les critères (la mémoire évite les doublons)
  5. Superviseur : mail uniquement en cas d'anomalie

Lancement :
  python agents_bourse.py --diagnostic   -> vérifie les identifiants
  python agents_bourse.py --test-mail    -> envoie un mail de test
  python agents_bourse.py --une-fois     -> un seul passage (aussi pour GitHub Actions)
  python agents_bourse.py                -> tourne en continu : un passage toutes les 15 minutes
                                            (jours ouvrés, de 9h00 à 17h30, heure de Paris)
"""
import argparse, base64, csv, json, logging, math, os, re, smtplib, sys, time, warnings
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

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
# Plus d'adresse écrite en dur : mets DEST_EMAIL dans le .env (sinon le mail part vers GMAIL_USER)
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
FORMULE = (os.getenv("FORMULE_FORTUNEO") or "progress").lower()
MIN_ACTIONS = 10
OBJECTIF_NET = 0.10                 # +10 % net visé
# Prélèvements sociaux sur la plus-value (PEA de plus de 5 ans). À vérifier selon ton enveloppe.
TAUX_PRELEVEMENTS = float(os.getenv("TAUX_PRELEVEMENTS", "0.172"))
HORIZON_MAX_SEANCES = 30
TAUX_REUSSITE_MIN = 0.60
MIN_SIGNAUX = 5                     # minimum de jours "5/5 voyants" pour un backtest conditionnel
VOLATILITE_MIN = 1.5
SEUIL_ANALYSTES = 0.65              # 65 % d'avis à l'achat
MIN_ANALYSTES = 3
EVENEMENT_JOURS = 7                 # on évite résultats / dividende dans les 7 prochains jours

# --- Recherche
MAX_DOSSIERS = 3000
PAGES_PAR_PLACE = 10
TAILLE_LOT_TELECHARGEMENT = 50
DELAI_MEMOIRE_JOURS = 30
FREQUENCE_MINUTES = 15              # un passage toutes les 15 minutes
FENETRE_DEBUT = (9, 0)              # jours ouvrés, heure de Paris
FENETRE_FIN = (17, 30)
CACHE_CANDIDATS_MIN = 60            # la liste des candidats (screener) est rafraîchie toutes les 60 min
CACHE_ACTUS_MIN = 60                # une réponse Marketaux est réutilisée pendant 60 min (quota gratuit)
SOURCE_CANDIDATS = (os.getenv("SOURCE_CANDIDATS") or "crible").lower()   # "crible" (fichier CSV) ou "yahoo" (recherche large)
FICHIER_CRIBLE = Path(os.getenv("FICHIER_CRIBLE") or (DOSSIER / "crible_output.csv"))
FONDAMENTAL_LOCAL = env_bool("FONDAMENTAL_LOCAL", True)   # 0 = on fait confiance au filtrage fondamental de Crible

# --- Options (modifiables dans le .env : 0 ou 1)
ACTUS_OBLIGATOIRES = env_bool("ACTUS_OBLIGATOIRES", True)
ANALYSTES_STRICT = env_bool("ANALYSTES_STRICT", False)   # 1 = refuse les titres sans analyste
FILTRE_MARCHE = env_bool("FILTRE_MARCHE", True)          # 1 = pas d'achat si le marché est sous sa MM200

MARCHE_INDICE = "^STOXX50E"
CHUTE_MARCHE_MAX_PCT = -3.0         # disjoncteur : indice qui chute de plus de 3 % en un jour
MARKETAUX_MAX_REQUETES = 90         # offre gratuite : 100 requêtes par jour

PAYS_PEA = {
    "FR", "DE", "NL", "IT", "ES", "BE", "PT", "IE", "LU", "FI", "AT", "GR",
    "SE", "DK", "PL", "CZ", "HU", "RO", "BG", "HR", "SK", "SI", "EE", "LV", "LT",
    "MT", "CY", "NO", "IS", "LI",
}
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

# Suffixes Yahoo des places en EUR (".F" retiré : doublon de ".DE")
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

# ----------------------------------------------------------------- SUIVI (entonnoir + anomalies)
STATS = {}
ANOMALIES = []
_anomalies_envoyees = set()         # (jour, message) déjà signalés par mail : pas de spam toutes les 15 min
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
# Une action déjà envoyée par mail n'est plus proposée pendant DELAI_MEMOIRE_JOURS jours.
# Mémoire sur GitHub si configuré, sinon fichier local (indispensable : le code tourne toutes les 15 min).
FICHIER_MEMOIRE_LOCAL = DOSSIER / "memoire_alertes.json"
_MEMOIRE_SESSION = {}   # garde-fou : pas de doublon pendant que le script tourne, même si GitHub est en panne


def _github_actif():
    return bool(GITHUB_TOKEN and GITHUB_REPOSITORY)


def _url_memoire():
    return f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/memoire_alertes.json"


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
    """Renvoie (dict, sha). Renvoie (None, None) si la lecture a échoué (pour ne rien écraser)."""
    try:
        r = requests.get(_url_memoire(), headers=GITHUB_HEADERS, timeout=20)
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
        return  # lecture échouée : on n'écrase pas l'historique
    for t in tickers:
        memoire[t] = maintenant().isoformat()
    memoire = _purger(memoire)
    contenu = base64.b64encode(json.dumps(memoire, indent=1).encode("utf-8")).decode("utf-8")
    payload = {"message": f"Mémoire: {', '.join(tickers)}", "content": contenu}
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(_url_memoire(), headers=GITHUB_HEADERS, json=payload, timeout=20)
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
    """Lue UNE SEULE fois par passage."""
    if _github_actif():
        memoire, _ = _lire_memoire_et_sha()
    else:
        memoire = _lire_memoire_locale()
    memoire = _purger(memoire or {})
    memoire.update(_purger(_MEMOIRE_SESSION))
    return memoire


def memoriser_alerte(ticker):
    """À appeler juste après l'envoi du mail."""
    _MEMOIRE_SESSION[ticker] = maintenant().isoformat()
    if _github_actif():
        mettre_a_jour_memoire_github([ticker])
    else:
        memoire = _lire_memoire_locale()
        memoire[ticker] = maintenant().isoformat()
        _ecrire_memoire_locale(_purger(memoire))


# ----------------------------------------------------------------- FRAIS & PRIX CIBLE
def frais(montant):
    if FORMULE == "starter":
        return montant * 0.0035
    if FORMULE == "trader":
        return 9.50 if montant <= 10000 else montant * 0.0010
    return 4.90 if montant <= 3000 else montant * 0.0015


def prix_minimum_pour_objectif_net(prix_achat):
    """Prix de vente minimal pour gagner OBJECTIF_NET net (frais d'achat + de vente + prélèvements)."""
    n = MIN_ACTIONS
    valeur_achat = n * prix_achat
    cout_total = valeur_achat + frais(valeur_achat)
    gain_brut = cout_total * OBJECTIF_NET / (1 - TAUX_PRELEVEMENTS)
    a_encaisser = cout_total + gain_brut          # montant voulu après frais de vente
    p = a_encaisser / n
    for _ in range(6):
        p = (a_encaisser + frais(n * p)) / n
    return math.ceil(p * 100) / 100


# ----------------------------------------------------------------- TECHNIQUE
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
        "rsi": rsi.between(30, 60).fillna(False),
        "macd": ((hist > hist.shift(1)) & (hist.shift(1) > hist.shift(2))).fillna(False),
        "volume": (vol.rolling(20).mean() >= 100000).fillna(False),
    }
    return f, rsi, var5, repli


def voyants(h):
    """Analyse technique d'un historique déjà téléchargé (aucun appel réseau)."""
    if h is None or len(h) < 210:
        return None
    f, rsi, var5, repli = flags_serie(h)
    flags = {k: bool(s.iloc[-1]) for k, s in f.items()}
    signal = pd.concat(list(f.values()), axis=1).all(axis=1)   # jours où les 5 voyants étaient verts

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


def historique_cible(h, hausse_pct, signal=None):
    """
    Part des cas où le cours touche +hausse_pct% dans les HORIZON_MAX_SEANCES séances.
    Backtest CONDITIONNEL : uniquement les jours où les 5 voyants étaient verts
    (s'il y en a assez), sinon repli sur tous les jours.
    Renvoie (taux, durée médiane en séances, nb de cas, mode).
    """
    hi, cl = h["High"].values, h["Close"].values
    n = len(cl) - HORIZON_MAX_SEANCES
    if n <= 0:
        return 0.0, 0, 0, "aucun"

    def evaluer(indices):
        ok, durees = 0, []
        for i in indices:
            cible = cl[i] * (1 + hausse_pct / 100)
            for j in range(i + 1, i + 1 + HORIZON_MAX_SEANCES):
                if hi[j] >= cible:
                    ok += 1
                    durees.append(j - i)
                    break
        if not len(indices):
            return 0.0, 0
        mediane = sorted(durees)[len(durees) // 2] if durees else 0
        return ok / len(indices), mediane

    if signal is not None:
        idx = [i for i in range(n) if bool(signal.iloc[i])]
        if len(idx) >= MIN_SIGNAUX:
            taux, duree = evaluer(idx)
            return taux, duree, len(idx), "conditionnel"
    taux, duree = evaluer(list(range(n)))
    return taux, duree, n, "global"


def telecharger_historiques(tickers):
    """Téléchargement groupé (beaucoup plus rapide et moins bloqué que ticker par ticker)."""
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
    """Filtre de marché + disjoncteur. Non bloquant si l'indice est indisponible."""
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
    """Résultats ou détachement de dividende dans les EVENEMENT_JOURS prochains jours."""
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
    """(part d'avis à l'achat, nombre d'analystes) ou (None, 0) si pas de couverture."""
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
    """Valeur d'une ligne de bilan, ou None si absente / vide."""
    try:
        v = df.loc[nom].iloc[i]
        return None if v != v else float(v)
    except Exception:
        return None


def ratio(a, b):
    return None if a is None or b in (None, 0) else a / b


def gt(a, b):
    return None if a is None or b is None else a > b


def lt(a, b):
    return None if a is None or b is None else a < b


def le(a, b):
    return None if a is None or b is None else a <= b


def calculer_score_local(t, info):
    """
    Piotroski (sur les critères calculables, ramené à /9) + Altman Z'' (hors secteur financier).
    Renvoie (None, message) si les données sont insuffisantes (compté à part dans l'entonnoir).
    """
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

    # Altman Z'' (marchés émergents / non cotées) : seuils 2,6 (sain) et 1,1 (détresse). Non applicable aux banques.
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


def infos_entreprise(t, info, ticker):
    """(nom, ISIN valide ou 'Inconnu', éligibilité PEA). Éligibilité décidée uniquement sur l'ISIN."""
    nom = info.get("longName") or info.get("shortName") or ticker
    try:
        isin = t.isin
    except Exception:
        isin = None
    if isin and ISIN_RE.match(str(isin)):
        return nom, isin, ("Oui" if isin[:2] in PAYS_PEA else "Non")
    return nom, "Inconnu", "Inconnu"


# ----------------------------------------------------------------- ACTUALITÉS (Marketaux)
_marketaux_jour = {"date": None, "n": 0}   # quota de 100 requêtes PAR JOUR (et non par passage)
_cache_actus = {}                           # ticker -> (heure, fait, message)


def _quota_marketaux_restant():
    auj = maintenant().date()
    if _marketaux_jour["date"] != auj:
        _marketaux_jour["date"], _marketaux_jour["n"] = auj, 0
    return MARKETAUX_MAX_REQUETES - _marketaux_jour["n"]


def _interroger_marketaux(ticker):
    """Renvoie (fait, message, definitif). definitif=False en cas d'erreur : pas de mise en cache."""
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
        # On n'affiche jamais l'exception brute : elle contiendrait l'URL avec la clé API.
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

    cibles = {ticker.upper(), ticker.split(".")[0].upper()}
    notes = []                                 # (sentiment, titre) pour CETTE entreprise uniquement
    for art in articles:
        for ent in art.get("entities", []):
            if str(ent.get("symbol", "")).upper() in cibles and ent.get("sentiment_score") is not None:
                notes.append((float(ent["sentiment_score"]), art.get("title")))
    if not notes:
        return None, "Pas de score de sentiment pour cette action.", True

    moyen = sum(s for s, _ in notes) / len(notes)
    if moyen > 0.1:
        meilleur = max(notes, key=lambda x: x[0])[1]
        return (meilleur or "Sentiment positif détecté"), f"Sentiment moyen : {moyen:.2f}", True
    return None, f"Sentiment trop faible ou négatif ({moyen:.2f})", True


def analyser_actualites_marketaux(ticker):
    """Avec un passage toutes les 15 min, on réutilise la réponse pendant CACHE_ACTUS_MIN minutes."""
    maint = maintenant()
    en_cache = _cache_actus.get(ticker)
    if en_cache and maint - en_cache[0] < timedelta(minutes=CACHE_ACTUS_MIN):
        return en_cache[1], en_cache[2]
    fait, msg, definitif = _interroger_marketaux(ticker)
    if definitif:
        _cache_actus[ticker] = (maint, fait, msg)
    return fait, msg


# ----------------------------------------------------------------- AGENTS
def agent_screener_yahoo():
    """Option SOURCE_CANDIDATS=yahoo : recherche large sur les places européennes, actions en EUR."""
    candidats = {}
    for exchange, pays in PLACES:
        q = EquityQuery("and", [
            EquityQuery("eq", ["exchange", exchange]),
            EquityQuery("lt", ["intradayprice", PRIX_MAX]),
            EquityQuery("gt", ["intradayprice", PRIX_MIN]),
            EquityQuery("gt", ["avgdailyvol3m", 50000]),
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

    # Tri par liquidité décroissante AVANT de couper à MAX_DOSSIERS (et non au hasard)
    tries = sorted(candidats, key=lambda s: candidats[s]["liquidite"], reverse=True)
    garde = {s: candidats[s] for s in tries[:MAX_DOSSIERS]}
    log.info("Agent 1 : %d titres en EUR trouvés, %d conservés.", len(candidats), len(garde))
    return garde, False


def lire_crible():
    """Lit la liste générée par Crible. Renvoie {} si le fichier est absent, vide ou illisible."""
    if not FICHIER_CRIBLE.exists():
        return {}
    try:
        with open(FICHIER_CRIBLE, newline="", encoding="utf-8-sig") as f:      # utf-8-sig : accepte le BOM d'Excel
            echantillon = f.read(4096)
            f.seek(0)
            try:
                dialecte = csv.Sniffer().sniff(echantillon, delimiters=",;\t")   # virgule, point-virgule ou tabulation
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
    """Agent 1 : liste des candidats. Renvoie (dict, est_liste_de_secours)."""
    if SOURCE_CANDIDATS == "yahoo":
        return agent_screener_yahoo()
    candidats = lire_crible()
    if candidats:
        log.info("Agent 1 : %d candidats lus dans %s.", len(candidats), FICHIER_CRIBLE.name)
        return candidats, False
    anomalie(f"{FICHIER_CRIBLE.name} introuvable, vide ou illisible : liste de secours utilisée.")
    return {t: {"prix": None, "liquidite": 0} for t in TICKERS_SECOURS}, True


def agent_filtre(candidats):
    """Agent 2 : filtre technique (5 voyants verts) sur historiques téléchargés par lots."""
    historiques = telecharger_historiques(list(candidats))
    compter("historiques_recus", len(historiques))
    retenus = []
    for ticker, h in historiques.items():
        ana = voyants(h)
        if ana is None:
            compter("rejet_historique_court")
            continue
        if not ana["tous_verts"]:
            compter("rejet_voyants")
            continue
        prix = round(float(h["Close"].iloc[-1]), 3)   # dernier cours (en séance : cours du moment)
        if not (PRIX_MIN <= prix <= PRIX_MAX):
            compter("rejet_prix")
            continue
        if ana["volatilite"] < VOLATILITE_MIN:
            compter("rejet_volatilite")
            continue
        prec = float(h["Close"].iloc[-2])
        ana.update({"ticker": ticker, "prix": prix, "var_pct": round((prix / prec - 1) * 100, 2)})
        retenus.append(ana)
        log.info("  -> %s validé (5/5 voyants) ! Prix: %s €", ticker, prix)
    compter("passent_filtre_technique", len(retenus))
    return retenus


def agent_analyse_fondamentale(titre, memoire):
    """Agent 3 : les contrôles gratuits d'abord, l'API limitée (Marketaux) en dernier."""
    ticker = titre["ticker"]

    if ticker in memoire:
        compter("rejet_deja_alerte")
        return None

    prix_cible = prix_minimum_pour_objectif_net(titre["prix"])
    hausse_requise_pct = (prix_cible / titre["prix"] - 1) * 100
    taux, duree, nb_cas, mode = historique_cible(titre["h"], hausse_requise_pct, titre["signal"])
    log.info("  -> %s backtest %s : %.0f %% de réussite sur %d cas (+%.1f %% requis)",
             ticker, mode, taux * 100, nb_cas, hausse_requise_pct)
    if taux < TAUX_REUSSITE_MIN:
        compter("rejet_backtest")
        return None

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

    nom, isin, pea = infos_entreprise(t, info, ticker)
    if isin == "Inconnu":
        compter("rejet_isin_inconnu")
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

    fait, msg_sentiment = analyser_actualites_marketaux(ticker)
    log.info("  -> %s Marketaux : %s", ticker, msg_sentiment)
    if not fait:
        if ACTUS_OBLIGATOIRES:
            compter("rejet_actualites")
            return None
        fait = "Aucune actualité positive (non bloquant)"

    return {
        "ticker": ticker, "nom": nom, "isin": isin, "pea": pea, "prix": titre["prix"],
        "score_fin": score, "raison_fin": raison, "taux_hist": round(taux * 100, 1),
        "duree_hist": duree, "mode_backtest": mode, "prix_cible": prix_cible,
        "fait": fait, "analystes": txt_analystes,
    }


def verifier_resultat(r):
    """Contrôle final (rôle du superviseur) : on n'envoie rien qui ne soit cohérent."""
    problemes = []
    if not r.get("prix") or r["prix"] <= 0 or r["prix"] > PRIX_MAX:
        problemes.append("prix d'achat invalide")
    if r["prix_cible"] <= (r.get("prix") or 0):
        problemes.append("prix de vente <= prix d'achat")
    if not ISIN_RE.match(r["isin"]):
        problemes.append("ISIN invalide")
    if r["pea"] != "Oui":
        problemes.append("non éligible PEA")
    if not r["duree_hist"] or r["duree_hist"] > HORIZON_MAX_SEANCES:
        problemes.append("durée estimée invalide")
    return problemes


def formater_duree(seances):
    jours = max(1, round(seances * 7 / 5))      # séances (5/semaine) -> jours calendaires
    if jours < 30:
        return f"{jours} jour{'s' if jours > 1 else ''}"
    return f"{max(1, round(jours / 30))} mois"


def envoyer_pepite(r, memoire):
    """Agent 4 : le mail part DÈS QU'UN titre a passé tous les critères (fin de son analyse).
    La mémoire empêche de renvoyer le même titre pendant DELAI_MEMOIRE_JOURS jours."""
    pb = verifier_resultat(r)
    if pb:
        anomalie(f"Résultat {r['ticker']} bloqué avant envoi : {', '.join(pb)}.")
        return False
    corps = (
        f"{r['nom']} {r['isin']}\n"
        f"Achat {MIN_ACTIONS} a {r['prix']:.2f} Euros et vente a {r['prix_cible']:.2f} Euros "
        f"dans environ {formater_duree(r['duree_hist'])}"
    )
    log.info("Pépite retenue : %s | %s | %s | news : %s", r["ticker"], r["raison_fin"],
             r["analystes"], str(r["fait"])[:80])
    try:
        envoyer_mail("ACHAT Bourse", corps)
    except Exception as e:
        # Pas de mémoire si le mail n'est pas parti : le titre sera retenté au prochain passage.
        anomalie(f"Envoi du mail impossible : {type(e).__name__}.")
        return False
    memoire[r["ticker"]] = maintenant().isoformat()
    memoriser_alerte(r["ticker"])
    try:
        suivi.enregistrer_alerte(r["ticker"], r["nom"], r["isin"], r["prix"], r["prix_cible"], r["duree_hist"])
    except Exception as e:
        anomalie(f"Suivi : enregistrement impossible ({type(e).__name__}).")
    return True


def resume_entonnoir():
    return " | ".join(f"{k}={v}" for k, v in STATS.items())


def signaler_au_superviseur(sujet, lignes):
    """Mail d'anomalie : chaque message n'est envoyé qu'une fois par jour (pas de spam toutes les 15 min)."""
    auj = maintenant().date()
    _anomalies_envoyees.difference_update({k for k in _anomalies_envoyees if k[0] != auj})
    nouvelles = [m for m in dict.fromkeys(lignes) if (auj, m) not in _anomalies_envoyees]
    if not nouvelles:
        return
    corps = ["Anomalies détectées :", ""] + [f"- {m}" for m in nouvelles]
    if STATS:
        corps += ["", "Entonnoir : " + resume_entonnoir()]
    try:
        envoyer_mail(sujet, "\n".join(corps))
        _anomalies_envoyees.update((auj, m) for m in nouvelles)
    except Exception as e:
        log.error("Mail du superviseur impossible : %s", type(e).__name__)


def agent_superviseur():
    """Agent 5 : seul à écrire pour signaler une anomalie. Silencieux si tout va bien."""
    if ACTUS_OBLIGATOIRES and not MARKETAUX_API_KEY:
        anomalie("Clé Marketaux manquante : aucune pépite ne peut être validée.")
    cands = STATS.get("candidats", 0)
    if cands and STATS.get("historiques_recus", 0) < 0.5 * cands:
        anomalie("Historiques Yahoo incomplets (moins de la moitié reçus).")
    if ANOMALIES:
        signaler_au_superviseur("SUPERVISEUR Bourse — anomalies", ANOMALIES)


def candidats_du_moment():
    """Liste des candidats : rafraîchie toutes les CACHE_CANDIDATS_MIN minutes seulement (le screener est lent)."""
    if SOURCE_CANDIDATS != "yahoo":
        return agent_actualites()[0]     # lecture de fichier : pas besoin de cache
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
            if r and envoyer_pepite(r, memoire):       # mail dès la fin de l'analyse de CE titre
                nb_mails += 1
        except Exception as e:
            anomalie(f"Erreur d'analyse sur {titre['ticker']} : {type(e).__name__}")

    log.info("Entonnoir : %s", resume_entonnoir())
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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--test", action="store_true", help="un passage (peut envoyer un vrai mail)")
    p.add_argument("--une-fois", action="store_true", help="un seul passage puis arrêt")
    p.add_argument("--diagnostic", action="store_true", help="vérifie les identifiants")
    p.add_argument("--test-mail", action="store_true", help="envoie un mail de test")
    a = p.parse_args()

    if a.diagnostic:
        log.info("DIAGNOSTIC -> Gmail User: %s", "OK" if GMAIL_USER else "MANQUANT")
        log.info("DIAGNOSTIC -> Gmail Pwd: %s", "OK" if GMAIL_APP_PASSWORD else "MANQUANT")
        log.info("DIAGNOSTIC -> Destinataire: %s", "OK" if DEST_EMAIL else "MANQUANT")
        log.info("DIAGNOSTIC -> Marketaux Key: %s", "OK" if MARKETAUX_API_KEY else "MANQUANT")
        log.info("DIAGNOSTIC -> Source des candidats: %s", SOURCE_CANDIDATS)
        if SOURCE_CANDIDATS != "yahoo":
            log.info("DIAGNOSTIC -> %s: %s", FICHIER_CRIBLE.name,
                     f"{len(lire_crible())} symboles utilisables" if FICHIER_CRIBLE.exists() else "INTROUVABLE")
        log.info("DIAGNOSTIC -> Mémoire des alertes: %s",
                 "GitHub" if _github_actif() else f"fichier local ({FICHIER_MEMOIRE_LOCAL.name})")
        return

    if a.test_mail:
        envoyer_mail("TEST Bourse", "Si tu lis ce message, l'envoi de mail fonctionne.")
        return

    if a.test or a.une_fois:
        sys.exit(0 if passage_securise() else 1)

    # Boucle : un passage toutes les FREQUENCE_MINUTES minutes (départ à départ),
    # les jours ouvrés entre FENETRE_DEBUT et FENETRE_FIN (heure de Paris).
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
