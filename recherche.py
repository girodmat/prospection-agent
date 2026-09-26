"""Recherche de prospects : trouve les entreprises d'un métier autour d'une ville, audite leur site
et produit un prospects.csv trié du plus prometteur au moins prometteur.

Exemples :
  python recherche.py plombier Versailles
  python recherche.py coiffeur "Saint-Germain-en-Laye" --rayon 3 --max 60
  python recherche.py restaurant Versailles --web 20     # + recherche web par Claude (ton abonnement)
  python recherche.py --liste                              # métiers disponibles

Tout est gratuit : annuaire officiel des entreprises, OpenStreetMap et audit des sites fait sur ton PC.
L'option --web utilise Claude Code sur ton abonnement (pas de clé API).
"""

import argparse
import csv
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from math import cos, radians, sqrt
from pathlib import Path

import yaml

import audit
import sources
from claude_cli import ErreurClaude, demander

DOSSIER = Path(__file__).parent

POIDS = {"injoignable": 40, "mobile": 30, "ancien": 20, "https": 15, "rdv": 15, "tel": 10, "lent": 10,
         "lourd": 5, "mentions": 5}
COLONNES = ["score", "entreprise", "contact", "metier", "ville", "email", "telephone", "site", "constat", "source",
            "problemes", "adresse", "code_postal", "siret", "date_creation", "effectif", "origine"]
FORMES_JURIDIQUES = {"sarl", "sas", "sasu", "eurl", "sa", "sci", "snc", "ei", "eirl", "ets", "etablissements", "et", "fils",
                     "cie", "the", "le", "la", "les", "l", "de", "du", "des", "d"}


# --- Fusion des sources -----------------------------------------------------------

def mots(nom: str) -> set[str]:
    nom = unicodedata.normalize("NFKD", nom.lower()).encode("ascii", "ignore").decode()
    return {m for m in re.split(r"[^a-z0-9]+", nom) if len(m) > 1 and m not in FORMES_JURIDIQUES}


def meme_entreprise(a: dict, b: dict) -> bool:
    ma, mb = mots(a["entreprise"]), mots(b["entreprise"])
    if not ma or not mb:
        return False
    proches = len(ma & mb) / min(len(ma), len(mb)) >= 0.6
    if not proches:
        return False
    if None not in (a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon")):
        return distance_km(a["lat"], a["lon"], b["lat"], b["lon"]) < 0.5
    return not a.get("code_postal") or not b.get("code_postal") or a["code_postal"] == b["code_postal"]


def distance_km(lat1, lon1, lat2, lon2) -> float:
    dx = (lon2 - lon1) * 111.32 * cos(radians((lat1 + lat2) / 2))
    dy = (lat2 - lat1) * 110.57
    return sqrt(dx * dx + dy * dy)


def fusionner(annuaire: list[dict], osm: list[dict]) -> list[dict]:
    prospects = {p["siret"] or id(p): p for p in annuaire}.values()
    prospects = [dict(p, site="", telephone="", email="") for p in prospects]
    for lieu in osm:
        doublon = next((p for p in prospects if meme_entreprise(p, lieu)), None)
        if doublon:
            for champ in ("site", "telephone", "email"):
                doublon[champ] = doublon[champ] or lieu[champ]
            doublon["origine"] += "+openstreetmap"
        else:
            prospects.append(dict(lieu, contact="", siret="", date_creation="", effectif=""))
    return [p for p in prospects if p["entreprise"]]


# --- Recherche web par Claude (optionnelle) ------------------------------------------

SCHEMA_WEB = {
    "type": "object",
    "properties": {
        "site": {"type": "string"}, "telephone": {"type": "string"}, "email": {"type": "string"},
        "page_source": {"type": "string"},
    },
    "required": ["site", "telephone", "email", "page_source"],
    "additionalProperties": False,
}


def chercher_sur_le_web(p: dict, metier: str, modele: str) -> None:
    consigne = f"""Trouve les coordonnées professionnelles publiques de cette entreprise :
- nom : {p['entreprise']}
- métier : {metier}
- adresse : {p.get('adresse', '')} {p.get('code_postal', '')} {p.get('ville', '')}

Renvoie son site officiel (pas un annuaire), son téléphone et son e-mail professionnel, et l'URL de la page où tu as trouvé l'e-mail.
Ne renvoie que ce que tu as réellement vu dans les résultats de recherche. En cas de doute sur l'entreprise, laisse vide."""
    systeme = "Tu fais des recherches web factuelles. Tu n'inventes jamais une URL, un numéro ou une adresse e-mail."
    r = demander(consigne, SCHEMA_WEB, systeme, modele=modele, outils="WebSearch", delai=240)
    if r["site"] and not p["site"] and site_correspond(r["site"], p["entreprise"]):
        p["site"] = r["site"]
    p["telephone"] = p["telephone"] or r["telephone"]
    if r["email"] and not p["email"] and "@" in r["email"]:
        p["email"] = r["email"]
        p["source_email"] = domaine(r["page_source"]) or "internet"
    p["origine"] += "+web"


def site_correspond(site: str, nom: str) -> bool:
    """Vérifie que le site trouvé parle bien de cette entreprise (évite les erreurs d'homonymes)."""
    try:
        texte = audit.requests.get(audit.normaliser_url(site), headers=audit.ENTETES, timeout=15).text
    except audit.requests.RequestException:
        return False
    mots_page = mots(texte[:200_000])
    return bool(mots(nom) & mots_page)


def domaine(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", url or "")
    return m.group(1) if m else ""


# --- Score et constat ---------------------------------------------------------------

def evaluer(p: dict, resultat_audit: dict) -> None:
    anciennete = sources.anciennete_ans(p.get("date_creation") or "")
    if not p["site"]:
        score = 45 + (10 if anciennete and anciennete >= 3 else 0) + (10 if p.get("effectif") not in ("", "NN", "00") else 0)
        constat = "Je n'ai pas trouvé de site internet pour votre entreprise"
        if anciennete and anciennete >= 3:
            constat += f", alors qu'elle existe depuis {int(anciennete)} ans"
        problemes = ["pas de site"]
    else:
        codes = sorted(resultat_audit["problemes"], key=lambda c: -POIDS.get(c[0], 0))
        score = min(100, sum(POIDS.get(code, 0) for code, _ in codes) + (10 if resultat_audit["constructeur"] else 0))
        problemes = [texte for _, texte in codes]
        constat = " et ".join(problemes[:2])
        constat = constat[:1].upper() + constat[1:]
        if resultat_audit["emails"] and not p["email"]:
            p["email"] = resultat_audit["emails"][0]
            p["source_email"] = "votre site internet"
        if resultat_audit["constructeur"]:
            problemes.append(f"site fait avec {resultat_audit['constructeur']}")
    if p.get("effectif") in ("21", "22", "31", "32", "41", "42", "51", "52", "53"):
        score = max(0, score - 40)  # 50 salariés et plus : rarement la bonne cible
    p["score"] = score
    p["constat"] = constat
    p["problemes"] = " ; ".join(problemes)
    p["effectif"] = sources.EFFECTIFS.get(p.get("effectif", ""), "")
    if p["email"]:
        p["source"] = p.get("source_email") or ("OpenStreetMap" if "openstreetmap" in p["origine"] else "internet")


# --- Programme principal -------------------------------------------------------------

def main():
    metiers = yaml.safe_load((DOSSIER / "metiers.yaml").read_text(encoding="utf-8"))
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("metier", nargs="?", help="métier ciblé (voir --liste)")
    parser.add_argument("ville", nargs="?", help="ville autour de laquelle chercher")
    parser.add_argument("--rayon", type=float, default=5, help="rayon en km (défaut : 5)")
    parser.add_argument("--max", type=int, default=100, help="nombre maximum d'entreprises (défaut : 100)")
    parser.add_argument("--web", type=int, default=0, metavar="N",
                        help="fait chercher par Claude le site/l'e-mail des N entreprises sans site (ton abonnement)")
    parser.add_argument("--modele", default="", help="modèle Claude pour --web (défaut : celui de ton abonnement)")
    parser.add_argument("--sortie", type=Path, default=DOSSIER / "prospects.csv")
    parser.add_argument("--liste", action="store_true", help="affiche les métiers disponibles")
    args = parser.parse_args()

    if args.liste or not args.metier:
        print("Métiers disponibles :", ", ".join(metiers))
        return
    if args.metier not in metiers:
        sys.exit(f"Métier inconnu : {args.metier}. Disponibles : {', '.join(metiers)} (ajoute-le dans metiers.yaml).")
    if not args.ville:
        sys.exit("Précise une ville, par exemple : python recherche.py plombier Versailles")
    config = metiers[args.metier]

    try:
        commune = sources.trouver_commune(args.ville)
        print(f"Recherche des {args.metier}s à {args.rayon:g} km autour de {commune['nom']}…")
        annuaire = sources.entreprises_autour(commune["lat"], commune["lon"], args.rayon, config["naf"], args.max)
        print(f"  annuaire officiel : {len(annuaire)} établissement(s)")
        try:
            osm = sources.lieux_osm(commune["lat"], commune["lon"], args.rayon, config.get("osm", []))
            print(f"  OpenStreetMap : {len(osm)} lieu(x)")
        except sources.ErreurSource as e:
            osm = []
            print(f"  OpenStreetMap indisponible, on continue sans ({e})")
    except sources.ErreurSource as e:
        sys.exit(f"Erreur : {e}")

    prospects = fusionner(annuaire, osm)[: args.max]
    for p in prospects:
        p["metier"] = args.metier

    sans_site = [p for p in prospects if not p["site"]][: args.web]
    for i, p in enumerate(sans_site, 1):
        print(f"  recherche web {i}/{len(sans_site)} : {p['entreprise']}…")
        try:
            chercher_sur_le_web(p, args.metier, args.modele)
        except ErreurClaude as e:
            print(f"    ✗ {e}")
            if "installé" in str(e):
                break

    print(f"Audit de {sum(1 for p in prospects if p['site'])} site(s)…")
    with ThreadPoolExecutor(max_workers=8) as pool:
        audits = list(pool.map(lambda p: audit.auditer(p["site"], config.get("rdv", False)), prospects))
    for p, a in zip(prospects, audits):
        evaluer(p, a)

    prospects.sort(key=lambda p: (-p["score"], not p["email"]))
    with open(args.sortie, "w", encoding="utf-8-sig", newline="") as f:  # utf-8-sig : lisible directement dans Excel
        ecrivain = csv.DictWriter(f, fieldnames=COLONNES, extrasaction="ignore")
        ecrivain.writeheader()
        ecrivain.writerows(prospects)

    avec_email = sum(1 for p in prospects if p["email"])
    avec_tel = sum(1 for p in prospects if p["telephone"] and not p["email"])
    print(f"\n{len(prospects)} prospects écrits dans {args.sortie.name} : {avec_email} avec e-mail, "
          f"{avec_tel} joignables seulement par téléphone.")
    print("Les meilleurs :")
    for p in prospects[:5]:
        print(f"  {p['score']:>3}  {p['entreprise'][:35]:<36} {p['constat'][:70]}")
    print("\nRelis la liste et supprime les lignes qui ne te conviennent pas avant de lancer l'agent.")


if __name__ == "__main__":
    main()
