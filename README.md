# Trading Bot

Bot de trading automatisé pour Raspberry Pi — strategies RSI, MA Crossover, Breakout via Alpaca, avec risk management, dashboard web, backtest et notifications Telegram.

> Ce repo contient deux projets indépendants :
> | Projet | Rôle |
> |--------|------|
> | **trading-bot** (racine) | Bot de trading — ce fichier |
> | **trading-routines/** | Veille automatisée — briefings via Claude + Telegram |

---

## Table des matières

1. [Obtenir les clés API](#1-obtenir-les-clés-api)
2. [Prérequis matériel](#2-prérequis-matériel)
3. [Installation sur Raspberry Pi](#3-installation-sur-raspberry-pi)
4. [Configuration](#4-configuration)
5. [Utilisation](#5-utilisation)
6. [Dashboard web](#6-dashboard-web)
7. [Backtest](#7-backtest)
8. [Production avec Docker](#8-production-avec-docker)
9. [Passer en live](#9-passer-en-live)
10. [Kill switch d'urgence](#10-kill-switch-durgence)
11. [Développement local](#11-développement-local)
12. [Règles non-négociables](#12-règles-non-négociables)

---

## 1. Obtenir les clés API

### Alpaca (broker — obligatoire)

1. Créer un compte sur **[alpaca.markets](https://alpaca.markets)**
2. Aller dans **Paper Trading** → **Overview** → **API Keys** → **Generate New Key**
3. Copier **Key ID** (`ALPACA_API_KEY`) et **Secret Key** (`ALPACA_API_SECRET`)
   > La Secret Key n'est affichée qu'une seule fois — la copier immédiatement.
4. L'URL paper trading est `https://paper-api.alpaca.markets` (déjà pré-renseignée)

Pour le live trading (plus tard) : aller dans **Live Trading** → **API Keys** et changer `ALPACA_BASE_URL`.

### Telegram (notifications — recommandé pour Raspberry Pi headless)

**Créer le bot :**
1. Ouvrir Telegram → chercher **@BotFather** → `/newbot`
2. Choisir un nom (ex: `MonTradingBot`) et un username (ex: `mon_trading_bot`)
3. Copier le **token** affiché → `TELEGRAM_BOT_TOKEN`

**Obtenir votre Chat ID :**
1. Chercher **@userinfobot** dans Telegram → `/start`
2. Copier l'**Id** affiché → `TELEGRAM_CHAT_ID_GLOBAL`
3. Envoyer un message à votre bot (sinon il ne peut pas vous contacter)

### Anthropic (trading-routines uniquement — optionnel)

1. Créer un compte sur **[console.anthropic.com](https://console.anthropic.com)**
2. **API Keys** → **Create Key** → copier → `ANTHROPIC_API_KEY` dans `trading-routines/.env`

---

## 2. Prérequis matériel

| Composant | Minimum | Recommandé |
|-----------|---------|------------|
| Modèle | Raspberry Pi 4 — 4 Go RAM | Raspberry Pi 5 — 8 Go RAM |
| Stockage | SSD USB3 64 Go | SSD USB3 256 Go |
| Alimentation | Officielle Pi | Officielle Pi + **UPS** (onduleur) |
| Réseau | Wifi | **Ethernet filaire** |
| OS | Raspberry Pi OS Lite 64-bit | Ubuntu Server 24.04 arm64 |

> **Ne pas utiliser de carte SD pour la base de données** — elle mourra en quelques semaines sous les writes. SSD USB3 obligatoire en production.

> **Le live trading sur laptop est bloqué par défaut.** Seul le Raspberry Pi (ou un serveur) est la cible recommandée.

---

## 3. Installation sur Raspberry Pi

### 3.1 Préparer le système

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.12 python3.12-venv python3-pip git curl
```

### 3.2 Cloner le projet

```bash
git clone <url-du-repo> /home/pi/trading
cd /home/pi/trading
```

### 3.3 Créer l'environnement Python

```bash
python3.12 -m venv .venv
source .venv/bin/activate

# Installer les dépendances
pip install -e ".[dev]"
```

> Ajouter `source /home/pi/trading/.venv/bin/activate` à votre `~/.bashrc` pour ne pas avoir à le refaire.

### 3.4 Monter le SSD (si applicable)

```bash
# Identifier le SSD
lsblk

# Formater si neuf (efface tout)
sudo mkfs.ext4 /dev/sda1

# Monter
sudo mkdir -p /var/lib/trading-bot
sudo mount /dev/sda1 /var/lib/trading-bot

# Montage automatique au boot
echo '/dev/sda1 /var/lib/trading-bot ext4 defaults,noatime 0 2' | sudo tee -a /etc/fstab

# Créer les dossiers de données
mkdir -p /var/lib/trading-bot/data
```

---

## 4. Configuration

### 4.1 Créer le fichier .env

```bash
cp .env.example .env
nano .env   # ou vim, ou votre éditeur préféré
```

### 4.2 Renseigner les valeurs

```bash
# ── Base de données ─────────────────────────────────────────────────
# Mode paper/dev : SQLite (simple, aucune installation)
DATABASE_URL=sqlite+aiosqlite:///./data/paper.db
DATABASE_URL_SYNC=sqlite:///./data/paper.db

# Mode production : PostgreSQL (voir section 8)
# DATABASE_URL=postgresql+asyncpg://trading:MOT_DE_PASSE@localhost:5432/trading
# DATABASE_URL_SYNC=postgresql+psycopg2://trading:MOT_DE_PASSE@localhost:5432/trading

# ── Sécurité ────────────────────────────────────────────────────────
# Générer une clé : python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY=remplacer-par-une-chaine-aleatoire-de-32-caracteres-minimum
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=votre-mot-de-passe-dashboard

# ── Alpaca ──────────────────────────────────────────────────────────
ALPACA_API_KEY=PKxxxxxxxxxxxxxxxx
ALPACA_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# ── Telegram (optionnel) ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=123456789:ABCdef...
TELEGRAM_CHAT_ID_GLOBAL=987654321

# ── Environnement ────────────────────────────────────────────────────
ENVIRONMENT=development
DATA_DIR=./data
CONFIG_DIR=./config
```

### 4.3 Initialiser la base de données

```bash
# SQLite (paper trading, développement)
python scripts/init_db.py --url sqlite:///./data/paper.db

# PostgreSQL (production)
python scripts/init_db.py
# ou : alembic upgrade head
```

### 4.4 Remplir les données historiques (recommandé)

```bash
# 2 ans de bars 15m pour les symboles des stratégies actives
python scripts/backfill_data.py \
  --provider alpaca \
  --symbols SPY,QQQ,IWM \
  --timeframe 15m \
  --years 2
```

---

## 5. Utilisation

### 5.1 Lancer le paper trading

```bash
# Mode continu (tourne indéfiniment, tick toutes les 15 minutes)
python scripts/run_paper.py

# Un seul tick puis quitte (test de configuration)
python scripts/run_paper.py --once

# Une seule stratégie
python scripts/run_paper.py --strategy rsi_mean_reversion

# Config alternatives
python scripts/run_paper.py --config-dir config/strategies --risk-config config/risk_global.yaml
```

Le runner :
- Vérifie les heures de marché (ne tourne pas si marché fermé)
- Évalue chaque paire stratégie/symbole à chaque bar 15m
- Gate chaque signal à travers le RiskManager (8 niveaux)
- Soumet les ordres paper à Alpaca
- Envoie les alertes Telegram (si configuré)

### 5.2 Activer le service au démarrage (systemd)

```bash
sudo tee /etc/systemd/system/trading-bot.service << 'EOF'
[Unit]
Description=Trading Bot — paper trading
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/trading
ExecStart=/home/pi/trading/.venv/bin/python scripts/run_paper.py
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable trading-bot.service
sudo systemctl start trading-bot.service

# Vérifier
sudo systemctl status trading-bot.service
journalctl -u trading-bot.service -f
```

---

## 6. Dashboard web

```bash
# Démarrer le dashboard
python scripts/run_dashboard.py                    # http://0.0.0.0:8080
python scripts/run_dashboard.py --port 8090        # port alternatif
python scripts/run_dashboard.py --host 127.0.0.1  # localhost uniquement
```

Accéder via navigateur : `http://<ip-du-raspy>:8080`

Identifiants : `DASHBOARD_USER` / `DASHBOARD_PASSWORD` depuis `.env`

**Ce qu'affiche le dashboard :**
- Statut du kill switch (alerte rouge si engagé)
- PnL du jour, perte mensuelle €, positions ouvertes, stratégies actives
- Tableau des positions ouvertes avec PnL non réalisé
- Tableau des stratégies (enabled/disabled, paper/live)
- Historique des 30 derniers trades
- Historique des 20 derniers ordres
- Derniers events de risque
- Auto-refresh toutes les 30 secondes

**API JSON :** `GET http://<ip>:8080/api/status` — utile pour Uptime Kuma ou scripts de monitoring.

### Accès depuis n'importe où (Tailscale — recommandé)

```bash
# Sur le Raspberry Pi
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# Récupérer l'IP Tailscale
tailscale ip -4
```

Dashboard accessible depuis votre téléphone : `http://<ip-tailscale>:8080`

### Service systemd pour le dashboard

```bash
sudo tee /etc/systemd/system/trading-dashboard.service << 'EOF'
[Unit]
Description=Trading Bot — Dashboard
After=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/trading
ExecStart=/home/pi/trading/.venv/bin/python scripts/run_dashboard.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable trading-dashboard.service
sudo systemctl start trading-dashboard.service
```

---

## 7. Backtest

```bash
# Backtest d'une stratégie sur une période historique
python scripts/run_backtest.py \
  --strategy rsi_mean_reversion \
  --symbol SPY \
  --start 2024-01-01 \
  --end   2024-12-31

# Avec equity de départ personnalisée
python scripts/run_backtest.py \
  --strategy ma_crossover \
  --symbol QQQ \
  --start 2023-01-01 \
  --end   2024-12-31 \
  --equity 5000

# Exporter les trades en CSV
python scripts/run_backtest.py \
  --strategy breakout \
  --symbol IWM \
  --start 2024-01-01 \
  --end   2024-12-31 \
  --output results/backtest_breakout_iwm.csv
```

**Résultat affiché :**
```
Trades: 47  Win rate: 55.3%  Net PnL: $312.40  Return: 3.12%  Max DD: -4.21%  Sharpe: 1.43  PF: 1.62
```

### Modifier les paramètres d'une stratégie

Éditer `config/strategies/<nom>.yaml` :

```yaml
params:
  rsi_period: 14      # période RSI
  oversold: 30        # seuil de survente (entrée)
  overbought: 70      # seuil de surachat (sortie)
  stop_loss_pct: 2.0  # stop-loss en %
  take_profit_pct: 4.0
  max_holding_bars: 96  # 96 × 15m = 24h max
```

---

## 8. Production avec Docker

Pour un déploiement robuste (PostgreSQL + redémarrage automatique) :

### 8.1 Installer Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
```

### 8.2 Préparer les secrets

```bash
# Dossier sécurisé pour les secrets
sudo mkdir -p /etc/trading-bot
sudo chmod 700 /etc/trading-bot
sudo cp .env.example /etc/trading-bot/trading.env
sudo nano /etc/trading-bot/trading.env
# → renseigner les valeurs avec DATABASE_URL postgresql://...
```

### 8.3 Préparer les volumes

```bash
sudo mkdir -p /var/lib/trading-bot/{postgres,data,reports}
sudo chown -R 1000:1000 /var/lib/trading-bot/data /var/lib/trading-bot/reports
sudo chown -R 999:999 /var/lib/trading-bot/postgres
```

### 8.4 Lancer la stack

```bash
cd /home/pi/trading

# Démarrer (build + launch)
DB_PASSWORD=MOT_DE_PASSE_ICI docker compose -f docker-compose.prod.yml up -d

# Initialiser la base de données
docker compose -f docker-compose.prod.yml exec bot \
  python scripts/init_db.py

# Vérifier les logs
docker compose -f docker-compose.prod.yml logs bot -f
docker compose -f docker-compose.prod.yml logs db -f
```

### 8.5 Commandes courantes (Docker)

```bash
# Statut des conteneurs
docker compose -f docker-compose.prod.yml ps

# Logs d'un service
docker compose -f docker-compose.prod.yml logs bot -f
docker compose -f docker-compose.prod.yml logs bot -f | grep rsi_mean_reversion

# Redémarrer le bot sans toucher la DB
docker compose -f docker-compose.prod.yml restart bot

# Shell dans le conteneur
docker compose -f docker-compose.prod.yml exec bot bash

# Arrêt propre
docker compose -f docker-compose.prod.yml down

# Mise à jour (pull + rebuild)
git pull origin main
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
```

### 8.6 Backup de la base de données

```bash
# Backup manuel
docker compose -f docker-compose.prod.yml exec db \
  pg_dump -U trading trading | gzip > /var/lib/trading-bot/backup_$(date +%Y%m%d).sql.gz

# Backup quotidien automatique (cron)
(crontab -l 2>/dev/null; echo "0 3 * * * docker compose -f /home/pi/trading/docker-compose.prod.yml exec -T db pg_dump -U trading trading | gzip > /var/lib/trading-bot/backup_\$(date +\%Y\%m\%d).sql.gz") | crontab -
```

---

## 9. Passer en live

> Lire attentivement toutes les règles de la section 12 avant de procéder.

**Étape 1 — Vérifier que le paper trading est stable**

Une stratégie doit avoir au moins 30 jours de paper trading sans anomalie avant de passer en live.

**Étape 2 — Modifier le YAML de la stratégie**

```yaml
# config/strategies/rsi_mean_reversion.yaml
mode: live   # était: paper
```

**Étape 3 — Changer les clés Alpaca**

Dans `.env` :
```bash
ALPACA_API_KEY=votre-cle-live
ALPACA_API_SECRET=votre-secret-live
ALPACA_BASE_URL=https://api.alpaca.markets
```

**Étape 4 — Définir la variable d'approbation**

```bash
# Dans .env — une variable par stratégie live
TRADING_BOT_LIVE_APPROVAL_RSI_MEAN_REVERSION=yes
```

**Étape 5 — Redémarrer**

```bash
sudo systemctl restart trading-bot.service
# ou Docker :
docker compose -f docker-compose.prod.yml restart bot
```

> **Le kill switch ne liquide pas les positions ouvertes.** Si besoin de clôturer d'urgence, utiliser directement l'app Alpaca.

---

## 10. Kill switch d'urgence

Le kill switch stoppe tous les nouveaux ordres immédiatement (sans liquider les positions existantes).

### Via la base de données (SQLite)

```bash
# Engager le kill switch global
python - << 'EOF'
from app.data.database import get_session
from app.data.models import KillSwitch
from datetime import UTC, datetime
from sqlmodel import select

with get_session() as session:
    ks = session.exec(select(KillSwitch).where(KillSwitch.scope == "global")).first()
    if ks:
        ks.engaged = True
        ks.engaged_at = datetime.now(tz=UTC)
        ks.engaged_by = "operator"
        ks.reason = "intervention manuelle"
        session.add(ks)
    else:
        session.add(KillSwitch(
            scope="global", engaged=True,
            engaged_at=datetime.now(tz=UTC),
            engaged_by="operator", reason="intervention manuelle",
        ))
    session.commit()
    print("Kill switch engagé.")
EOF

# Redémarrer le bot pour que le changement soit pris en compte
sudo systemctl restart trading-bot.service
```

```bash
# Relâcher le kill switch
python - << 'EOF'
from app.data.database import get_session
from app.data.models import KillSwitch
from sqlmodel import select

with get_session() as session:
    ks = session.exec(select(KillSwitch).where(KillSwitch.scope == "global")).first()
    if ks:
        ks.engaged = False
        session.add(ks)
        session.commit()
        print("Kill switch relâché.")
EOF
```

### Via Docker (PostgreSQL)

```bash
# Engager
docker compose -f docker-compose.prod.yml exec db \
  psql -U trading -c "UPDATE kill_switches SET engaged=true, engaged_by='operator', reason='intervention manuelle' WHERE scope='global';"

# Relâcher
docker compose -f docker-compose.prod.yml exec db \
  psql -U trading -c "UPDATE kill_switches SET engaged=false WHERE scope='global';"

# Redémarrer le bot
docker compose -f docker-compose.prod.yml restart bot
```

---

## 11. Développement local

```bash
# Cloner et installer
git clone <url> trading && cd trading
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Configurer (SQLite pour le dev)
cp .env.example .env
# Éditer .env → DATABASE_URL_SYNC=sqlite:///./data/dev.db
python scripts/init_db.py --url sqlite:///./data/dev.db

# Tests
pytest tests/unit/ -v
pytest tests/unit/ -q --tb=short --cov=app

# Linter
ruff check app/ scripts/ tests/
ruff format app/ scripts/ tests/

# Type checking
mypy app/ --strict

# Lancer un seul tick (test rapide sans attendre 15m)
python scripts/run_paper.py --once --strategy rsi_mean_reversion
```

### Ajouter une stratégie

1. Créer `app/strategies/<nom>.py` avec `@strategy_registry.register("<nom>")`
2. Créer `config/strategies/<nom>.yaml`
3. Importer dans `app/strategies/__init__.py`

### Ajouter un broker

1. Créer `app/providers/<nom>/provider.py` avec `@broker_registry.register("<nom>")`
2. Créer `app/providers/<nom>/config.py`
3. Importer dans `app/providers/__init__.py`

Voir `tests/fixtures/dummy_provider/` pour un exemple minimal.

---

## 12. Règles non-négociables

1. **Isolation broker** — aucun code métier ne connaît Alpaca directement
2. **Idempotence** — `client_order_id` généré avant tout appel broker
3. **Source de vérité positions** — le broker, jamais la DB locale
4. **Pas de liquidation automatique** sur kill switch
5. **Stop-loss côté broker** en live (bracket orders) pour survivre aux crashs Pi
6. **Gate paper→live par stratégie** — bypass explicitement loggué
7. **Hard stop mensuel 50 €** — non-relâchable avant le 1er du mois suivant
8. **Live interdit sur laptop** sauf `ALLOW_LIVE_ON_LAPTOP=true` explicite
9. **PDT compliance obligatoire** en live (max 3 day-trades sur 5 jours glissants)
10. **Déploiement live progressif** — 1 stratégie à la fois, jamais 3 simultanément
11. **`Decimal` pour tout ce qui touche à l'argent** — jamais `float`
12. **Pas de `print`, `float` monétaire, `time.sleep` async** en production
