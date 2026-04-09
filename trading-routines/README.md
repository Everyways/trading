# trading-routines

Briefings automatisés pour le projet de trading bot de Benoît. Chaque cadence (hebdomadaire, mensuelle, trimestrielle) déclenche une recherche web via l'API Anthropic (Claude Sonnet 4.6 avec le tool `web_search`), génère un rapport markdown et envoie un résumé sur Telegram.

## Ce que ça fait

| Cadence | Déclenchement | Objectif |
|---|---|---|
| **Hebdomadaire** | Lundi 08:00 Europe/Paris | Statut Alpaca, incidents, SDK updates, régime marché court terme |
| **Mensuelle** | 1er du mois 09:00 | Features brokers, paysage concurrentiel, fiscalité FR, ecosystem Python |
| **Trimestrielle** | 1er janv/avril/juillet/oct 10:00 | Audit profond, recommandation migration broker, revue stratégies, rotation clés API |

Chaque routine :
1. Lit son prompt depuis `prompts/<routine>.md`
2. Appelle l'API Anthropic avec `web_search` activé
3. Sauvegarde le rapport dans `reports/YYYY-MM-DD_HHMM_<routine>.md`
4. Envoie un résumé sur Telegram (si configuré)

## Coût estimé

- Hebdo : ~$0.03–0.05 par run × 52 = **~$2/an**
- Mensuel : ~$0.05–0.10 par run × 12 = **~$1/an**
- Trimestriel : ~$0.10–0.20 par run × 4 = **~$0.6/an**
- **Total : ~$3–5/an**

## Installation

### Option 1 — Local (dev/test)

```bash
git clone <repo> trading-routines
cd trading-routines

# Python 3.12+ requis
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Édite .env : mets ta clé ANTHROPIC_API_KEY (et Telegram si tu veux)

# Test immédiat (manuel)
python trading_routines.py list
python trading_routines.py run weekly

# Ou lance le scheduler en foreground
python trading_routines.py schedule
```

### Option 2 — Docker sur Raspberry Pi (recommandé prod)

```bash
cp .env.example .env
# Édite .env

docker compose up -d --build
docker compose logs -f trading-routines
```

Le scheduler tourne en permanence, déclenche automatiquement les routines selon les cron expressions de `config.yaml`.

### Option 3 — systemd (sans Docker)

```ini
# /etc/systemd/system/trading-routines.service
[Unit]
Description=Trading Routines Scheduler
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/pi/trading-routines
ExecStart=/home/pi/trading-routines/.venv/bin/python trading_routines.py schedule
Restart=on-failure
RestartSec=30
User=pi
EnvironmentFile=/home/pi/trading-routines/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now trading-routines
```

## Utilisation

### Commandes CLI

```bash
# Lister les routines configurées
python trading_routines.py list

# Déclencher une routine manuellement (utile pour tester)
python trading_routines.py run weekly
python trading_routines.py run monthly
python trading_routines.py run quarterly

# Lancer le scheduler (bloque, à lancer dans tmux/systemd/docker)
python trading_routines.py schedule
```

### Déclencher manuellement via Docker

```bash
docker compose run --rm trading-routines python trading_routines.py run weekly
```

## Structure

```
trading-routines/
├── trading_routines.py   # Tout le code (briefer + telegram + scheduler + CLI)
├── config.yaml           # Routines : noms, cron, prompt files
├── prompts/
│   ├── weekly.md         # Prompt du briefing hebdo
│   ├── monthly.md        # Prompt du briefing mensuel
│   └── quarterly.md      # Prompt du briefing trimestriel
├── reports/              # Rapports générés (gitignored)
├── .env.example          # Template de config
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

## Personnalisation

### Éditer les prompts

Les prompts sont des fichiers markdown simples dans `prompts/`. Deux placeholders sont remplacés à l'exécution :

- `{{TODAY}}` → date du jour (YYYY-MM-DD)
- `{{ROUTINE}}` → nom de la routine (weekly/monthly/quarterly)

Modifier un prompt ne nécessite **aucun redémarrage** du scheduler si tu utilises Docker avec le volume monté en lecture seule : la prochaine exécution lira la nouvelle version.

### Ajouter une routine

1. Crée un nouveau fichier dans `prompts/` (ex: `daily.md`)
2. Ajoute une entrée dans `config.yaml` :

```yaml
- name: daily
  enabled: true
  cron: "0 7 * * *"          # Tous les jours à 07h
  prompt_file: daily.md
  telegram_summary: true
```

3. Redémarre le scheduler.

### Modifier les cron expressions

Édite `config.yaml`. Les expressions suivent le format standard 5-fields Linux cron. Exemples utiles :

```yaml
cron: "0 8 * * 1"        # Lundi 08:00
cron: "0 9 1 * *"        # 1er du mois 09:00
cron: "0 10 1 1,4,7,10 *" # 1er jan/avr/jul/oct à 10:00
cron: "*/15 * * * *"     # Toutes les 15 min (test uniquement !)
```

## Monitoring

### Logs

```bash
# Docker
docker compose logs -f trading-routines

# systemd
journalctl -u trading-routines -f
```

### Rapports

Les rapports sont dans `reports/` nommés `YYYY-MM-DD_HHMM_<routine>.md`. Ouvrables dans n'importe quel éditeur markdown (VS Code, Obsidian, Typora).

Suggestion : synchronise ce dossier avec Syncthing / iCloud / Google Drive pour y accéder depuis ton mobile.

### Alertes

Si Telegram est configuré, chaque exécution (réussie ou échouée) envoie une notification avec :
- Résumé des 20 premières lignes du rapport
- Nom du fichier sauvegardé
- En cas d'échec : le message d'erreur

## Sécurité

- **Secrets** : clés API Anthropic et Telegram uniquement dans `.env`, jamais en git. `.env` est gitignored par défaut, vérifie-le.
- **Clé Anthropic** : permissions minimales, aucun privilège de billing requis.
- **Rate limiting** : le scheduler déclenche au maximum 1 routine à la fois, pas de risque de dépassement quota.
- **Rotation** : rotation trimestrielle de la clé API Anthropic recommandée (aligné sur la politique du trading bot §22.6).

## Troubleshooting

### "ANTHROPIC_API_KEY field required"
Tu n'as pas rempli `.env` ou il n'est pas chargé. Vérifie avec `cat .env`.

### "Prompt file not found"
Les prompts sont dans `prompts/` par défaut. Vérifie `PROMPTS_DIR` dans `.env`.

### Les cron ne se déclenchent pas
- Vérifie que le conteneur tourne : `docker compose ps`
- Vérifie les logs : `docker compose logs trading-routines`
- Teste une routine manuellement pour isoler le problème

### Coût plus élevé que prévu
Regarde les logs : chaque run affiche le `token_usage`. Si c'est anormalement haut, c'est probablement que le prompt a été modifié et que Claude génère plus que prévu. Réduis `MAX_TOKENS` ou `MAX_WEB_SEARCHES` dans `.env`.

## Philosophie

Ce projet est conçu pour rester **petit, lisible et modifiable**. Un seul fichier Python, trois fichiers prompt, un config YAML. Pas de framework, pas de DB, pas de microservices. Si tu veux ajouter une feature, commence par te demander si elle mérite plus que 20 lignes.

La règle d'or : **ce n'est pas un produit, c'est un outil personnel.** Reste radin sur la complexité.
