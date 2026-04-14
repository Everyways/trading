# Contexte permanent — Bot de trading algorithmique de Benoît

Ce document décrit de façon exhaustive le projet de trading bot algorithmique développé par Benoît.
Il sert de contexte de fond permanent pour tous les briefings de veille. L'assistant IA doit le
lire en priorité pour comprendre le projet avant de formuler toute réponse ou recommandation.

---

## 1. Identité du projet

**Porteur** : Benoît — développeur indépendant, particulier, résident fiscal français.  
**Objectif** : Automatiser des stratégies de trading algorithmique sur les marchés actions US, avec
une gestion rigoureuse du risque et une conformité réglementaire totale pour un résident français.  
**Stade actuel** : Opérationnel en mode paper et live sur Alpaca. Capital live réel déployé.  
**Horizon** : Long terme. Le projet évolue progressivement — nouvelles stratégies, nouveaux brokers,
optimisation continue des paramètres.  
**Langue de tous les briefings** : **Français**. Les termes techniques restent en anglais quand
c'est l'usage courant (ex : "backtesting", "drawdown", "PDT").

---

## 2. Architecture technique

### Stack principal

| Composant | Technologie |
|-----------|-------------|
| Langage | Python 3.12 (typing strict, `from __future__ import annotations`) |
| Plateforme | Raspberry Pi 4, 4 Go RAM, SSD 128 Go, Raspberry Pi OS 64-bit (Debian Bookworm) |
| Architecture | Monolithe async modulaire avec pattern plugin pour brokers et stratégies |
| Scheduler | APScheduler (cron-based, async) — toutes les 15 min pendant les heures de marché |
| Base de données | SQLModel + SQLite (stockage local des ordres, positions, snapshots) |
| Configuration | Pydantic Settings + fichiers YAML par composant (`config/strategies/*.yaml`) |
| Logging | Loguru (rotation automatique, niveau INFO en production) |
| Tests | Pytest + pytest-asyncio |
| Linting | Ruff (zero warnings exigé avant tout commit) |
| Web | FastAPI — API REST + dashboard HTML pour le monitoring local |

### Modules du projet

| Module | Rôle |
|--------|------|
| `app/core/` | Domain models (`Order`, `Position`, `Instrument`, `Candle`), enums, registries |
| `app/strategies/` | 6 stratégies (plugin pattern via `@strategy_registry.register`) |
| `app/providers/` | Brokers (plugin pattern via `@broker_registry.register`) |
| `app/backtest/` | Moteur de backtest (`BacktestEngine`, `BacktestMetrics`) |
| `app/execution/` | `runner.py` — boucle d'exécution principale ; `strategy_loader.py` |
| `app/risk/` | `manager.py` — gestion du risque global et par stratégie |
| `app/web/` | API REST (`/api/status`, `/api/orders`) + dashboard |
| `app/data/` | Modèles SQLModel, sessions DB |
| `scripts/` | CLI : `run_backtest.py`, `run_walk_forward.py`, `export_tax_fr.py` |
| `trading-routines/` | Sous-projet autonome — briefings IA hebdo/mensuel/trimestriel |
| `config/strategies/` | Un YAML par stratégie (paramètres, universe, risk limits) |
| `config/risk_global.yaml` | Limites globales de risque (kill switch, hard stop, PDT) |

### Pattern BrokerProvider

Tous les brokers sont abstraits derrière l'ABC `BrokerProvider`. Méthodes obligatoires :
`connect()`, `disconnect()`, `get_account()`, `place_order()`, `cancel_order()`,
`get_positions()`, `get_historical_candles()`. Chaque provider est enregistré dans
`broker_registry`. Cette architecture garantit la portabilité inter-broker sans toucher
aux stratégies ni à la boucle d'exécution.

### Pattern Strategy

Chaque stratégie hérite de `BaseStrategy` et est enregistrée dans `strategy_registry`.
Interface principale : `generate_signal(candles: DataFrame, params: dict) -> Signal | None`.
Les paramètres sont externalisés dans `config/strategies/<name>.yaml`. Le chargeur de
stratégies (`strategy_loader.py`) résout l'univers de symboles au démarrage.

---

## 3. Stratégies implémentées (6)

### 3.1 RSI Mean Reversion

**Principe** : Achat quand le RSI passe sous le seuil de survente (`oversold`), vente quand il
repasse au-dessus du seuil de surachat (`overbought`). La MA200 sert de filtre de tendance.

| Paramètre | Valeur typique | Description |
|-----------|----------------|-------------|
| `rsi_period` | 14 | Période du RSI |
| `oversold` | 30 | Seuil d'achat |
| `overbought` | 70 | Seuil de vente |
| `lookback` | 250 | Barres OHLCV requises pour calcul |

**Timeframe** : 15 minutes  
**Régime favorable** : Marché en range, faible volatilité directionnelle, oscillations régulières  
**Régime défavorable** : Tendance forte (RSI reste en zone extrême, faux signaux répétés)  
**Point d'attention** : NaN en début de série géré par `pd.isna(last_rsi)` — valeur correcte.

### 3.2 MA Crossover (Moving Average Crossover)

**Principe** : Signal long quand la MA courte croise la MA longue à la hausse (golden cross),
signal de sortie quand elle croise à la baisse (death cross). Filtre ATR pour éviter les
crossovers en marché atone.

| Paramètre | Valeur typique | Description |
|-----------|----------------|-------------|
| `short_window` | 9 | Période MA courte (EWM) |
| `long_window` | 21 | Période MA longue (EWM) |
| `atr_min_pct` | 0.5 | Filtre : ATR% minimum requis |

**Timeframe** : 15 minutes  
**Régime favorable** : Tendance claire et soutenue (bull ou bear prolongé)  
**Régime défavorable** : Marché choppy, lateralisation (whipsaws fréquents, pertes cumulées)

### 3.3 Breakout

**Principe** : Achat quand le prix casse au-dessus du plus haut des N dernières périodes
avec confirmation par un volume supérieur à la moyenne. Vente symétrique sur cassure du
plus bas.

| Paramètre | Valeur typique | Description |
|-----------|----------------|-------------|
| `lookback_period` | 20 | Barres pour calcul du range |
| `volume_multiplier` | 1.5 | Volume requis vs MA volume |

**Timeframe** : 15 minutes  
**Régime favorable** : Volatilité élevée avec direction, catalyseurs fondamentaux/macro  
**Régime défavorable** : Faible volatilité (faux breakouts, pull-backs immédiats)  
**Universe** : Profil `equity_fallback` — **TSLA, NVDA, COIN** (très liquides en intraday)

### 3.4 Bollinger Bands Mean Reversion

**Principe** : Achat quand le prix clôture sous la bande inférieure (survente relative), vente
quand il atteint la bande supérieure ou la bande centrale (retour à la moyenne).

| Paramètre | Valeur typique | Description |
|-----------|----------------|-------------|
| `bb_period` | 20 | Période de la moyenne centrale |
| `bb_std_dev` | 2.0 | Nombre d'écarts-types des bandes |

**Timeframe** : 15 minutes  
**Régime favorable** : Range avec cycles de volatilité réguliers  
**Régime défavorable** : Breakout directionnel fort (prix longe la bande sans revenir)

### 3.5 MACD Crossover

**Principe** : Signal long quand la ligne MACD croise la ligne signal à la hausse. L'histogramme
MACD mesure la force du momentum. Filtrage optionnel sur histogramme minimum.

| Paramètre | Valeur typique | Description |
|-----------|----------------|-------------|
| `fast_period` | 12 | EMA rapide |
| `slow_period` | 26 | EMA lente |
| `signal_period` | 9 | Lissage du signal |
| `min_histogram` | 0.0 | Filtre force du momentum |

**Timeframe** : 15 minutes  
**Régime favorable** : Tendance avec momentum mesurable  
**Régime défavorable** : Range (oscillations rapides autour de zéro, faux signaux)

### 3.6 ADX + EMA Trend Following

**Principe** : Filtre de tendance par l'ADX (Average Directional Index). Position longue
uniquement si ADX > seuil (tendance suffisamment forte) ET prix au-dessus de l'EMA longue.
Sortie si ADX retombe sous le seuil ou si prix passe sous l'EMA.

| Paramètre | Valeur typique | Description |
|-----------|----------------|-------------|
| `adx_period` | 14 | Période de l'ADX |
| `adx_threshold` | 20 | Seuil de tendance (anciennement 25) |
| `ema_period` | 50 | Période de l'EMA longue |

**Timeframe** : 15 minutes  
**Régime favorable** : Tendances fortes et durables (ADX > seuil stable)  
**Régime défavorable** : Marchés choppy ou en consolidation (ADX < seuil chronique)

### 3.7 Profil equity_fallback

Universe de secours utilisé quand le profil crypto n'est pas disponible (cas Alpaca + FR).
Symboles : **TSLA, NVDA, COIN**. Très liquides, forte volatilité intraday, spreads serrés.
COIN est une action tracker crypto — exposé à la crypto sans restriction géographique.

---

## 4. Broker principal — Alpaca

### Caractéristiques générales

| Attribut | Valeur |
|----------|--------|
| Type | Broker US API-first, commission-free sur actions US |
| Marchés | Actions US (NYSE, NASDAQ, AMEX), ETFs US |
| **Crypto pour FR** | **Non disponible** — Alpaca Crypto exige un KYC US |
| Mode paper | Oui (comptes paper/live séparés, même API) |
| Mode live | Oui — compte ouvert avec capital réel |
| Devise | USD uniquement |
| SDK Python | `alpaca-py` (officiel, maintenu par Alpaca) |
| API | REST + WebSocket streaming (cotations temps réel, ordres) |
| Fractional shares | Activé |

### Compte live de Benoît

- **Capital live** : Pool de **500 €** (≈ $540 selon taux de change)
- **Hard stop mensuel** : **−50 €**. Si la perte mensuelle atteint ce seuil, le bot se suspend
  automatiquement jusqu'au 1er du mois suivant (reset automatique)
- **Types d'ordres utilisés** : Market orders, limit orders, stop-loss orders
- **Position sizing** : Fractional shares (permet des ordres < 1 action)

### Limitations importantes

- Règle PDT active (voir section 6)
- Exposition au risque de change EUR/USD non hedgée
- Pas de levier utilisé
- Crypto indisponible pour résidents FR → equity_fallback systématique

---

## 5. Second broker pressenti — Interactive Brokers (IBKR)

### Statut

IBKR est identifié comme le candidat naturel pour une migration ou une utilisation complémentaire
à moyen terme. L'architecture `BrokerProvider` a été conçue explicitement pour cette portabilité.
Aucune intégration IBKR n'est déployée à ce jour.

### Points clés à évaluer

- **Onboarding FR** : IBKR Europe (Dublin, réglementé EU) accepte les résidents français. KYC standard.
- **API disponibles** : Client Portal API (REST, maintenu activement), TWS Gateway (local), FIX/CTCI
- **SDK Python** : `ib_insync` (communautaire, robuste) ou `ibapi` officiel (plus verbeux)
- **Commissions** : Structure Tiered recommandée pour petits comptes actifs. Vérifier minimum mensuel.
- **Avantages vs Alpaca** : Marchés EU, crypto disponible pour FR, taux de change intégrés, options
- **PDT** : Possibilité de contournement (compte IBKR UK/EU non soumis à FINRA, ou portfolio margin)

### Critères de migration

Migration justifiée si : (a) capital dépasse $25 000 (fin de la contrainte PDT sur Alpaca aussi),
ou (b) features absentes d'Alpaca deviennent nécessaires (crypto FR, marchés EU, options).
À réévaluer chaque trimestre avec une analyse chiffrée commission/feature.

---

## 6. Règle PDT (Pattern Day Trader) — FINRA Rule 4210

### Définition réglementaire

Un **day trade** est défini par FINRA comme l'achat et la vente (ou la vente courte et le rachat)
du même titre le **même jour de trading** sur un compte à marge. Un compte est classifié PDT
s'il exécute **4 day trades ou plus en 5 jours ouvrables** avec un capital inférieur à $25 000.

### Impact sur le compte de Benoît

Le compte Alpaca est < $25 000 → la règle PDT est **active et permanente** tant que le capital
reste sous ce seuil. Si la limite est atteinte sans respecter la règle, le broker restreint
le compte en mode "sell-only" pendant 90 jours.

### Stratégie de conformité : mode `overnight_hold`

Le bot opère exclusivement en mode `overnight_hold` :

- Les positions ouvertes en intraday ne sont **jamais clôturées le même jour** (sauf intervention
  manuelle exceptionnelle)
- Chaque position est conservée jusqu'au lendemain matin minimum avant d'être évaluée pour une clôture
- Ce mode génère un **risque overnight** (gap à l'ouverture) documenté et accepté
- Le compteur de day trades est suivi en DB ; si `recent_day_trades ≥ 3` sur 5 jours ouvrables,
  le bot passe en mode lecture seule jusqu'à expiration

### Compteur PDT dans `config/risk_global.yaml`

```yaml
pdt_compliance:
  enabled: true
  mode: overnight_hold
  max_day_trades_per_5d: 3
```

---

## 7. Fiscalité française — obligations de Benoît

### Régime fiscal applicable

Benoît est un **particulier résident fiscal français** réalisant des plus-values sur valeurs
mobilières étrangères (actions US cotées). Régime applicable : **Prélèvement Forfaitaire Unique
(PFU) à 30 %** (12,8 % impôt sur le revenu + 17,2 % prélèvements sociaux).

### Formulaires et obligations déclaratives annuelles

| Obligation | Formulaire | Échéance |
|------------|------------|----------|
| Plus-values mobilières | 2042-C (annexe) | Mai–juin (déclaration annuelle en ligne) |
| Compte-titres à l'étranger (Alpaca) | **3916-bis** | Avec déclaration annuelle — **OBLIGATOIRE**, même si solde nul |
| IBKR si ouvert | 3916-bis (ligne séparée) | Idem |
| Dividendes reçus | 2042-C également | Convention FR-US : retenue à la source 15 % imputable |

**Point critique** : L'omission du formulaire 3916-bis est une infraction fiscale (amende
minimale 1 500 € par compte non déclaré, majorée si fraude avérée).

### Calcul des plus-values en EUR

- Base imposable = Prix de vente (EUR) − Prix d'acquisition (EUR)
- Conversion USD → EUR : **taux de change du jour de chaque transaction** (taux BCE ou équivalent)
- **Frais de courtage** : déductibles de la base imposable
- **Moins-values** : imputables sur les plus-values de l'année ; excédent reportable 10 ans
- Dividendes : soumis au PFU après imputation de la retenue à la source US (15 % sous convention)

### Script d'export fiscal

`scripts/export_tax_fr.py` génère un CSV conforme aux attentes DGFiP avec les colonnes :
date, ticker, sens (achat/vente), quantité, prix unitaire USD, prix unitaire EUR, montant EUR,
taux de change, frais. Ce format doit rester synchronisé avec les évolutions DGFiP.

---

## 8. Infrastructure d'exploitation

### Raspberry Pi (serveur de production)

- Démarrage automatique via systemd service (`trading-bot.service`)
- Cron `@reboot` comme failsafe
- API web accessible localement sur port 8000 (FastAPI + dashboard HTML)
- Logs Loguru avec rotation hebdomadaire
- **Backup** : non automatisé actuellement — point de fragilité identifié

### Scheduler de briefings (`trading-routines/`)

Sous-projet autonome qui tourne indépendamment du bot principal. Géré par APScheduler.

| Cadence | Déclenchement | Focus |
|---------|---------------|-------|
| Hebdomadaire | Lundi 08h00 Europe/Paris | Veille des 7 derniers jours |
| Mensuel | 1er du mois 09h00 | Audit mensuel complet |
| Trimestriel | 1er jan/avr/jul/oct 10h00 | Revue stratégique + rotation clés API |

### Rapport quotidien Telegram

Le bot envoie chaque jour de bourse à **16h30 ET** (après clôture des marchés US) un récapitulatif
via Telegram : P&L réalisé du jour, positions ouvertes, ordres exécutés, alertes de risque.

### Walk-forward optimisation

`scripts/run_walk_forward.py` — optimisation rolling In-Sample / Out-of-Sample des paramètres
de stratégie. Fenêtre IS typique : 252 jours ; fenêtre OOS : 63 jours ; step : 63 jours.
Métriques disponibles : Sharpe ratio, total return, profit factor, win rate.

---

## 9. Rôle et règles de l'assistant IA

L'assistant IA (Claude) réalise des briefings de veille automatisés. Son rôle est
**uniquement informatif et factuel**. Il recherche des informations récentes, les synthétise
et alerte sur les risques. Il ne prend pas de décisions de trading.

### Règles permanentes (non négociables)

1. **Langue de sortie : français**. Les termes techniques restent en anglais si c'est l'usage
   courant dans la communauté (ex : "backtesting", "drawdown", "trailing stop").

2. **Pas de conseil d'investissement**. Faits, observations, points de vigilance — jamais de
   recommandation d'achat ou de vente d'un titre financier.

3. **Pas de prédiction directionnelle**. "Le marché risque de..." = interdit. "La semaine
   dernière, SPY a baissé de 2,3 %" = correct.

4. **Citations systématiques**. Chaque affirmation factuelle doit être sourcée (tags de
   citation `web_search`). Sans source vérifiable = sans affirmation.

5. **Honnêteté sur les lacunes**. Si `web_search` ne trouve rien de pertinent → écrire
   "Rien à signaler" ou "Données insuffisantes". Ne jamais meubler avec du contenu générique.

6. **Signalement urgent**. Si une information **critique** est trouvée (ex : Alpaca ferme
   le service pour les résidents FR, modification législative majeure impactant le PFU,
   faille de sécurité sur un SDK utilisé), la placer **en tête du résumé exécutif avec 🚨**.

7. **Pertinence ciblée**. Se concentrer sur ce qui impacte directement le projet de Benoît :
   retail, résident FR, actions US, petit capital, Raspberry Pi, Alpaca/IBKR. Éviter les
   informations génériques sur le trading institutionnel ou les marchés non pertinents.

8. **Concision**. Respecter les limites de mots imposées dans chaque prompt. Un bon briefing
   est dense et actionnable, pas long et générique.

---

## 10. Glossaire technique

| Terme | Définition dans ce projet |
|-------|--------------------------|
| **PDT** | Pattern Day Trader — règle FINRA qui restreint les day trades sur comptes < $25 k |
| **Day trade** | Achat et vente du même titre le même jour de bourse (définition FINRA) |
| **Overnight hold** | Stratégie de compliance PDT : garder les positions au moins jusqu'au lendemain |
| **IS / OOS** | In-Sample (entraînement) / Out-of-Sample (validation hors échantillon) |
| **Equity fallback** | Universe de secours : TSLA, NVDA, COIN (quand crypto indisponible) |
| **Hard stop mensuel** | Perte maximale mensuelle acceptée (−50 €) — déclenche suspension du bot |
| **PFU** | Prélèvement Forfaitaire Unique — flat tax française 30 % sur revenus du capital |
| **3916-bis** | Formulaire annuel obligatoire de déclaration des comptes titres étrangers |
| **2042-C** | Annexe de la déclaration de revenus pour plus-values mobilières |
| **BrokerProvider** | Interface abstraite Python définissant le contrat de tout broker intégré |
| **strategy_registry** | Registre Python des stratégies disponibles, peuplé au démarrage |
| **broker_registry** | Registre Python des brokers disponibles, peuplé au démarrage |
| **Sharpe ratio** | Rendement ajusté du risque : (R − Rf) / σ. Sans unité. |
| **Drawdown** | Perte cumulée depuis le dernier pic de capital |
| **Timeframe** | Durée d'une bougie OHLCV — ici systématiquement 15 minutes |
| **RSI** | Relative Strength Index — oscillateur de momentum (0–100) |
| **EWM** | Exponentially Weighted Moving average |
| **MACD** | Moving Average Convergence Divergence — momentum / tendance |
| **ADX** | Average Directional Index — force d'une tendance (0–100, fort > 20) |
| **Bollinger Bands** | Bandes de volatilité : moyenne ± N écarts-types |
| **Whipsaw** | Faux signal MA crossover suivi d'un retournement immédiat |
| **VIX** | CBOE Volatility Index — volatilité implicite S&P 500, surnommé "indice de la peur" |
| **SPY / QQQ / IWM** | ETFs trackers S&P 500, NASDAQ-100, Russell 2000 |
| **FINRA** | Financial Industry Regulatory Authority — régulateur US des brokers |
| **SEC** | Securities and Exchange Commission — régulateur US des marchés |
| **AMF** | Autorité des Marchés Financiers — régulateur français |
| **DGFiP** | Direction Générale des Finances Publiques — administration fiscale française |
| **IBKR** | Interactive Brokers — second broker pressenti |
| **alpaca-py** | SDK Python officiel d'Alpaca (REST + WebSocket) |
| **ib_insync** | Bibliothèque Python communautaire pour l'API TWS d'IBKR |
| **APScheduler** | Bibliothèque Python de scheduling (cron, interval, one-shot) |
| **Walk-forward** | Technique d'optimisation de paramètres anti-overfitting sur données historiques |
| **Paper trading** | Simulation sans capital réel — ordres fictifs, même API |
| **Live trading** | Mode réel — ordres envoyés au broker avec capital réel engagé |
| **Telegram bot** | Interface de notification et de monitoring via l'API Telegram Bot |
| **COIN** | Coinbase Global Inc. — action tracker de l'exposition crypto sur Alpaca pour FR |
| **equity_fallback** | Profil d'univers : TSLA, NVDA, COIN quand crypto indisponible |
| **Kill switch** | Mécanisme de suspension immédiate du bot (hard stop, défaut API, intervention manuelle) |
| **Position snapshot** | Enregistrement périodique de la position nette par stratégie et instrument |
| **Fractional shares** | Actions en fractions — permet des ordres < 1 titre (activé sur Alpaca) |
