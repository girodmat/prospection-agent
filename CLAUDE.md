# Prospection-agent — documentation du projet

Outil personnel de prospection pour un créateur de sites web indépendant (France) qui vend des
sites vitrines (400 € HT au lancement) + maintenance mensuelle à des TPE locales.

Deux outils en ligne de commande, en Python, sans base de données ni serveur :

1. **`recherche.py`** — trouve les prospects : entreprises d'un métier autour d'une ville, audit de
   leur site, score, constat → `prospects.csv`.
2. **`agent.py`** — prospection par e-mail autonome : envoie, relance, lit les réponses, y répond,
   et confie à l'utilisateur ce qui sort de son profil.

Le guide utilisateur (installation, commandes, planification) est dans `README.md`. Ce fichier-ci
décrit le fonctionnement interne, les choix de conception et l'état du projet.

## Contraintes de l'utilisateur (à respecter)

- **Aucun coût à l'usage** : pas de clé API Anthropic, pas d'API payante. Claude est appelé via
  **Claude Code sur l'abonnement Claude** de l'utilisateur (`claude -p`, voir `claude_cli.py`).
  Les sources de données sont gratuites (API de l'État, OpenStreetMap) et l'audit des sites est fait localement.
- **Autonomie** : l'utilisateur veut que l'agent envoie et réponde seul, pas seulement des brouillons.
  Les garde-fous (ci-dessous) décident quand il doit rendre la main.
- **Utilisateur débutant** : messages, commentaires et documentation en français, simples.

## Architecture

```
recherche.py ──► sources.py (annuaire État, OpenStreetMap, communes)
     │      └──► audit.py   (audit HTTP des sites)
     │      └──► claude_cli.py (option --web : recherche web par Claude)
     ▼
prospects.csv  (relu et nettoyé par l'utilisateur)
     ▼
agent.py ──► redaction.py ──► claude_cli.py   (rédaction/analyse par Claude)
     │  └──► messagerie.py (SMTP pour envoyer, IMAP pour lire)
     ▼
suivi.json (état + historique de chaque prospect), desinscrits.txt, sortie/
```

| Fichier | Rôle |
|---|---|
| `recherche.py` | CLI de recherche : fusion des sources, recherche web optionnelle, audit en parallèle, score, constat, écriture du CSV |
| `sources.py` | Accès aux API gratuites : `geo.api.gouv.fr` (commune → coordonnées), `recherche-entreprises.api.gouv.fr/near_point` (établissements par code NAF et rayon), Overpass/OpenStreetMap (commerces avec site/tél/e-mail) |
| `audit.py` | Télécharge la page d'accueil (User-Agent mobile) et détecte les problèmes ; cherche les e-mails sur l'accueil puis sur les pages contact/mentions |
| `metiers.yaml` | Métier → codes NAF + étiquettes OSM + `rdv: true` si la réservation en ligne est un argument pour ce métier |
| `agent.py` | CLI de l'agent : classe `Suivi` (suivi.json), classe `Agent` (envoyer, relever), commandes `presentation`, `envoyer`, `relever`, `tourner`, `statut` |
| `redaction.py` | Prompt système construit à partir du profil, schémas JSON de sortie, consignes pour : kit de présentation, premier e-mail, relance, analyse de réponse ; signature et mention légale |
| `messagerie.py` | Envoi SMTP (465 SSL ou 587 STARTTLS, en-têtes de fil `In-Reply-To`/`References`, `List-Unsubscribe`) ; lecture IMAP des non-lus avec `BODY.PEEK` (ne marque pas lu) ; nettoyage des citations ; détection des réponses automatiques |
| `claude_cli.py` | Unique point d'appel à Claude : lance `claude -p --output-format json --json-schema … --system-prompt … --tools …` en retirant `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` de l'environnement (force l'abonnement), dans un dossier temporaire (évite de charger un CLAUDE.md), et renvoie `structured_output` |
| `profil.yaml` | Configuration de l'utilisateur : identité, présentation, offres, style, réglages d'envoi. **Seule source d'information autorisée pour Claude** |
| `prospects.exemple.csv` | Exemple du format d'entrée de l'agent |

## Outil 1 : `recherche.py`

`python recherche.py <metier> <ville> [--rayon 5] [--max 100] [--web N] [--modele ""] [--sortie prospects.csv]`

Déroulé :
1. `sources.trouver_commune` : la commune la plus peuplée portant ce nom → lat/lon.
2. `sources.entreprises_autour` : `near_point` paginé (25 par page, pause 0,2 s car limite de 7 req/s),
   filtré sur les établissements actifs (`etat_administratif == "A"`). Nom affiché = nom commercial,
   sinon enseigne, sinon `nom_complet`. Contact = premier dirigeant personne physique.
3. `sources.lieux_osm` : une requête Overpass `around:` pour toutes les étiquettes du métier. En cas
   d'échec, on continue sans OSM.
4. `fusionner` : rapproche OSM et annuaire par similarité de noms (mots normalisés sans accents ni
   formes juridiques, recouvrement ≥ 60 %) + distance < 500 m (ou même code postal si pas de
   coordonnées). OSM complète site/tél/e-mail ; un lieu OSM sans correspondance devient un prospect.
5. `--web N` : pour les N premiers prospects sans site, Claude cherche site/tél/e-mail avec l'outil
   `WebSearch`. Un site n'est retenu que si la page contient au moins un mot du nom de l'entreprise
   (`site_correspond`, anti-homonymes).
6. `audit.auditer` en parallèle (8 threads).
7. `evaluer` : score + constat + source de l'e-mail, puis tri (score décroissant, prospects avec e-mail d'abord).

### Score

- **Avec site** : somme des poids des problèmes (`POIDS` dans `recherche.py`) :
  injoignable 40, mobile 30, ancien 20, https 15, rdv 15, tel 10, lent 10, lourd 5, mentions 5 ;
  +10 si le site est fait avec un constructeur (Wix, Jimdo, PagesJaunes/Solocal…). Plafonné à 100.
- **Sans site** : 45, +10 si l'entreprise a ≥ 3 ans, +10 si elle a des salariés.
- **50 salariés et plus** : −40 (rarement la cible).

### Détections de l'audit (`audit.py`)

| code | règle |
|---|---|
| `injoignable` | erreur réseau, certificat invalide ou HTTP ≥ 400 |
| `mobile` | pas de balise `meta name="viewport"` |
| `https` | l'URL finale (après redirections) n'est pas en https |
| `ancien` | année du `©`/copyright la plus récente ≤ année courante − 4 |
| `rdv` | (métiers `rdv: true`) aucun outil connu : doctolib, planity, calendly, zenchef, thefork… |
| `tel` | aucun lien `tel:` |
| `lent` | page d'accueil > 3 s |
| `lourd` | page d'accueil > 4 Mo |
| `mentions` | le mot « mentions » n'apparaît pas |

Ces détections sont des heuristiques : l'utilisateur doit relire les constats avant l'envoi.

### Format de `prospects.csv` (UTF-8 avec BOM, pour Excel)

`score, entreprise, contact, metier, ville, email, telephone, site, constat, source, problemes, adresse,
code_postal, siret, date_creation, effectif, origine`

- `constat` : phrase reprise telle quelle dans le premier e-mail (les 2 problèmes les plus graves, ou
  « Je n'ai pas trouvé de site internet pour votre entreprise[, alors qu'elle existe depuis N ans] »).
- `source` : origine de l'e-mail (« votre site internet », « OpenStreetMap », domaine de la page
  trouvée par Claude…). Elle est citée dans la mention légale.
- `origine` : sources ayant fourni la ligne (`annuaire`, `openstreetmap`, `web`, combinées par `+`).

## Outil 2 : `agent.py`

Commandes : `presentation`, `envoyer [csv]`, `relever`, `tourner [csv]` (relever puis envoyer, à
planifier toutes les 30 min), `statut`. `--simulation` : tout est rédigé mais rien n'est envoyé ni
marqué lu, le suivi n'est pas sauvegardé, le journal est écrit dans `sortie/`.

L'agent refuse de démarrer tant que `profil.yaml` contient « À REMPLIR ». Le mot de passe e-mail vient
uniquement de la variable d'environnement `MOT_DE_PASSE_EMAIL`.

### Cycle de vie d'un prospect (`statut` dans suivi.json)

```
nouveau ──premier e-mail──► contacte ──(N jours sans réponse)──► relance
   │                           │  réponse analysée par Claude
   │                           ├─ interesse     → réponse auto (propose l'appel) → chaud  + notification
   │                           ├─ question      → réponse auto si tout est dans le profil → en_discussion
   │                           ├─ pas_interesse → remerciement → clos
   │                           ├─ desinscription → aucune réponse → desinscrit (+ desinscrits.txt)
   │                           └─ autre / info manquante → a_traiter + notification avec brouillon
   └─ info manquante pour rédiger → a_traiter
Rejet du serveur (mailer-daemon/postmaster mentionnant l'adresse) → invalide
```

Statuts où l'agent agit seul : `contacte`, `relance`, `en_discussion`, `chaud` (`STATUTS_AUTO`).
Ailleurs (`a_traiter`, `clos`), un nouveau message du prospect déclenche seulement une notification.

### Garde-fous (ne pas les affaiblir sans l'accord de l'utilisateur)

- Claude ne peut utiliser que le profil ; toute information manquante va dans `a_verifier`, et **un
  message avec `a_verifier` non vide n'est jamais envoyé** : il est confié à l'utilisateur.
- Les messages des prospects sont traités comme des données (anti-injection) ; une tentative de
  manipulation est classée `autre`, donc confiée.
- Signature et mention légale (origine de l'adresse + « répondez STOP ») sont **ajoutées par le code**,
  jamais rédigées par l'IA. Mention présente sur premiers e-mails et relances.
- `desinscrits.txt` est permanent : consulté à l'import et à chaque envoi. Ne jamais le vider.
- Limites : `limite_par_jour` (premiers e-mails + relances), `pause_entre_envois` (±50 % au hasard),
  `relances_max`, `reponses_auto_max` (au-delà, conversation confiée).
- Réponses automatiques (en-tête `Auto-Submitted`, `X-Autoreply`, objet « absence »…) ignorées.
  Les e-mails d'expéditeurs inconnus restent non lus et ne sont pas touchés.
- `suivi.json` est sauvegardé même en cas d'erreur en cours d'exécution (bloc `finally`), pour ne
  jamais réenvoyer un e-mail déjà parti.

### Format de `suivi.json`

Dictionnaire indexé par e-mail (minuscules). Chaque fiche = colonnes du CSV + `statut`, `relances`,
`reponses_auto`, `objet_initial`, `dernier_message_id`, `references`, et `historique` : liste de
`{date, sens: "envoye"|"recu", type: "premier"|"relance"|"reponse", objet, corps, message_id}`.

## Appels à Claude (`claude_cli.demander`)

- Sortie structurée garantie par `--json-schema` ; le résultat est lu dans le champ
  `structured_output` du JSON renvoyé par `claude -p --output-format json`. Vérifié réellement.
- `--tools ""` pour la rédaction (aucun outil), `--tools WebSearch --allowedTools WebSearch` pour la recherche web.
- `modele` dans `profil.yaml` (ou `--modele`) : vide = modèle par défaut de l'abonnement ; `sonnet`
  ou `opus` possibles (opus consomme plus vite les limites de l'abonnement).
- Erreurs (Claude Code absent, limite d'abonnement atteinte, refus, délai) → `ErreurClaude`,
  gérée prospect par prospect sans arrêter le lot.

## État du projet

**Vérifié en conditions réelles** : rédaction d'un premier e-mail et analyse d'une réponse via
`claude -p` (l'e-mail cite le constat ; une question hors profil est bien mise dans `a_verifier`).

**Vérifié uniquement avec des doublures (faux client mail, faux Claude, faux sites servis en local)** :
tous les scénarios de l'agent (limite par jour, relance, désinscription, intéressé, question hors
profil, réponse automatique, adresse invalide, simulation, persistance), l'audit, la fusion des
sources et le programme `recherche.py` complet. Ces scripts de test n'ont pas été ajoutés au dépôt.

**Jamais exécuté contre les vrais services** (l'environnement de développement bloquait le réseau) :
- `geo.api.gouv.fr`, `recherche-entreprises.api.gouv.fr/near_point` : le code suit la documentation,
  mais les noms de champs de la réponse (`results`, `matching_etablissements`, `siege`,
  `liste_enseignes`, `nom_commercial`, `dirigeants`, `tranche_effectif_salarie`, `latitude`…) n'ont pas
  été confrontés à une vraie réponse. **Premier point à vérifier** : lancer
  `python recherche.py plombier Versailles` et, si le résultat est vide ou incohérent, afficher une
  réponse brute de l'API et ajuster `sources.entreprises_autour`.
- Overpass/OpenStreetMap, l'option `--web`, et l'envoi/lecture sur une vraie boîte mail (SMTP/IMAP).

## Pistes suivantes

- Confronter `sources.py` aux vraies réponses des API (voir ci-dessus).
- Ajouter les tests au dépôt (pytest) en reprenant les scénarios décrits plus haut.
- Laisser l'utilisateur ajuster les poids du score et les seuils dans un fichier de configuration.
- Export des prospects « à appeler » (sans e-mail) sous forme de fiche d'appel.
- Détecter les sites « PagesJaunes/Solocal » et les prospects avec note Google élevée mais sans site
  (arguments commerciaux forts).
- Éventuellement : copie des e-mails envoyés dans le dossier « Envoyés » via IMAP `APPEND`.
