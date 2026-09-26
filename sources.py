"""Sources de données gratuites : communes (geo.api.gouv.fr), entreprises (API Recherche
d'entreprises de l'État) et commerces/artisans cartographiés (OpenStreetMap)."""

import time
from datetime import date

import requests

ENTETES = {"User-Agent": "prospection-agent/1.0 (outil personnel de prospection)"}
TIMEOUT = 30


class ErreurSource(Exception):
    pass


def _get(url: str, **params) -> dict | list:
    try:
        r = requests.get(url, params=params, headers=ENTETES, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise ErreurSource(f"{url} injoignable : {e}")
    if r.status_code == 429:  # trop de requêtes : on patiente puis on réessaie une fois
        time.sleep(2)
        r = requests.get(url, params=params, headers=ENTETES, timeout=TIMEOUT)
    if not r.ok:
        raise ErreurSource(f"{url} a répondu {r.status_code} : {r.text[:200]}")
    return r.json()


# --- Communes ------------------------------------------------------------------

def trouver_commune(nom: str) -> dict:
    """Renvoie {nom, code_insee, codes_postaux, lat, lon} pour la commune la plus peuplée de ce nom."""
    resultats = _get("https://geo.api.gouv.fr/communes", nom=nom, fields="nom,code,codesPostaux,centre,population",
                     boost="population", limit=5)
    if not resultats:
        raise ErreurSource(f"commune introuvable : {nom}")
    c = resultats[0]
    lon, lat = c["centre"]["coordinates"]
    return {"nom": c["nom"], "code_insee": c["code"], "codes_postaux": c.get("codesPostaux", []), "lat": lat, "lon": lon}


# --- Annuaire officiel des entreprises ------------------------------------------

def _titre(texte: str) -> str:
    return " ".join(m.capitalize() for m in (texte or "").lower().split())


def _dirigeant(resultat: dict) -> str:
    for d in resultat.get("dirigeants") or []:
        if d.get("type_dirigeant", "personne physique") == "personne physique" and d.get("nom"):
            prenom = (d.get("prenoms") or "").split()[0] if d.get("prenoms") else ""
            return f"{_titre(prenom)} {_titre(d['nom'])}".strip()
    return ""


def _nom_affiche(resultat: dict, etab: dict) -> str:
    enseignes = etab.get("liste_enseignes") or []
    nom = etab.get("nom_commercial") or (enseignes[0] if enseignes else "") or resultat.get("nom_complet") or ""
    return _titre(nom)


def entreprises_autour(lat: float, lon: float, rayon_km: float, codes_naf: list[str], max_resultats: int) -> list[dict]:
    """Établissements en activité exerçant ces activités dans le rayon donné."""
    prospects, page = [], 1
    while len(prospects) < max_resultats:
        donnees = _get("https://recherche-entreprises.api.gouv.fr/near_point", lat=lat, long=lon, radius=rayon_km,
                       activite_principale=",".join(codes_naf), page=page, per_page=25)
        resultats = donnees.get("results") or []
        for r in resultats:
            etabs = r.get("matching_etablissements") or [r.get("siege") or {}]
            for e in etabs:
                if e.get("etat_administratif", "A") != "A":
                    continue
                prospects.append({
                    "entreprise": _nom_affiche(r, e),
                    "contact": _dirigeant(r),
                    "adresse": _titre(e.get("adresse", "")),
                    "code_postal": e.get("code_postal", ""),
                    "ville": _titre(e.get("libelle_commune", "")),
                    "siret": e.get("siret", ""),
                    "date_creation": e.get("date_creation") or r.get("date_creation") or "",
                    "effectif": e.get("tranche_effectif_salarie") or r.get("tranche_effectif_salarie") or "",
                    "lat": _nombre(e.get("latitude")), "lon": _nombre(e.get("longitude")),
                    "origine": "annuaire",
                })
        if page >= int(donnees.get("total_pages") or 1) or not resultats:
            break
        page += 1
        time.sleep(0.2)  # l'API accepte 7 requêtes par seconde
    return prospects[:max_resultats]


def anciennete_ans(date_creation: str) -> float | None:
    try:
        return (date.today() - date.fromisoformat(date_creation[:10])).days / 365.25
    except ValueError:
        return None


# Tranches d'effectif salarié de l'INSEE ("NN" ou vide = pas de salarié connu).
EFFECTIFS = {"00": "0 salarié", "01": "1-2 salariés", "02": "3-5 salariés", "03": "6-9 salariés", "11": "10-19 salariés",
             "12": "20-49 salariés", "21": "50-99 salariés", "22": "100-199 salariés"}


def _nombre(valeur) -> float | None:
    try:
        return float(valeur)
    except (TypeError, ValueError):
        return None


# --- OpenStreetMap ----------------------------------------------------------------

def lieux_osm(lat: float, lon: float, rayon_km: float, etiquettes: list[str]) -> list[dict]:
    """Commerces et artisans cartographiés sur OpenStreetMap, souvent avec site, téléphone, e-mail."""
    if not etiquettes:
        return []
    filtres = "".join(
        f'nwr["{cle}"="{valeur}"]["name"](around:{int(rayon_km * 1000)},{lat},{lon});'
        for cle, valeur in (e.split("=", 1) for e in etiquettes)
    )
    requete = f"[out:json][timeout:60];({filtres});out center tags;"
    try:
        r = requests.post("https://overpass-api.de/api/interpreter", data={"data": requete}, headers=ENTETES, timeout=90)
        r.raise_for_status()
    except requests.RequestException as e:
        raise ErreurSource(f"OpenStreetMap injoignable : {e}")
    lieux = []
    for el in r.json().get("elements", []):
        t = el.get("tags", {})
        centre = el.get("center") or el
        lieux.append({
            "entreprise": t.get("name", ""),
            "site": t.get("website") or t.get("contact:website") or "",
            "telephone": t.get("phone") or t.get("contact:phone") or "",
            "email": t.get("email") or t.get("contact:email") or "",
            "adresse": " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x),
            "code_postal": t.get("addr:postcode", ""),
            "ville": t.get("addr:city", ""),
            "lat": centre.get("lat"), "lon": centre.get("lon"),
            "origine": "openstreetmap",
        })
    return lieux
