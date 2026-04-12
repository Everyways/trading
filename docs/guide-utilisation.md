# Guide d'utilisation — Trading Bot

Ce document explique le fonctionnement interne du bot, les décisions qu'il prend
à chaque cycle, et comment interpréter ce qu'il affiche. Il est complémentaire au
`README.md` (installation) et au `docs/glossaire.md` (jargon).

---

## Table des matières

1. [Vue d'ensemble](#1-vue-densemble)
2. [Le cycle de trading (tick)](#2-le-cycle-de-trading-tick)
3. [Les stratégies disponibles](#3-les-stratégies-disponibles)
4. [Le Risk Manager — 8 niveaux de protection](#4-le-risk-manager--8-niveaux-de-protection)
5. [Le sizing des positions](#5-le-sizing-des-positions)
6. [Les ordres bracket](#6-les-ordres-bracket)
7. [Le kill switch](#7-le-kill-switch)
8. [Dashboard — lire les données](#8-dashboard--lire-les-données)
9. [Notifications Telegram](#9-notifications-telegram)
10. [Modifier les paramètres d'une stratégie](#10-modifier-les-paramètres-dune-stratégie)
11. [Créer une nouvelle stratégie](#11-créer-une-nouvelle-stratégie)
12. [FAQ opérationnelle](#12-faq-opérationnelle)
13. [Optimisation des paramètres par Walk-Forward](#13-optimisation-des-paramètres-par-walk-forward)

---

## 1. Vue d'ensemble

```
Marché (Alpaca)
      │  candles OHLCV
      ▼
 TradingRunner  ─────────────────────────────────────────────────────────
      │                                                                   │
      │  1. Heure marché OK ?                                             │
      │  2. Récupère les bougies historiques (REST)                       │
      │  3. Appelle strategy.generate_signal(df)                          │
      │  4. Passe le signal dans RiskManager.check_order()                │
      │  5. Taille la position (position sizer)                           │
      │  6. Soumet l'ordre market (ou bracket) à Alpaca                   │
      │  7. Écrit un PositionSnapshot en base                             │
      └──────────────────────────────────────────────────────────────────
```

Le bot tourne en **boucle permanente** avec un tick toutes les **15 minutes**
(à 0h, 15m, 30m, 45m). Entre deux ticks il ne fait rien — aucune surveillance
de prix en temps réel, aucun trailing stop actif côté bot.

En dehors des heures de marché (9h30–16h00 ET, lundi–vendredi), les ticks
passent la vérification d'heure et s'arrêtent immédiatement sans évaluer
les stratégies.

---

## 2. Le cycle de trading (tick)

À chaque tick (toutes les 15 minutes), le runner exécute les étapes suivantes
**pour chaque paire stratégie + symbole** :

### Étape 1 — Vérification marché

```python
await provider.healthcheck()  # appelle l'endpoint clock d'Alpaca
```

Si le marché est fermé (week-end, jours fériés US, pre/post-market), le tick
est sauté en entier.

### Étape 2 — Récupération des bougies

Le runner demande les `lookback × timeframe × 2` dernières minutes de bougies
(le facteur ×2 sert de tampon pour les jours sans volume ou les gaps
week-end). Seules les bougies **clôturées** (`is_closed=True`) sont utilisées —
jamais la bougie en cours de formation.

### Étape 3 — Génération du signal

La stratégie reçoit un DataFrame pandas `(time, open, high, low, close, volume)`
trié par date croissante, et un objet `StrategyContext` contenant :

| Champ | Contenu |
|-------|---------|
| `params` | Paramètres du YAML (rsi_period, etc.) |
| `instrument` | Symbole + classe d'actif |
| `current_position` | Position ouverte ou `None` |
| `account_equity` | Solde du compte Alpaca |
| `current_time` | Heure UTC du tick |

La stratégie retourne un `Signal(side=BUY/SELL/CLOSE)` ou `None` (pas de signal).

### Étape 4 — Gate Risk Manager

Avant de soumettre le moindre ordre, le signal passe par 8 vérifications
(voir section 4). Si une vérification échoue, l'ordre est bloqué et la raison
est loggée.

### Étape 5 — Sizing

Si le signal est BUY, la quantité d'actions est calculée de façon à ne risquer
qu'un pourcentage fixé de l'equity (voir section 5).

### Étape 6 — Soumission de l'ordre

Un `OrderRequest` (market, bracket ou autre) est envoyé à Alpaca via REST.
Le `client_order_id` est un UUID généré avant l'appel, ce qui rend chaque
soumission idempotente (une panne pendant l'appel ne double pas l'ordre).

### Étape 7 — Snapshot des positions

Après le gather de toutes les paires, le runner écrit un enregistrement
`PositionSnapshot` en base pour chaque position non-plate détectée chez le
broker. C'est ce qui alimente le dashboard.

---

## 3. Les stratégies disponibles

Six stratégies sont disponibles — trois historiques et trois nouvelles.
Elles peuvent toutes tourner simultanément (univers partiellement différents).

| Stratégie | Approche | Marchés idéaux | Timeframe |
|-----------|----------|----------------|-----------|
| `rsi_mean_reversion` | Oscillateur | Range, ETF liquides | 15m |
| `ma_crossover` | Tendance EMA | Directionnel, tech | 15m |
| `breakout` | Momentum + volume | Volatil, crypto/tech | 15m |
| `bollinger_bands` | Mean reversion BB | Range, ETF liquides | 15m |
| `macd_crossover` | Tendance MACD | Directionnel, tech | 15m |
| `adx_ema_trend` | Tendance filtrée ADX | Tendance forte uniquement | 15m |

---

### RSI Mean Reversion (`rsi_mean_reversion`)

**Logique :** Acheter quand le RSI(14) passe sous 30 (zone de survente) et que
le prix est au-dessus de la MA200 (filtre de tendance haussière). Vendre quand
le RSI dépasse 70 (zone de surachat) ou quand le nombre de bougies détenu
dépasse `max_holding_bars`.

**Paramètres clés :**

| Paramètre | Défaut | Description |
|-----------|--------|-------------|
| `rsi_period` | 14 | Nombre de bougies pour calculer le RSI |
| `oversold` | 30 | Seuil d'entrée (achat si RSI < seuil) |
| `overbought` | 70 | Seuil de sortie (vente si RSI > seuil) |
| `trend_filter_ma` | 200 | Période MA — aucun achat si prix < MA |
| `stop_loss_pct` | 2.0 | Stop-loss à X% sous le prix d'entrée |
| `take_profit_pct` | 4.0 | Take-profit à X% au-dessus du prix d'entrée |
| `max_holding_bars` | 96 | Sortie forcée après N bougies (96×15m = 24h) |

**Quand ça marche bien :** marchés en range (oscillation latérale) sur des ETF
liquides comme SPY, QQQ, IWM.

**Quand ça performe mal :** marchés en forte tendance directionnelle (le RSI
peut rester en zone extrême longtemps).

---

### MA Crossover (`ma_crossover`)

**Logique :** Achat quand la moyenne mobile courte (EMA20) croise au-dessus de
la longue (EMA50) — "golden cross". Vente quand elle recroise en dessous —
"death cross". Un filtre ATR supprime les signaux quand le marché est trop calme
(volatilité insuffisante pour couvrir les frais).

**Paramètres clés :**

| Paramètre | Défaut | Description |
|-----------|--------|-------------|
| `ma_short` | 20 | Période de la moyenne courte (EMA) |
| `ma_long` | 50 | Période de la moyenne longue (EMA) |
| `atr_period` | 14 | Période ATR pour le filtre de volatilité |
| `min_atr_pct` | 0.5 | Seuil minimum ATR/prix (%) pour trader |
| `stop_loss_pct` | 2.0 | Stop-loss en % |

**Quand ça marche bien :** marchés directionnels avec des tendances nettes.

**Quand ça performe mal :** marchés en range ou très volatils (faux croisements
répétés = whipsaw).

---

### Breakout (`breakout`)

**Logique :** Achat quand le prix clôture au-dessus du plus haut des N dernières
bougies ET que le volume dépasse `volume_multiplier × volume_moyen`. La
confirmation volumique évite les faux breakouts. Un cooldown (`cooldown_bars`)
empêche de rentrer plusieurs fois de suite après un signal.

**Paramètres clés :**

| Paramètre | Défaut | Description |
|-----------|--------|-------------|
| `lookback_bars` | 20 | Fenêtre de recherche du plus haut (breakout level) |
| `volume_ma_period` | 20 | Fenêtre de la MA de volume |
| `volume_multiplier` | 1.5 | Volume doit être > 1.5× la MA pour confirmer |
| `stop_loss_pct` | 3.0 | Stop-loss en % |
| `take_profit_pct` | 6.0 | Take-profit en % |
| `cooldown_bars` | 10 | Bougies à attendre après un signal (anti-spam) |

**Univers :** Cette stratégie cible des actifs volatils (crypto ou actions
tech). Elle essaie d'abord BTC/USD et ETH/USD ; si Alpaca ne supporte pas la
crypto sur votre compte, elle bascule automatiquement sur TSLA/NVDA/COIN.

---

### Bollinger Bands Mean Reversion (`bollinger_bands`)

**Logique :** Les bandes de Bollinger définissent une zone statistique "normale"
autour d'une MA20 (±2 écarts-types). Quand le prix clôture **sous** la bande
inférieure ET que le RSI confirme la faiblesse (RSI < 45), la stratégie achète
en anticipant un retour à la moyenne. Elle vend quand le prix remonte au-dessus
de la bande médiane (MA20) — la mean reversion est complète.

```
Bande sup ─────────────────────────────────────────────────────
MA 20     ─────────────── (objectif de sortie) ────────────────
Bande inf ────────────────────  ← BUY ici (clôture < bande inf
                                    + RSI < 45)
```

**Complémentaire avec RSI Mean Reversion :** les deux partagent le même univers
(SPY/QQQ/IWM) mais ont des conditions d'entrée différentes — les BB réagissent
à la volatilité relative, le RSI à l'amplitude du mouvement.

**Paramètres clés :**

| Paramètre | Défaut | Description |
|-----------|--------|-------------|
| `bb_period` | 20 | Fenêtre de calcul des bandes (MA + écart-type) |
| `bb_std` | 2.0 | Multiplicateur d'écarts-types (2.0 = ~95% des prix) |
| `rsi_period` | 14 | Période du RSI de confirmation |
| `rsi_confirm` | 45.0 | RSI doit être < ce seuil pour valider l'entrée |
| `stop_loss_pct` | 2.0 | Stop-loss en % sous le prix d'entrée |
| `take_profit_pct` | 3.0 | Take-profit en % (bracket order) |

**Quand ça marche bien :** marchés en range sur ETF liquides avec oscillations
régulières autour de la moyenne.

**Quand ça performe mal :** tendances fortes — le prix peut rester sous la bande
inférieure longtemps ("walking the band").

---

### MACD Crossover (`macd_crossover`)

**Logique :** Le MACD (Moving Average Convergence Divergence) est la différence
entre l'EMA12 et l'EMA26 du prix. Sa "ligne signal" est une EMA9 du MACD lui-même.
Quand le MACD croise **au-dessus** de sa ligne signal → BUY ; en dessous → SELL.

Avantage sur le simple croisement EMA : le MACD filtre les oscillations courtes
et détecte les changements de momentum avant que les moyennes mobiles elles-mêmes
ne se croisent. Le `min_histogram` évite les faux signaux sur des croisements
très faibles (quasi-tangents).

```
  MACD line ────╮
                ╰──────────── (crossover → BUY)
  Signal line ──────────────────────────────────
  Histogram  ▁▂▃▄▅  (positif = momentum haussier)
```

**Paramètres clés :**

| Paramètre | Défaut | Description |
|-----------|--------|-------------|
| `fast` | 12 | EMA rapide (standard MACD) |
| `slow` | 26 | EMA lente (standard MACD) |
| `signal_period` | 9 | EMA de la ligne signal (standard MACD) |
| `min_histogram` | 0.0 | Seuil minimum de l'histogramme (0 = tous les croisements) |
| `stop_loss_pct` | 2.0 | Stop-loss en % |
| `take_profit_pct` | 4.0 | Take-profit en % (bracket order) |

**Quand ça marche bien :** marchés directionnels, tendances de fond confirmées.

**Quand ça performe mal :** marchés très volatils avec retournements rapides
(le MACD lag persiste car il est basé sur des EMA).

---

### ADX + EMA Trend (`adx_ema_trend`)

**Logique :** L'ADX (Average Directional Index) mesure la **force** d'une
tendance indépendamment de sa direction. Quand ADX < 20 le marché est en range ;
quand ADX > 25 une tendance directionnelle est établie.

Cette stratégie ne génère de signaux **que quand ADX ≥ threshold** — éliminant
ainsi la principale faiblesse de MA Crossover (les faux croisements en range).
Les lignes directionnelles DI+ et DI- confirment le sens de la tendance.

```
ADX > 25 → tendance forte, signaux actifs
   EMA20 > EMA50 et DI+ > DI- → BUY (tendance haussière confirmée)
   EMA20 < EMA50 et DI- > DI+ → SELL (tendance baissière confirmée)

ADX < 25 → range, aucun signal émis
```

**Paramètres clés :**

| Paramètre | Défaut | Description |
|-----------|--------|-------------|
| `ema_fast` | 20 | EMA courte pour la direction |
| `ema_slow` | 50 | EMA longue pour la direction |
| `adx_period` | 14 | Période du lissage ADX/DI (Wilder = 14) |
| `adx_threshold` | 25.0 | ADX minimum pour générer des signaux |
| `stop_loss_pct` | 2.0 | Stop-loss en % |
| `take_profit_pct` | 5.0 | Take-profit en % (bracket order) |

**Quand ça marche bien :** marchés avec des tendances nettes et durables.
Produit moins de trades que MA Crossover mais avec un meilleur ratio signal/bruit.

**Quand ça performe mal :** marchés en range persistant (ADX reste bas — pas de
signal du tout, ce qui est en soi une protection).

---

## 4. Le Risk Manager — 8 niveaux de protection

Chaque signal passe **séquentiellement** par ces 8 vérifications. La première
qui échoue bloque l'ordre et log la raison.

```
Signal entrant
     │
     ▼
[1] Kill switch global engagé ?  →  BLOQUE
     │
     ▼
[2] Stratégie pausée aujourd'hui ?  →  BLOQUE
     │
     ▼
[3] Perte mensuelle ≥ 50 € ?  →  ENGAGE kill switch global + BLOQUE
     │
     ▼
[4] Perte journalière globale ≥ 3% equity ?  →  BLOQUE
     │
     ▼
[5] Perte journalière stratégie ≥ 2% equity ?  →  PAUSE stratégie + BLOQUE
     │
     ▼
[6] Nombre de positions ouvertes ≥ max_concurrent ?  →  BLOQUE
     │
     ▼
[7] ≥ 3 day trades sur les 5 derniers jours (PDT) ?  →  BLOQUE
     │
     ▼
[8] Taux d'ordres ≥ max_orders_per_minute ?  →  BLOQUE
     │
     ▼
     OK → signal transmis au position sizer
```

### Ce qui se remet à zéro

| Quand | Reset |
|-------|-------|
| Chaque jour à 9h25 ET | PnL journalier, pause de stratégie, rate limiter |
| Le 1er de chaque mois à 00h01 UTC | Compteur de perte mensuelle |
| Jamais automatiquement | Kill switch global — reset manuel obligatoire |

### Modifier les limites

Dans `config/risk_global.yaml` (limites globales) ou dans chaque YAML de
stratégie (section `risk:`).

```yaml
# config/risk_global.yaml
global_limits:
  max_monthly_loss_eur: 50.0    # hard stop permanent
  max_daily_loss_pct: 3.0       # perte journalière globale
  max_daily_loss_pct: 3.0

# config/strategies/rsi_mean_reversion.yaml
risk:
  max_risk_per_trade_pct: 1.0   # % equity risqué par trade
  max_concurrent_positions: 2   # positions simultanées
  max_daily_loss_pct: 2.0       # pause si dépassé
```

---

## 5. Le sizing des positions

La quantité d'actions achetée est calculée via la **formule du risque fixe** :

```
qty = (equity × risk_pct/100) / (entry_price × stop_loss_pct/100)
```

**Exemple :**
- Equity : 10 000 $
- `max_risk_per_trade_pct` : 1 % → risque maximum : 100 $
- Prix d'entrée : 450 $ (SPY)
- `stop_loss_pct` : 2 % → stop à 441 $, perte par action : 9 $

```
qty = 100 / 9 ≈ 11 actions
```

Si SPY atteint 441 $ (stop), la perte nette sera ≈ 100 $ = 1 % de l'equity.

> Alpaca supporte les **fractions d'actions** pour les ETF US. Le bot n'arrondit
> pas qty à l'entier — il peut acheter 11.11 actions.

---

## 6. Les ordres bracket

Un **ordre bracket** est un ordre d'achat market auquel sont automatiquement
attachés deux ordres de sortie fils :

- **Stop-loss** : ordre SELL stop déclenché si le prix descend sous un seuil
- **Take-profit** : ordre SELL limit déclenché si le prix monte au-dessus d'un seuil

Les deux fils sont liés : dès que l'un s'exécute, l'autre est annulé
automatiquement par Alpaca (mécanisme OCO — One Cancels the Other).

### Activer les brackets pour une stratégie

Ajouter `take_profit_pct` dans le YAML de la stratégie :

```yaml
params:
  stop_loss_pct: 2.0      # stop à -2%
  take_profit_pct: 4.0    # TP à +4% (ratio R:R de 1:2)
```

Si `take_profit_pct` est absent ou nul, le bot soumet un ordre market simple
sans legs attachées.

### Avantage en pratique

Les deux ordres de sortie vivent **dans le système Alpaca**, indépendamment du
bot. Si le Raspberry Pi tombe en panne ou redémarre, les stops et TPs sont
toujours actifs — le broker les gère seul.

### Limites Alpaca

- Les brackets ne sont disponibles qu'en `time_in_force: day` (pour les actions).
- Ne pas mélanger `extended_hours: true` avec les brackets.
- En paper trading, les ordres fils s'exécutent sur le prix bid/ask simulé.

---

## 7. Le kill switch

Le kill switch est le **mécanisme d'arrêt d'urgence** du bot. Il a deux modes
de déclenchement.

### Déclenchement automatique

Le Risk Manager engage automatiquement le kill switch global si :
- La perte mensuelle atteint 50 € (configurable via `max_monthly_loss_eur`)

### Déclenchement manuel

Via la base de données (voir README section 10) ou via le dashboard web.

### Ce qui se passe quand il s'engage

1. **Tick suivant** : le runner détecte `is_halted() == True`
2. **Liquidation automatique** : un ordre market SELL est soumis pour chaque
   position non-plate ouverte chez Alpaca
3. **Notification Telegram** : une alerte critique est envoyée pour chaque
   position liquidée
4. **Ticks suivants** : le bot ne fait plus rien (pas de re-soumission de SELLs)

### Ce qui NE se passe PAS

- Les **ordres bracket en attente** (stops, TPs enfants) ne sont pas annulés —
  ils restent actifs chez Alpaca. Pour les annuler, utiliser l'interface Alpaca
  directement.
- Le bot ne redémarre pas automatiquement — un redémarrage manuel après reset
  du kill switch est nécessaire.

### Réinitialiser le kill switch

```bash
# SQLite
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
        print("Kill switch réinitialisé.")
EOF

# Redémarrer le bot
sudo systemctl restart trading-bot.service
```

---

## 8. Dashboard — lire les données

Accès : `http://<ip-du-raspy>:8080` (identifiants dans `.env`)

### Barre d'alerte rouge

Apparaît uniquement si le kill switch global est engagé. Affiche la raison
et l'heure d'engagement. **Action requise avant de redémarrer.**

### Cartes résumé (4 métriques)

| Carte | Ce qu'elle mesure |
|-------|------------------|
| **PnL du jour** | Somme des `pnl_net` des trades clôturés aujourd'hui (UTC) |
| **Perte mensuelle €** | Perte cumulée du mois en cours convertie en EUR (×0.92) |
| **Positions ouvertes** | Nombre de positions non-plates dans le dernier PositionSnapshot |
| **Stratégies actives** | Nombre de stratégies avec `enabled=true` en base |

> Le PnL du jour ne reflète que les trades **clôturés**. Une position ouverte
> avec un PnL non réalisé négatif n'apparaît pas encore dans le PnL du jour.

### Tableau "Positions ouvertes"

Source : table `position_snapshots`, dernière entrée par paire (stratégie, instrument).

| Colonne | Signification |
|---------|---------------|
| Stratégie | Nom de la stratégie qui détient la position |
| Symbole | Ticker (ex: SPY) |
| Qty | Nombre d'actions (peut être fractionnaire) |
| Prix moyen | Prix d'entrée moyen pondéré |
| PnL non réalisé | Gain/perte latent(e) en USD |
| Mode | `paper` ou `live` |

### Tableau "Trades récents"

Les 30 derniers trades **clôturés** (entrée + sortie complètes). Un trade
est créé en base quand la stratégie émet un signal SELL/CLOSE et que
l'ordre est exécuté.

| Colonne | Signification |
|---------|---------------|
| Entrée / Sortie | Horodatages UTC du round-trip complet |
| PnL net | Gain après frais de courtage |
| Durée | Temps entre entrée et sortie |

### Tableau "Événements de risque"

Chaque fois qu'un ordre est bloqué (sauf raisons mineures comme
"rate limit"), ou qu'un kill switch s'engage, un `RiskEvent` est créé.
Niveaux de sévérité :

| Niveau | Signification |
|--------|---------------|
| `info` | Information, aucune action requise |
| `warn` | Stratégie pausée, surveiller |
| `critical` | Kill switch engagé, action requise |

### API JSON

`GET /api/status` retourne les mêmes données en JSON, protégé par les mêmes
identifiants HTTP Basic. Utile pour Uptime Kuma ou un script de monitoring.

```bash
curl -u admin:motdepasse http://<ip>:8080/api/status | python3 -m json.tool
```

---

## 9. Notifications Telegram

Si `TELEGRAM_BOT_TOKEN` et `TELEGRAM_CHAT_ID_GLOBAL` sont renseignés dans
`.env`, le bot envoie les alertes suivantes :

| Événement | Heure | Message |
|-----------|-------|---------|
| Démarrage | au lancement | Bot démarré, liste des stratégies actives |
| Ordre BUY | temps réel | Symbole, quantité, prix estimé, stratégie |
| Ordre SELL | temps réel | Symbole, quantité, stratégie |
| Blocage critique | temps réel | Kill switch ou hard stop mensuel atteint |
| Liquidation kill switch | temps réel | Chaque position liquidée (alerte critique) |
| **Rapport journalier** | **16h30 ET** | **Bilan complet du jour (voir ci-dessous)** |
| Erreur non gérée | temps réel | Exception avec contexte stratégie/symbole |

Les notifications **non critiques** (signal bloqué par rate limit, marché
fermé, etc.) ne génèrent pas de message Telegram pour ne pas spammer.

### Rapport journalier (16h30 ET)

Chaque jour de bourse à 16h30 (heure de New York), le bot envoie
automatiquement un récapitulatif complet :

```
📊 Rapport journalier — 12/04/2026
PnL net : +$47.82
Trades : 5  —  Victoires : 3/5 (60%)
Positions ouvertes : 1
Perte mensuelle : €12.40

Par stratégie :
  • rsi_mean_reversion — 3 trade(s), +$31.50
  • macd_crossover — 2 trade(s), +$16.32
```

Le rapport est compilé à partir de la table `trades` (clôturés dans la
journée UTC) et des positions ouvertes chez le broker au moment de l'envoi.
Si aucune notification Telegram n'est configurée, le rapport est simplement
ignoré (le bot tourne normalement).

### Tester les notifications

```bash
python - << 'EOF'
import asyncio
from app.config import get_settings
from app.notifications.telegram import TelegramNotifier
from decimal import Decimal

async def test():
    s = get_settings()
    n = TelegramNotifier(s.telegram_bot_token, s.telegram_chat_id_global)
    await n.notify_startup(["rsi_mean_reversion", "ma_crossover"])

asyncio.run(test())
EOF
```

---

## 10. Modifier les paramètres d'une stratégie

Toute modification de YAML est prise en compte **au prochain redémarrage**
du bot (pas de rechargement à chaud).

```bash
# Éditer le fichier YAML
nano config/strategies/rsi_mean_reversion.yaml

# Redémarrer le bot
sudo systemctl restart trading-bot.service

# Vérifier que les paramètres sont chargés
journalctl -u trading-bot.service | grep "Strategy ready"
```

### Désactiver une stratégie sans l'effacer

```yaml
# config/strategies/ma_crossover.yaml
enabled: false
```

### Restreindre une stratégie à certains symboles

```yaml
universe:
  - symbol: SPY
    asset_class: equity
  # QQQ et IWM commentés = non tradés
```

### Changer le timeframe

```yaml
timeframe: 1h    # 1m | 5m | 15m | 30m | 1h | 4h | 1d
lookback: 200    # adapter le lookback au timeframe
```

> Changer le timeframe recalcule automatiquement le ratio Sharpe dans les
> backtests (annualisation correcte).

---

## 11. Créer une nouvelle stratégie

### Fichier Python minimal

```python
# app/strategies/ma_simple.py
from __future__ import annotations
from typing import TYPE_CHECKING, Any
import pandas as pd
from app.core.domain import Signal
from app.core.enums import SignalSide
from app.core.registry import strategy_registry
from app.strategies.base import Strategy

if TYPE_CHECKING:
    from app.strategies.base import StrategyContext

@strategy_registry.register("ma_simple")
class MASimple(Strategy):
    name = "ma_simple"
    version = "1.0.0"
    description = "Simple moving average crossover"
    required_timeframe = "15m"
    required_lookback = 60

    def generate_signal(self, candles: pd.DataFrame, ctx: StrategyContext) -> Signal | None:
        p: dict[str, Any] = ctx.params
        close = candles["close"]
        fast = close.rolling(p.get("fast", 10)).mean()
        slow = close.rolling(p.get("slow", 30)).mean()

        if pd.isna(fast.iloc[-2]) or pd.isna(slow.iloc[-2]):
            return None

        # Croisement haussier
        if fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]:
            return Signal(
                strategy_name=self.name,
                instrument=ctx.instrument,
                side=SignalSide.BUY,
                reason="fast MA crossed above slow MA",
                time=ctx.current_time,
            )
        return None

    def validate_params(self, params: dict[str, Any]) -> None:
        if params.get("fast", 10) >= params.get("slow", 30):
            raise ValueError("fast must be < slow")
```

### Enregistrer la stratégie

```python
# app/strategies/__init__.py
from app.strategies import ma_crossover, ma_simple, rsi_mean_reversion  # noqa: F401
```

### Fichier YAML de configuration

```yaml
# config/strategies/ma_simple.yaml
name: ma_simple
version: "1.0.0"
enabled: true
mode: paper
provider: alpaca

universe:
  - symbol: SPY
    asset_class: equity

timeframe: 15m
lookback: 60

params:
  fast: 10
  slow: 30
  stop_loss_pct: 1.5

risk:
  max_risk_per_trade_pct: 1.0
  max_concurrent_positions: 1
  max_daily_loss_pct: 2.0
  max_drawdown_pct: 8.0
  max_exposure_pct: 10.0
  max_orders_per_minute: 5

execution:
  order_type: market
  time_in_force: day
```

### Vérifier avec un backtest

```bash
python scripts/run_backtest.py \
  --strategy ma_simple \
  --symbol SPY \
  --start 2024-01-01 \
  --end   2024-12-31
```

---

## 12. FAQ opérationnelle

**Q : Le bot ne trade pas, les logs disent "Market closed — skipping tick".**

Normal. Alpaca considère le marché fermé hors des heures de session US
(9h30–16h00 ET). Le bot ne trade pas le soir, la nuit, ni le week-end.

---

**Q : Aucun signal n'est généré alors que le marché est ouvert.**

Plusieurs causes possibles :
1. Les conditions de la stratégie ne sont pas remplies (RSI > 30, pas de croisement, pas de breakout).
2. Le nombre de bougies retournées par Alpaca est insuffisant (`only N candles (need M) — skipping`).
3. La stratégie est pausée (`paused for today`) — vérifier les RiskEvents dans le dashboard.

---

**Q : Le bot a soumis un ordre mais je ne le vois pas dans le dashboard.**

Le dashboard affiche les `PositionSnapshots`, écrits après chaque tick.
Il peut y avoir un délai d'une bougie (15 minutes) entre la soumission
de l'ordre et son apparition dans le tableau.

---

**Q : J'ai modifié le YAML mais le bot n'en tient pas compte.**

Les configs sont lues au démarrage uniquement. Redémarrer le service :
```bash
sudo systemctl restart trading-bot.service
```

---

**Q : Comment arrêter d'urgence sans kill switch ?**

```bash
# Arrêt propre (attend la fin du tick en cours)
sudo systemctl stop trading-bot.service

# Arrêt immédiat (forcé)
sudo systemctl kill -s SIGKILL trading-bot.service
```

Les ordres déjà soumis à Alpaca continuent leur cycle de vie normalement
(les legs bracket restent actifs chez le broker).

---

**Q : Le bot a planté au milieu d'un tick. A-t-il soumis l'ordre deux fois ?**

Non. Le `client_order_id` est un UUID généré **avant** l'appel REST. En cas
de crash après soumission, si le bot redémarre et retente le même tick
(ce qui n'arrive pas normalement, les ticks sont à heure fixe), Alpaca
détecte le `client_order_id` déjà connu et rejette le doublon.

---

**Q : Puis-je faire tourner plusieurs bots (paper + live) en même temps ?**

Oui, à condition d'utiliser des bases de données séparées (variables
`DATABASE_URL` différentes) et des répertoires de travail distincts.
Un seul bot par broker account est recommandé.

---

**Q : Quel est l'impact sur les performances du Raspberry Pi ?**

Le bot est très léger entre les ticks (CPU ≈ 0 %). Pendant un tick,
la charge est brève (calcul pandas + 1–2 appels REST). Un Raspberry Pi 4
avec 4 Go de RAM gère sans difficulté 10 stratégies simultanées.

---

## 13. Optimisation des paramètres par Walk-Forward

Le walk-forward est la méthode la plus robuste pour choisir les paramètres
d'une stratégie : au lieu de chercher les meilleurs paramètres sur **toutes**
les données historiques (risque d'overfitting), on répète le processus sur
des fenêtres glissantes et on mesure la performance réelle hors échantillon.

### Principe

```
Données complètes : ──────────────────────────────────────────────▶
                    │←─ IS (252j) ─→│←─ OOS (63j) ─→│
                         ↕ grid search   ↕ forward-test
                    │←─ step (63j) ──▶│←─ IS ─→│←─ OOS ─→│
                                           …
```

| Terme | Définition |
|-------|-----------|
| **IS (In-Sample)** | Fenêtre d'entraînement : grid search sur tous les combos |
| **OOS (Out-of-Sample)** | Fenêtre de test : meilleurs params IS appliqués à des données inconnues |
| **Step** | Décalage de la fenêtre pour l'itération suivante |
| **Fold** | Une paire IS + OOS |

### Utilisation

```bash
python scripts/run_walk_forward.py \
    --strategy rsi_mean_reversion \
    --symbol   SPY \
    --start    2022-01-01 \
    --end      2024-12-31 \
    --in-sample      252 \
    --out-of-sample   63 \
    --step            63 \
    --param-grid config/param_grids/rsi_mean_reversion.yaml \
    --metric     sharpe \
    --output     results/wf_rsi_spy.csv
```

### Options

| Option | Défaut | Description |
|--------|--------|-------------|
| `--strategy` | — | Nom de la stratégie (doit exister dans `config/strategies/`) |
| `--symbol` | — | Ticker (ex. `SPY`, `QQQ`) |
| `--start` / `--end` | — | Plage de données complète |
| `--in-sample` | 252 | Durée de la fenêtre IS en jours calendaires |
| `--out-of-sample` | 63 | Durée de la fenêtre OOS en jours calendaires |
| `--step` | 63 | Avancement de la fenêtre entre deux folds |
| `--param-grid` | — | Chemin vers le fichier YAML de grille de paramètres |
| `--metric` | `sharpe` | Critère d'optimisation : `sharpe` \| `return` \| `profit_factor` \| `win_rate` |
| `--equity` | 10000 | Capital initial par fold (USD) |
| `--output` | — | Chemin CSV pour sauvegarder les résultats fold par fold |

### Grilles disponibles

| Fichier | Stratégie | Combinaisons |
|---------|-----------|-------------|
| `config/param_grids/rsi_mean_reversion.yaml` | RSI Mean Reversion | 81 |
| `config/param_grids/bollinger_bands.yaml` | Bollinger Bands MR | 81 |
| `config/param_grids/macd_crossover.yaml` | MACD Crossover | 24 |
| `config/param_grids/adx_ema_trend.yaml` | ADX + EMA Trend | 54 |

### Exemple de sortie

```
Strategy : rsi_mean_reversion  |  Symbol : SPY
Metric   : sharpe  |  Combinations : 81
IS=252d  OOS=63d  step=63d

Fetched 19 873 bars.
Walk-forward windows : 9

  #  IS-start    IS-end      OOS-start   OOS-end       IS-sharpe     OOS-sharpe  Trades    OOS-PnL ($)  Best params
--------------------------------------------------------------------------------------------------------------------
  1  2022-01-01  2022-12-31  2023-01-01  2023-03-31         1.8240         0.9130       8        +62.40  rsi_period=14  oversold=30  overbought=70  stop_loss_pct=2.0
  2  2022-04-01  2023-03-31  2023-04-01  2023-06-30         2.1050         1.1200       6        +41.20  rsi_period=10  oversold=25  overbought=65  stop_loss_pct=1.5
  …

==============================
RÉSUMÉ — 9 fold(s)
  OOS sharpe moyen  : 0.9847
  OOS PnL total     : $387.50
  OOS trades total  : 61
  Folds profitables : 7/9
==============================
```

### Interpréter les résultats

- **IS metric >> OOS metric** → overfitting probable sur la fenêtre IS.
  Réduire la taille de la grille ou augmenter la fenêtre IS.
- **Folds profitables < 50 %** → la stratégie n'est pas robuste sur ce
  symbole/timeframe. Ne pas déployer.
- **OOS PnL total > 0 et Sharpe OOS > 0,5** → seuil minimal acceptable
  avant de passer en paper trading.

### Créer sa propre grille

```yaml
# config/param_grids/ma_crossover.yaml
param_grid:
  fast_window:   [5, 10, 20]
  slow_window:   [50, 100, 200]
  stop_loss_pct: [1.0, 2.0, 3.0]
```

Chaque clé doit correspondre exactement au nom d'un paramètre lu par
`generate_signal()` via `ctx.params.get("clé", défaut)`.
Le produit cartésien est calculé automatiquement.
