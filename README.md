# Trading Bot

Bot de trading automatisé multi-stratégies en Python. Exécute RSI Mean Reversion, MA Crossover et Breakout en parallèle via Alpaca, avec dashboards de monitoring, risk management strict et kill switches.

---

## Prérequis matériel (Raspberry Pi — cible recommandée)

| Composant | Minimum | Recommandé |
|-----------|---------|------------|
| Modèle | Raspberry Pi 4 — 4 Go RAM | Raspberry Pi 5 — 8 Go RAM |
| Stockage | SSD USB3 64 Go | SSD USB3 256 Go |
| Alimentation | Officielle Pi | Officielle Pi + **UPS** (onduleur) |
| Réseau | Wifi | **Ethernet filaire** |
| OS | Raspberry Pi OS Lite 64-bit | Ubuntu Server 24.04 arm64 |

> ⚠️ **Ne pas utiliser de carte SD pour PostgreSQL** — elle mourra en quelques semaines. SSD USB3 obligatoire.

> ⚠️ **Le live trading sur laptop est refusé par défaut** (le bot refuse de démarrer). Le Raspberry Pi est la seule cible recommandée pour le live.

---

## Installation sur Raspberry Pi

### 1. Préparer le Raspberry Pi

```bash
# Mettre à jour le système
sudo apt update && sudo apt upgrade -y

# Installer Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# Vérifier
docker --version
docker compose version
```

### 2. Connecter et monter le SSD

```bash
# Identifier le SSD (chercher /dev/sda ou /dev/sdb)
lsblk

# Formater si neuf (⚠️ efface tout)
sudo mkfs.ext4 /dev/sda1

# Créer le point de montage et monter
sudo mkdir -p /var/lib/trading-bot
sudo mount /dev/sda1 /var/lib/trading-bot

# Montage automatique au boot
echo '/dev/sda1 /var/lib/trading-bot ext4 defaults,noatime 0 2' | sudo tee -a /etc/fstab
```

### 3. Cloner le projet

```bash
git clone <url-du-repo> /home/pi/trading-bot
cd /home/pi/trading-bot
```

### 4. Configurer l'environnement

```bash
# Créer le dossier de config sécurisé
sudo mkdir -p /etc/trading-bot
sudo chmod 700 /etc/trading-bot

# Copier et éditer la config
sudo cp .env.example /etc/trading-bot/trading.env
sudo nano /etc/trading-bot/trading.env
```

Valeurs minimales à renseigner dans `/etc/trading-bot/trading.env` :

```bash
# Clés Alpaca (paper pour commencer)
ALPACA_API_KEY=PKxxxxxxxxxxxxxxxx
ALPACA_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# Base de données (adaptée au compose prod)
DATABASE_URL=postgresql+asyncpg://trading:MOT_DE_PASSE_ICI@db:5432/trading
DATABASE_URL_SYNC=postgresql+psycopg2://trading:MOT_DE_PASSE_ICI@db:5432/trading

# Sécurité dashboard
SECRET_KEY=chaine-aleatoire-de-32-caracteres-minimum
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=votre-mot-de-passe-dashboard
DB_PASSWORD=MOT_DE_PASSE_ICI

# Telegram (optionnel mais recommandé)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID_GLOBAL=
```

### 5. Créer les dossiers de données

```bash
sudo mkdir -p /var/lib/trading-bot/{postgres,data,reports}
sudo chown -R 1000:1000 /var/lib/trading-bot/data /var/lib/trading-bot/reports
sudo chown -R 999:999 /var/lib/trading-bot/postgres   # uid postgres dans Docker
```

### 6. Lancer les services

```bash
cd /home/pi/trading-bot

# Premier démarrage (construit l'image)
docker compose -f docker/docker-compose.prod.yml up -d

# Vérifier que la DB est prête
docker compose -f docker/docker-compose.prod.yml logs db | tail -5

# Appliquer les migrations
docker compose -f docker/docker-compose.prod.yml exec bot alembic upgrade head

# Vérifier les logs du bot
docker compose -f docker/docker-compose.prod.yml logs bot -f
```

### 7. Remplir les données historiques

```bash
# Backfill 2 ans de données 15m pour toutes les stratégies
docker compose -f docker/docker-compose.prod.yml exec bot \
  python scripts/backfill_data.py \
  --provider alpaca \
  --symbols SPY,QQQ,IWM,AAPL,MSFT,TSLA,NVDA,COIN \
  --timeframe 15m --years 2
```

### 8. Configurer le service systemd (démarrage automatique)

```bash
sudo tee /etc/systemd/system/trading-bot.service << 'EOF'
[Unit]
Description=Trading Bot
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/home/pi/trading-bot
ExecStart=/usr/bin/docker compose -f docker/docker-compose.prod.yml up -d
ExecStop=/usr/bin/docker compose -f docker/docker-compose.prod.yml down
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable trading-bot.service
sudo systemctl start trading-bot.service

# Vérifier
sudo systemctl status trading-bot.service
```

---

## Accès au dashboard

### Sur le réseau local (LAN)

```
http://<ip-du-raspy>:8000
```

Trouver l'IP du Raspberry Pi : `hostname -I`

### Depuis n'importe où (Tailscale — recommandé)

Tailscale crée un VPN chiffré entre vos appareils, sans exposition publique.

```bash
# Sur le Raspberry Pi
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# Récupérer l'IP Tailscale
tailscale ip -4
```

Le dashboard devient accessible depuis votre téléphone ou ordinateur via `http://<ip-tailscale>:8000`, chiffré de bout en bout.

---

## Mise à jour

```bash
cd /home/pi/trading-bot
git pull origin main

# Rebuild et redémarrage
docker compose -f docker/docker-compose.prod.yml build bot
docker compose -f docker/docker-compose.prod.yml up -d

# Appliquer les nouvelles migrations si besoin
docker compose -f docker/docker-compose.prod.yml exec bot alembic upgrade head
```

---

## Commandes courantes

```bash
# Voir les logs en direct
docker compose -f docker/docker-compose.prod.yml logs bot -f

# Logs d'une stratégie spécifique
docker compose -f docker/docker-compose.prod.yml logs bot -f | grep rsi_mean_reversion

# Statut des services
docker compose -f docker/docker-compose.prod.yml ps

# Redémarrer le bot (sans toucher la DB)
docker compose -f docker/docker-compose.prod.yml restart bot

# Arrêt propre
docker compose -f docker/docker-compose.prod.yml down

# Shell dans le conteneur
docker compose -f docker/docker-compose.prod.yml exec bot bash
```

---

## Kill switches (urgence)

```bash
# Couper TOUTES les stratégies (kill global)
docker compose -f docker/docker-compose.prod.yml exec bot \
  python scripts/killswitch.py --scope global --engage --reason "intervention manuelle"

# Couper UNE stratégie
docker compose -f docker/docker-compose.prod.yml exec bot \
  python scripts/killswitch.py --scope strategy --name rsi_mean_reversion --engage

# Relâcher (le bot reprend)
docker compose -f docker/docker-compose.prod.yml exec bot \
  python scripts/killswitch.py --scope global --release
```

> ⚠️ Le kill switch **ne liquide pas** les positions ouvertes. Il stoppe uniquement les nouveaux ordres. Gérer manuellement via l'app Alpaca si besoin.

---

## Backups

```bash
# Backup manuel de la DB
docker compose -f docker/docker-compose.prod.yml exec db \
  pg_dump -U trading trading | gzip > /var/lib/trading-bot/backup_$(date +%Y%m%d).sql.gz

# Planifier un backup automatique quotidien (cron)
(crontab -l 2>/dev/null; echo "0 3 * * * docker compose -f /home/pi/trading-bot/docker/docker-compose.prod.yml exec -T db pg_dump -U trading trading | gzip > /var/lib/trading-bot/backup_\$(date +\%Y\%m\%d).sql.gz") | crontab -
```

---

## Passage en live (par stratégie)

> **Lire §12 de la spec avant toute chose.** Le live ne s'active jamais d'un coup sur les 3 stratégies.

1. Vérifier que la gate est passée pour la stratégie :
   ```bash
   docker compose -f docker/docker-compose.prod.yml exec bot \
     python scripts/check_gate.py --strategy rsi_mean_reversion
   ```

2. Éditer `config/strategies/rsi_mean_reversion.yaml` : changer `mode: paper` → `mode: live`

3. Commiter le changement :
   ```bash
   git add config/strategies/rsi_mean_reversion.yaml
   git commit -m "rsi_mean_reversion: switch to live"
   git push
   ```

4. Définir la variable d'approbation et redémarrer :
   ```bash
   echo 'TRADING_BOT_LIVE_APPROVAL_RSI_MEAN_REVERSION=yes' | sudo tee -a /etc/trading-bot/trading.env
   docker compose -f docker/docker-compose.prod.yml restart bot
   ```

---

## 12 règles non-négociables

1. **Isolation broker** : aucun code métier ne connaît Alpaca directement
2. **Idempotence** : `client_order_id` généré avant tout appel broker
3. **Source de vérité positions** : le broker, jamais la DB locale
4. **Pas de liquidation automatique** sur kill switch
5. **Stop-loss côté broker** en live (bracket orders) pour survivre aux crashs Raspberry Pi
6. **Gate paper→live par stratégie** — bypass explicitement loggué
7. **Hard stop mensuel 50 €** non-relâchable avant le 1er du mois suivant
8. **Live interdit sur laptop** sauf `ALLOW_LIVE_ON_LAPTOP=true` explicite
9. **PDT compliance obligatoire** en live (overnight hold forcé)
10. **Déploiement live progressif** : 1 stratégie à la fois, jamais 3 simultanément
11. **`Decimal` pour tout ce qui touche à l'argent** — jamais `float`
12. **Pas de `print`, `float`, `time.sleep` async** en production

---

## Ajouter un broker (contrat)

1. Créer `app/providers/<nom>/provider.py` avec `@broker_registry.register("<nom>")`
2. Créer `app/providers/<nom>/config.py` avec les settings
3. L'importer dans `app/providers/__init__.py`
4. Référencer `provider: <nom>` dans un YAML de stratégie

**Aucun autre fichier n'est modifié.** Voir `tests/fixtures/dummy_provider/` pour un exemple minimal.

## Ajouter une stratégie

1. Créer `app/strategies/<nom>.py` avec `@strategy_registry.register("<nom>")`
2. Créer `config/strategies/<nom>.yaml`
3. L'importer dans `app/strategies/__init__.py`

---

## Développement local

```bash
# Lancer uniquement la DB (paper + backtest uniquement)
docker compose -f docker/docker-compose.yml up -d db

# Installer les dépendances Python
pip install -e ".[dev]"

# Migrations
alembic upgrade head

# Tests
pytest tests/unit/ -v

# Linter + types
ruff check app/ tests/
mypy app/ --strict
```
