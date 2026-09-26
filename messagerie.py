"""Envoi (SMTP) et lecture (IMAP) des e-mails."""

import email
import imaplib
import re
import smtplib
from email.message import EmailMessage
from email.policy import default as politique
from email.utils import formataddr, make_msgid, parseaddr

MOTIFS_REPONSE_AUTO = re.compile(r"(r[ée]ponse automatique|absence|absent|out of office|automatic reply|autoreply)", re.I)
MOTIFS_CITATION = re.compile(r"^(Le .+a [ée]crit\s*:|On .+wrote\s*:|-----\s*(Original Message|Message d'origine)\s*-----)", re.I)


class Messagerie:
    def __init__(self, config: dict, expediteur_nom: str, expediteur_email: str, mot_de_passe: str):
        self.config = config
        self.expediteur = formataddr((expediteur_nom, expediteur_email))
        self.expediteur_email = expediteur_email
        self.mot_de_passe = mot_de_passe

    def envoyer(self, destinataire: str, objet: str, corps: str,
                en_reponse_a: str | None = None, references: str | None = None) -> str:
        msg = EmailMessage()
        msg["From"] = self.expediteur
        msg["To"] = destinataire
        msg["Subject"] = objet
        msg["Message-ID"] = make_msgid(domain=self.expediteur_email.split("@")[-1])
        msg["List-Unsubscribe"] = f"<mailto:{self.expediteur_email}?subject=STOP>"
        if en_reponse_a:
            msg["In-Reply-To"] = en_reponse_a
            msg["References"] = f"{references or ''} {en_reponse_a}".strip()
        msg.set_content(corps)

        c = self.config
        if int(c.get("smtp_port", 465)) == 465:
            serveur = smtplib.SMTP_SSL(c["smtp_serveur"], 465, timeout=30)
        else:
            serveur = smtplib.SMTP(c["smtp_serveur"], int(c["smtp_port"]), timeout=30)
            serveur.starttls()
        with serveur:
            serveur.login(c["identifiant"], self.mot_de_passe)
            serveur.send_message(msg)
        return msg["Message-ID"]

    def _imap(self):
        imap = imaplib.IMAP4_SSL(self.config["imap_serveur"], int(self.config.get("imap_port", 993)))
        imap.login(self.config["identifiant"], self.mot_de_passe)
        imap.select("INBOX")
        return imap

    def nouveaux_messages(self) -> list[dict]:
        """Messages non lus de la boîte de réception, sans les marquer comme lus."""
        imap = self._imap()
        try:
            _, donnees = imap.uid("search", None, "UNSEEN")
            messages = []
            for uid in donnees[0].split():
                _, brut = imap.uid("fetch", uid, "(BODY.PEEK[])")
                msg = email.message_from_bytes(brut[0][1], policy=politique)
                messages.append(analyser_message(uid.decode(), msg))
            return messages
        finally:
            imap.logout()

    def marquer_lu(self, uid: str):
        imap = self._imap()
        try:
            imap.uid("store", uid, "+FLAGS", "(\\Seen)")
        finally:
            imap.logout()


def analyser_message(uid: str, msg) -> dict:
    corps = ""
    partie = msg.get_body(preferencelist=("plain", "html"))
    if partie is not None:
        corps = partie.get_content()
        if partie.get_content_subtype() == "html":
            corps = re.sub(r"<[^>]+>", " ", corps)
    objet = str(msg.get("Subject", ""))
    auto_submitted = str(msg.get("Auto-Submitted", "no")).lower()
    return {
        "uid": uid,
        "de": parseaddr(str(msg.get("From", "")))[1].lower(),
        "objet": objet,
        "corps": sans_citation(corps),
        "message_id": str(msg.get("Message-ID", "")),
        "references": str(msg.get("References", "")),
        "reponse_auto": auto_submitted != "no" or bool(msg.get("X-Autoreply")) or bool(MOTIFS_REPONSE_AUTO.search(objet)),
    }


def sans_citation(texte: str) -> str:
    """Retire l'historique cité sous la réponse du prospect."""
    lignes = []
    for ligne in texte.splitlines():
        if MOTIFS_CITATION.match(ligne.strip()):
            break
        if not ligne.startswith(">"):
            lignes.append(ligne)
    return "\n".join(lignes).strip()
