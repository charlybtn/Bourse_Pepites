#!/usr/bin/env python3
"""
CODE DE TEST - Vérification de la chaîne Gemini + Mail.
Cible : LVMH (MC.PA)
"""
import argparse, json, logging, math, os, re, smtplib, sys, time, warnings
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

warnings.filterwarnings("ignore")

DOSSIER = Path(__file__).resolve().parent
LOG = DOSSIER / "bourse.log"

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

# ----------------------------------------------------------------- OUTILS
def envoyer_mail(sujet, corps):
    if not GMAIL_APP_PASSWORD: raise RuntimeError("Mot de passe Gmail manquant")
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = sujet, GMAIL_USER, DEST_EMAIL
    m.set_content(corps)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.send_message(m)
    log.info("Mail envoyé : %s", sujet)

def msg(role, texte): return {"role": role, "parts": [{"text": texte}]}

def gemini_analyse(ticker, nom, prix, rsi):
    if not GEMINI_API_KEY: 
        log.error("Pas de clé Gemini")
        return None
    
    modeles = ["gemini-1.5-flash", "gemini-2.0-flash"]
        
    prompt = (
        f"Tu es un analyste financier expert. Analyse l'entreprise {nom} (ticker {ticker}).\n"
        f"Le cours est actuellement de {prix} et le RSI est de {rsi}.\n"
        "Donne une note fondamentale sur 100.\n"
        "Réponds STRICTEMENT en JSON : {\"score\": int, \"raison\": \"ton analyse en 2 phrases\"}"
    )
    
    for modele in modeles:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{modele}:generateContent"
        try:
            log.info(f"Appel de Gemini ({modele})...")
            r = requests.post(url, headers={"x-goog-api-key": GEMINI_API_KEY}, json={"contents": [msg("user", prompt)], "generationConfig": {"responseMimeType": "application/json"}}, timeout=45)
            r.raise_for_status()
            texte = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            log.info(f"Réponse Gemini reçue !")
            return json.loads(re.sub(r"^```(?:json)?|```$", "", texte.strip(), flags=re.M).strip())
        except Exception as e:
            log.warning(f"Gemini {modele} fail: {e}")
            continue
    log.error("Tous les modèles Gemini ont échoué")
    return None

# ----------------------------------------------------------------- EXECUTION TEST
def run_test():
    ticker = "MC.PA"
    nom = "LVMH"
    
    log.info("--- DÉBUT DU TEST ---")
    
    try:
        fi = yf.Ticker(ticker).fast_info
        prix = round(float(fi.last_price), 3)
        log.info(f"Prix de {nom} récupéré : {prix} €")
    except:
        prix = 400.0 # Valeur au cas où
        log.warning("Impossible de récupérer le prix, on utilise 400.0")

    log.info("Appel de Gemini pour analyse fondamentale...")
    g = gemini_analyse(ticker, nom, prix, 50)
    
    if g:
        score = int(g.get("score", 0))
        raison = g.get("raison", "")
        log.info(f"Score Gemini : {score}/100 - {raison}")
        
        lignes = [
            "Bonjour Charly,\n",
            "Ceci est un EMAIL DE TEST pour valider la chaîne d'envoi.\n",
            f"=== {nom} ({ticker}) ===",
            f"Prix simulé : {prix} €",
            f"Score IA   : {score}/100",
            f"",
            f"💡 L'avis de Gemini :",
            f"{raison}",
        ]
        try:
            envoyer_mail("TEST Bourse — Chaîne validée", "\n".join(lignes))
        except Exception as e:
            log.error(f"Échec de l'envoi du mail : {e}")
    else:
        log.error("Gemini n'a pas répondu, impossible d'envoyer le mail.")

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

    # On lance toujours le test, peu importe les arguments
    run_test()

if __name__ == "__main__":
    main()
