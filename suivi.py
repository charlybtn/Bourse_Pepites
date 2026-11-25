"""
Suivi des alertes pour l'application mobile : tient à jour suivi.json
(dans le dépôt GitHub si GITHUB_TOKEN/GITHUB_REPOSITORY existent, sinon en fichier local).

Deux fonctions à appeler depuis le script principal :
  enregistrer_alerte(...)  -> juste après l'envoi d'un mail
  mettre_a_jour()          -> à la fin de chaque passage (limité à 1 écriture par heure)
"""
import base64, json, os, time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

PARIS = ZoneInfo("Europe/Paris")
BUDGET = float(os.getenv("BUDGET_PAR_TRADE", "250"))   # euros investis par action alertée
HORIZON = int(os.getenv("HORIZON_SEANCES", "30"))       # valeur par défaut ; le script principal envoie l'horizon de chaque alerte
MAJ_MINUTES = 60                                        # au plus une mise à jour des cours par heure
FICHIER = Path(__file__).resolve().parent / "suivi.json"


def _requete(methode, url, **kw):
    """Appel GitHub avec 3 essais (erreurs réseau ou serveur)."""
    derniere = None
    for essai in range(3):
        try:
            r = requests.request(methode, url, timeout=20, **kw)
            if r.status_code < 500:
                return r
            derniere = RuntimeError(f"code {r.status_code}")
        except requests.RequestException as e:
            derniere = e
        time.sleep(2 * (essai + 1))
    raise derniere


def _maintenant():
    return datetime.now(PARIS)


def _github():
    tok, repo = os.getenv("GITHUB_TOKEN"), os.getenv("GITHUB_REPOSITORY")
    if not (tok and repo):
        return None, None
    return f"https://api.github.com/repos/{repo}/contents/suivi.json", {
        "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}


def _lire():
    url, headers = _github()
    if url:
        r = _requete("GET", url, headers=headers)
        if r.status_code == 404:
            return {"alertes": []}, None
        r.raise_for_status()
        j = r.json()
        return json.loads(base64.b64decode(j["content"]).decode("utf-8")), j.get("sha")
    if FICHIER.exists():
        return json.loads(FICHIER.read_text(encoding="utf-8")), None
    return {"alertes": []}, None


def _ecrire(data, sha, message):
    texte = json.dumps(data, indent=1, ensure_ascii=False)
    url, headers = _github()
    if not url:
        FICHIER.write_text(texte, encoding="utf-8")
        return
    payload = {"message": message, "content": base64.b64encode(texte.encode("utf-8")).decode("utf-8")}
    if sha:
        payload["sha"] = sha
    _requete("PUT", url, headers=headers, json=payload).raise_for_status()


def _historique(h):
    """Cours de clôture sur 6 mois, pour le graphique de l'appli."""
    h = h.dropna(subset=["Close"])
    return [[d.isoformat(), round(float(c), 3)] for d, c in zip(h.index.date, h["Close"])]


def _historique_ticker(ticker):
    try:
        return _historique(yf.Ticker(ticker).history(period="6mo", interval="1d", auto_adjust=True))
    except Exception:
        return []


def enregistrer_alerte(ticker, nom, isin, prix, prix_cible, duree_seances, quantite=None, strategie=None, prix_achat_max=None, horizon=None):
    data, sha = _lire()
    if any(a["ticker"] == ticker and a["statut"] == "en_cours" for a in data["alertes"]):
        return
    data["alertes"].append({
        "ticker": ticker, "nom": nom, "isin": isin,
        "date_alerte": _maintenant().isoformat(), "prix_alerte": prix, "prix_cible": prix_cible,
        "quantite": quantite or max(1, int(BUDGET // prix)), "horizon": horizon or HORIZON, "strategie": strategie, "prix_achat_max": prix_achat_max, "duree_estimee_seances": duree_seances,
        "prix_actuel": prix, "seances": 0, "statut": "en_cours", "historique": _historique_ticker(ticker),
    })
    _ecrire(data, sha, f"Suivi: alerte {ticker}")


def mettre_a_jour(force=False):
    data, sha = _lire()
    ouvertes = [a for a in data["alertes"] if a["statut"] == "en_cours"]
    if not ouvertes:
        return
    derniere = data.get("maj")
    if not force and derniere and _maintenant() - datetime.fromisoformat(derniere) < timedelta(minutes=MAJ_MINUTES):
        return
    for a in ouvertes:
        try:
            h = yf.Ticker(a["ticker"]).history(period="6mo", interval="1d", auto_adjust=True)
            h = h.dropna(subset=["Close"])
            a["historique"] = _historique(h)
            debut = datetime.fromisoformat(a["date_alerte"]).date()
            h = h[[d >= debut for d in h.index.date]]
            if h.empty:
                continue
            apres = h.iloc[1:]                          # séances après le jour de l'alerte
            a["prix_actuel"] = round(float(h["Close"].iloc[-1]), 3)
            a["seances"] = len(apres)
            touche = apres[apres["High"] >= a["prix_cible"]]
            if not touche.empty:
                a.update(statut="cible_atteinte", prix_cloture=a["prix_cible"],
                         date_cloture=touche.index[0].date().isoformat())
            elif len(apres) >= a.get("horizon", HORIZON):
                a.update(statut="delai_depasse", prix_cloture=a["prix_actuel"],
                         date_cloture=h.index[-1].date().isoformat())
        except Exception:
            continue
    data["maj"] = _maintenant().isoformat()
    _ecrire(data, sha, "Suivi: mise à jour des cours")
