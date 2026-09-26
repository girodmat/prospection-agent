"""Agent de prospection autonome : il écrit, envoie, lit les réponses et y répond.

Commandes :
  python agent.py presentation            -> kit de présentation (pitch, e-mail, LinkedIn, téléphone)
  python agent.py envoyer [prospects.csv] -> importe les prospects, envoie premiers e-mails et relances
  python agent.py relever                 -> lit les réponses et y répond (ou te les transmet)
  python agent.py tourner [prospects.csv] -> relever puis envoyer (à lancer régulièrement)
  python agent.py statut                  -> où en est chaque prospect

Ajoute --simulation pour tout faire sans rien envoyer : les messages sont écrits dans sortie/.
"""

import argparse
import csv
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml

import redaction
from messagerie import Messagerie
from redaction import ErreurRedaction, mention_legale, signature

DOSSIER = Path(__file__).parent
SORTIE = DOSSIER / "sortie"
FICHIER_SUIVI = DOSSIER / "suivi.json"
FICHIER_DESINSCRITS = DOSSIER / "desinscrits.txt"
A_REMPLIR = "À REMPLIR"
EXPEDITEURS_REJET = ("mailer-daemon", "postmaster")

# Statuts où l'agent a encore la main. Ailleurs (a_traiter, clos…), c'est toi qui gères.
STATUTS_AUTO = {"contacte", "relance", "en_discussion", "chaud"}


def maintenant() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --- Profil -------------------------------------------------------------------

def charger_profil(chemin: Path) -> dict:
    with open(chemin, encoding="utf-8") as f:
        return yaml.safe_load(f)


def champs_a_remplir(valeur, chemin="") -> list[str]:
    """Liste les champs du profil qui contiennent encore « À REMPLIR »."""
    if isinstance(valeur, dict):
        return [c for k, v in valeur.items() for c in champs_a_remplir(v, f"{chemin}.{k}".lstrip("."))]
    if isinstance(valeur, list):
        return [c for i, v in enumerate(valeur) for c in champs_a_remplir(v, f"{chemin}[{i}]")]
    if isinstance(valeur, str) and A_REMPLIR in valeur:
        return [chemin]
    return []


# --- Suivi des prospects --------------------------------------------------------

class Suivi:
    """Fiche et historique de chaque prospect, indexés par adresse e-mail (suivi.json)."""

    def __init__(self, chemin: Path):
        self.chemin = chemin
        self.prospects: dict[str, dict] = json.loads(chemin.read_text(encoding="utf-8")) if chemin.exists() else {}

    def sauver(self):
        self.chemin.write_text(json.dumps(self.prospects, ensure_ascii=False, indent=2), encoding="utf-8")

    def importer_csv(self, fichier: Path) -> int:
        desinscrits = lire_desinscrits()
        ajoutes = 0
        with open(fichier, encoding="utf-8", newline="") as f:
            for ligne in csv.DictReader(f):
                email = (ligne.get("email") or "").strip().lower()
                if not email or email in self.prospects:
                    continue
                fiche = {k: (v or "").strip() for k, v in ligne.items() if k}
                fiche.update(email=email, statut="desinscrit" if email in desinscrits else "nouveau",
                             relances=0, reponses_auto=0, historique=[])
                self.prospects[email] = fiche
                ajoutes += 1
        return ajoutes

    def envois_du_jour(self) -> int:
        aujourd_hui = datetime.now().date().isoformat()
        return sum(1 for p in self.prospects.values() for m in p["historique"]
                   if m["sens"] == "envoye" and m.get("type") in ("premier", "relance") and m["date"].startswith(aujourd_hui))


def lire_desinscrits() -> set[str]:
    if not FICHIER_DESINSCRITS.exists():
        return set()
    return {l.strip().lower() for l in FICHIER_DESINSCRITS.read_text(encoding="utf-8").splitlines() if l.strip()}


def ajouter_desinscrit(email: str):
    if email not in lire_desinscrits():
        with open(FICHIER_DESINSCRITS, "a", encoding="utf-8") as f:
            f.write(email + "\n")


# --- Agent ----------------------------------------------------------------------

class Agent:
    def __init__(self, messagerie, profil: dict, suivi: Suivi, simulation: bool = False):
        self.messagerie = messagerie
        self.profil = profil
        self.config = profil["envoi"]
        self.suivi = suivi
        self.simulation = simulation
        self.journal: list[str] = []  # ce qui a été fait (écrit dans sortie/ en simulation)

    def log(self, texte: str):
        self.journal.append(texte)
        print(texte, file=sys.stderr)

    def _envoyer(self, p: dict, objet: str, corps: str, type_: str, en_reponse_a: str | None = None):
        if self.simulation:
            message_id = f"<simulation-{len(self.journal)}@local>"
            self.journal.append(f"\n---\n**À :** {p['email']}\n**Objet :** {objet}\n\n{corps}\n\n---")
        else:
            message_id = self.messagerie.envoyer(p["email"], objet, corps, en_reponse_a=en_reponse_a,
                                                 references=p.get("references"))
        p["historique"].append({"date": maintenant(), "sens": "envoye", "type": type_,
                                "objet": objet, "corps": corps, "message_id": message_id})
        p["dernier_message_id"] = message_id

    def _pause(self):
        if not self.simulation:
            pause = float(self.config.get("pause_entre_envois", 60))
            time.sleep(random.uniform(pause * 0.5, pause * 1.5))

    def _pied(self, p: dict, avec_mention: bool) -> str:
        pied = "\n\n" + signature(self.profil)
        if avec_mention:
            pied += "\n\n--\n" + mention_legale(p.get("source") or "votre site internet")
        return pied

    def _confier(self, p: dict, raison: str, brouillon: dict | None = None):
        """Passe la main : le prospect n'est plus traité automatiquement et tu es prévenu."""
        p["statut"] = "a_traiter"
        texte = f"{p.get('entreprise')} ({p['email']}) : {raison}"
        if brouillon and brouillon.get("corps"):
            texte += f"\n\nBrouillon proposé :\nObjet : {brouillon['objet']}\n\n{brouillon['corps']}"
        if brouillon and brouillon.get("a_verifier"):
            texte += "\n\nPoints à vérifier :\n" + "\n".join(f"- {x}" for x in brouillon["a_verifier"])
        self._notifier(f"À traiter : {p.get('entreprise')}", texte, p)
        self.log(f"  ⚠ confié à toi : {texte}")

    def _notifier(self, sujet: str, texte: str, p: dict):
        if self.simulation or not self.config.get("notifier", True):
            return
        dernier = next((m for m in reversed(p["historique"]) if m["sens"] == "recu"), None)
        if dernier:
            texte += f"\n\nSon dernier message :\n{dernier['corps']}"
        try:
            self.messagerie.envoyer(self.profil["identite"]["email"], f"[Prospection] {sujet}", texte)
        except Exception as e:  # une notification ratée ne doit pas bloquer l'agent
            self.log(f"  (notification non envoyée : {e})")

    # Envoi des premiers e-mails et des relances --------------------------------

    def envoyer(self):
        restant = int(self.config.get("limite_par_jour", 30)) - self.suivi.envois_du_jour()
        a_relancer = [p for p in self.suivi.prospects.values() if self._relance_due(p)]
        nouveaux = [p for p in self.suivi.prospects.values() if p["statut"] == "nouveau"]

        for p in a_relancer + nouveaux:
            if restant <= 0:
                self.log("Limite d'envois du jour atteinte : la suite partira au prochain passage.")
                break
            premier = p["statut"] == "nouveau"
            try:
                r = (redaction.rediger_premier_email if premier else redaction.rediger_relance)(self.profil, p)
            except ErreurRedaction as e:
                self.log(f"  ✗ {p.get('entreprise')} : {e}")
                continue
            if r["a_verifier"]:
                self._confier(p, "l'agent n'a pas pu rédiger sans information manquante", r)
                continue
            if premier:
                p["objet_initial"] = r["objet"]
                self._envoyer(p, r["objet"], r["corps"] + self._pied(p, True), "premier")
                p["statut"] = "contacte"
                self.log(f"  ✉ premier e-mail → {p.get('entreprise')}")
            else:
                objet = "Re: " + p.get("objet_initial", r["objet"])
                self._envoyer(p, objet, r["corps"] + self._pied(p, True), "relance",
                              en_reponse_a=p.get("dernier_message_id"))
                p["relances"] += 1
                p["statut"] = "relance"
                self.log(f"  ↻ relance → {p.get('entreprise')}")
            restant -= 1
            self._pause()

    def _relance_due(self, p: dict) -> bool:
        if p["statut"] not in ("contacte", "relance") or p["relances"] >= int(self.config.get("relances_max", 1)):
            return False
        dernier_envoi = max(m["date"] for m in p["historique"] if m["sens"] == "envoye")
        delai = timedelta(days=int(self.config.get("relance_apres_jours", 4)))
        return datetime.fromisoformat(dernier_envoi) + delai <= datetime.now()

    # Lecture et traitement des réponses -----------------------------------------

    def relever(self):
        for msg in self.messagerie.nouveaux_messages():
            if msg["de"].split("@")[0] in EXPEDITEURS_REJET:
                self._traiter_rejet(msg)
                continue
            p = self.suivi.prospects.get(msg["de"])
            if p is None:
                continue  # pas un prospect : le message reste non lu
            self._traiter_reponse(p, msg)
            if not self.simulation:
                self.messagerie.marquer_lu(msg["uid"])

    def _traiter_rejet(self, msg: dict):
        """Adresse invalide : le serveur de messagerie nous a renvoyé notre e-mail."""
        for email, p in self.suivi.prospects.items():
            if email in msg["corps"].lower() and p["statut"] in STATUTS_AUTO:
                p["statut"] = "invalide"
                self.log(f"  ✗ adresse invalide : {p.get('entreprise')} ({email})")
                if not self.simulation:
                    self.messagerie.marquer_lu(msg["uid"])

    def _traiter_reponse(self, p: dict, msg: dict):
        if msg["reponse_auto"]:
            self.log(f"  · réponse automatique ignorée : {p.get('entreprise')}")
            return
        p["historique"].append({"date": maintenant(), "sens": "recu", "objet": msg["objet"],
                                "corps": msg["corps"], "message_id": msg["message_id"]})
        p["dernier_message_id"] = msg["message_id"]
        p["references"] = msg["references"]

        if p["statut"] == "desinscrit":
            return
        if p["statut"] not in STATUTS_AUTO:
            self._notifier(f"Nouveau message de {p.get('entreprise')}", "Ce prospect t'a été confié : réponds-lui toi-même.", p)
            self.log(f"  ✉ message de {p.get('entreprise')} (conversation gérée par toi)")
            return
        if p["reponses_auto"] >= int(self.config.get("reponses_auto_max", 3)):
            self._confier(p, "la conversation a atteint le nombre maximal de réponses automatiques")
            return

        try:
            r = redaction.analyser_reponse(self.profil, p)
        except ErreurRedaction as e:
            self._confier(p, f"l'agent n'a pas pu analyser la réponse ({e})")
            return

        intention = r["intention"]
        if intention == "desinscription":
            p["statut"] = "desinscrit"
            ajouter_desinscrit(p["email"])
            self.log(f"  ⊘ désinscription : {p.get('entreprise')}")
            return
        if intention == "autre" or r["a_verifier"]:
            self._confier(p, f"réponse à vérifier ({intention}) – {r['resume']}", r)
            return

        objet = msg["objet"] if msg["objet"].lower().startswith("re") else "Re: " + msg["objet"]
        self._envoyer(p, objet, r["corps"] + self._pied(p, False), "reponse", en_reponse_a=msg["message_id"])
        p["reponses_auto"] += 1
        p["statut"] = {"interesse": "chaud", "question": "en_discussion", "pas_interesse": "clos"}[intention]
        self.log(f"  ↩ réponse ({intention}) → {p.get('entreprise')} : {r['resume']}")
        if intention == "interesse":
            self._notifier(f"{p.get('entreprise')} est intéressé !",
                           f"{r['resume']}\n\nL'agent lui a proposé un appel et attend ses disponibilités.", p)


# --- Commandes -------------------------------------------------------------------

def ecrire_sortie(nom: str, contenu: str) -> Path:
    SORTIE.mkdir(exist_ok=True)
    chemin = SORTIE / f"{nom}-{datetime.now():%Y%m%d-%H%M%S}.md"
    chemin.write_text(contenu, encoding="utf-8")
    return chemin


def cmd_presentation(profil: dict) -> Path:
    r = redaction.rediger_presentation(profil)
    contenu = f"""# Kit de présentation

## Pitch court
{r['pitch_court']}

## E-mail de présentation
**Objet :** {r['email_objet']}

{r['email_corps']}

{signature(profil)}

## Message LinkedIn
{r['message_linkedin']}

## Pitch téléphone (30 s)
{r['pitch_telephone']}
"""
    if r["a_verifier"]:
        contenu += "\n**À compléter dans ton profil :**\n" + "\n".join(f"- {x}" for x in r["a_verifier"]) + "\n"
    return ecrire_sortie("presentation", contenu)


def cmd_statut(suivi: Suivi):
    if not suivi.prospects:
        print("Aucun prospect. Lance : python agent.py envoyer prospects.csv")
        return
    compte: dict[str, int] = {}
    for p in suivi.prospects.values():
        compte[p["statut"]] = compte.get(p["statut"], 0) + 1
    print("  ".join(f"{s}: {n}" for s, n in sorted(compte.items())) + "\n")
    for p in sorted(suivi.prospects.values(), key=lambda p: p["statut"]):
        dernier = p["historique"][-1]["date"][:16].replace("T", " ") if p["historique"] else "-"
        print(f"{p['statut']:<14} {p.get('entreprise', '')[:30]:<31} {p['email']:<35} {dernier}")


def creer_messagerie(profil: dict) -> Messagerie:
    mot_de_passe = os.environ.get("MOT_DE_PASSE_EMAIL")
    if not mot_de_passe:
        sys.exit("Définis la variable d'environnement MOT_DE_PASSE_EMAIL (voir le README).")
    ident = profil["identite"]
    return Messagerie(profil["envoi"], f"{ident['prenom']} {ident['nom']}", ident["email"], mot_de_passe)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profil", type=Path, default=DOSSIER / "profil.yaml")
    parser.add_argument("--simulation", action="store_true", help="n'envoie rien et ne modifie pas le suivi")
    sous = parser.add_subparsers(dest="commande", required=True)
    sous.add_parser("presentation", help="génère ton kit de présentation")
    for nom, aide in (("envoyer", "envoie premiers e-mails et relances"), ("tourner", "relever puis envoyer")):
        s = sous.add_parser(nom, help=aide)
        s.add_argument("csv", type=Path, nargs="?", help="nouveaux prospects à importer")
    sous.add_parser("relever", help="lit les réponses et y répond")
    sous.add_parser("statut", help="où en est chaque prospect")
    args = parser.parse_args()

    suivi = Suivi(FICHIER_SUIVI)
    if args.commande == "statut":
        cmd_statut(suivi)
        return

    profil = charger_profil(args.profil)
    manquants = champs_a_remplir(profil)
    if manquants:
        sys.exit("Complète d'abord ton profil (profil.yaml). Champs à remplir :\n  - " + "\n  - ".join(manquants))

    try:
        if args.commande == "presentation":
            print(f"Kit écrit dans {cmd_presentation(profil).relative_to(DOSSIER)}")
            return
        agent = Agent(creer_messagerie(profil), profil, suivi, simulation=args.simulation)
        if getattr(args, "csv", None):
            agent.log(f"{suivi.importer_csv(args.csv)} nouveau(x) prospect(s) importé(s).")
        if args.commande in ("relever", "tourner"):
            agent.relever()
        if args.commande in ("envoyer", "tourner"):
            agent.envoyer()
    except ErreurRedaction as e:
        sys.exit(str(e))
    finally:
        # Même en cas d'erreur en cours de route, on garde la trace de ce qui est déjà parti.
        if args.commande != "presentation" and not args.simulation:
            suivi.sauver()

    if args.simulation:
        chemin = ecrire_sortie("simulation", "# Simulation (rien n'a été envoyé)\n\n" + "\n".join(agent.journal))
        print(f"Simulation écrite dans {chemin.relative_to(DOSSIER)} — le suivi n'a pas été modifié.")


if __name__ == "__main__":
    main()
