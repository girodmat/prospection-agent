"""Appel de Claude via Claude Code (commande `claude`), connecté à ton abonnement Claude :
pas de clé API, pas de tokens facturés. L'usage compte dans les limites de ton abonnement."""

import json
import os
import shutil
import subprocess
import tempfile


class ErreurClaude(Exception):
    pass


def demander(consigne: str, schema: dict, systeme: str, modele: str = "", outils: str = "", delai: int = 300) -> dict:
    """Envoie une consigne à Claude et renvoie sa réponse structurée selon `schema`.

    `outils` : "" = aucun outil ; "WebSearch" = Claude peut chercher sur le web.
    """
    commande = shutil.which("claude")
    if not commande:
        raise ErreurClaude("Claude Code n'est pas installé : voir le README, puis lance `claude` une fois pour te connecter.")
    arguments = [
        commande, "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(schema),
        "--system-prompt", systeme,
        "--tools", outils,
        "--strict-mcp-config",
        "--no-session-persistence",
    ]
    if outils:
        arguments += ["--allowedTools", outils]
    if modele:
        arguments += ["--model", modele]
    # Sans clé API dans l'environnement, Claude Code utilise la connexion à ton abonnement.
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    try:
        resultat = subprocess.run(arguments, input=consigne, capture_output=True, text=True, encoding="utf-8",
                                  env=env, cwd=tempfile.gettempdir(), timeout=delai)
    except subprocess.TimeoutExpired:
        raise ErreurClaude("Claude n'a pas répondu à temps")
    try:
        sortie = json.loads(resultat.stdout)
    except json.JSONDecodeError:
        raise ErreurClaude(f"réponse illisible de Claude Code : {(resultat.stderr or resultat.stdout).strip()[:300]}")
    if sortie.get("is_error") or not isinstance(sortie.get("structured_output"), dict):
        raise ErreurClaude(f"Claude Code : {sortie.get('result') or sortie.get('subtype')}")
    return sortie["structured_output"]
