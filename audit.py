"""Audit rapide et gratuit du site d'un prospect : ce qu'un client voit sur son téléphone."""

import re
import time
from datetime import date
from urllib.parse import urljoin, urlparse

import requests

ENTETES = {"User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/126.0 Mobile Safari/537.36"}

OUTILS_RDV = ["doctolib", "planity", "calendly", "zenchef", "thefork", "lafourchette", "resengo", "booksy",
              "treatwell", "kiute", "salonkee", "rdv360", "clicrdv", "agendize", "bookingkit", "simplybook",
              "setmore", "mindbody", "resamania", "maiia", "keldoc", "guestonline", "sevenrooms", "resy", "tablebooker"]
CONSTRUCTEURS = {"wix.com": "Wix", "wixsite": "Wix", "jimdo": "Jimdo", "pagesjaunes": "PagesJaunes/Solocal",
                 "solocal": "PagesJaunes/Solocal", "site123": "Site123", "e-monsite": "e-monsite",
                 "webself": "Webself", "123siteweb": "123siteweb", "godaddy": "GoDaddy"}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EMAILS_IGNORES = ("example", "exemple", "sentry", "wixpress", "domain.", "votre", "email@", "nom@", ".png", ".jpg", ".webp")
ANNEE = re.compile(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?((?:19|20)\d{2})", re.I)


def normaliser_url(site: str) -> str:
    site = (site or "").strip()
    if site and not site.startswith(("http://", "https://")):
        site = "http://" + site
    return site


def _telecharger(url: str) -> tuple[requests.Response | None, float, str]:
    debut = time.monotonic()
    try:
        r = requests.get(url, headers=ENTETES, timeout=15, allow_redirects=True)
        return r, time.monotonic() - debut, ""
    except requests.exceptions.SSLError:
        return None, 0, "certificat de sécurité invalide"
    except requests.RequestException:
        return None, 0, "site injoignable"


def auditer(site: str, rdv_utile: bool = False) -> dict:
    """Renvoie les problèmes détectés, sous forme de paires (code, texte), et les e-mails trouvés sur le site."""
    url = normaliser_url(site)
    resultat = {"site_ok": False, "problemes": [], "emails": [], "constructeur": ""}
    if not url:
        return resultat
    r, duree, erreur = _telecharger(url)
    if r is None or r.status_code >= 400:
        resultat["problemes"].append(("injoignable", erreur or f"le site renvoie une erreur ({r.status_code})"))
        return resultat

    html = r.text
    bas = html.lower()
    resultat["site_ok"] = True
    p = resultat["problemes"]

    if 'name="viewport"' not in bas and "name='viewport'" not in bas and "name=viewport" not in bas:
        p.append(("mobile", "le site n'est pas adapté aux téléphones"))
    if not r.url.startswith("https://"):
        p.append(("https", "le site n'est pas sécurisé (pas de HTTPS, les navigateurs affichent « non sécurisé »)"))
    annees = [int(a) for a in ANNEE.findall(html)]
    if annees and max(annees) <= date.today().year - 4:
        p.append(("ancien", f"le site semble ne plus être mis à jour depuis {max(annees)}"))
    if rdv_utile and not any(o in bas for o in OUTILS_RDV):
        p.append(("rdv", "pas de prise de rendez-vous ou de réservation en ligne"))
    if "tel:" not in bas:
        p.append(("tel", "le numéro de téléphone n'est pas cliquable sur mobile"))
    if duree > 3:
        p.append(("lent", f"la page d'accueil met {duree:.0f} secondes à se charger"))
    if len(r.content) > 4_000_000:
        p.append(("lourd", f"la page d'accueil est très lourde ({len(r.content) / 1_000_000:.0f} Mo)"))
    if "mentions" not in bas:
        p.append(("mentions", "pas de page de mentions légales (obligatoire)"))
    for indice, nom in CONSTRUCTEURS.items():
        if indice in bas:
            resultat["constructeur"] = nom
            break

    resultat["emails"] = _emails(html)
    if not resultat["emails"]:  # souvent sur la page contact ou les mentions légales
        for lien in _liens_contact(html, r.url)[:2]:
            r2, _, _ = _telecharger(lien)
            if r2 is not None and r2.ok:
                resultat["emails"] = _emails(r2.text)
                if resultat["emails"]:
                    break
    return resultat


def _emails(html: str) -> list[str]:
    trouves = []
    for e in EMAIL.findall(html.replace("[at]", "@").replace("(at)", "@")):
        e = e.lower().strip(".")
        if not any(i in e for i in EMAILS_IGNORES) and e not in trouves:
            trouves.append(e)
    return trouves


def _liens_contact(html: str, base: str) -> list[str]:
    liens = []
    domaine = urlparse(base).netloc
    for href in re.findall(r'href=["\']([^"\'#]+)["\']', html, re.I):
        if re.search(r"contact|mentions|legal", href, re.I):
            complet = urljoin(base, href)
            if urlparse(complet).netloc == domaine and complet not in liens:
                liens.append(complet)
    return liens
