# Glossaire — Jargon financier et technique

Définitions des termes utilisés dans le code, les configurations YAML et les
logs du bot. Classement alphabétique.

---

## A

### Account Equity (solde net du compte)
Valeur totale du compte broker = cash + valeur de marché de toutes les positions
ouvertes. C'est la base de calcul du sizing et de tous les seuils de risque
(ex : "perte journalière ≥ 3% de l'equity").

### APScheduler
Bibliothèque Python qui déclenche des fonctions à des heures précises. Le bot
l'utilise pour lancer les ticks à 0h, 15m, 30m, 45m et pour les resets journaliers
et mensuels.

### Asset Class (classe d'actif)
Catégorie d'un instrument financier. Le bot supporte :
- `equity` : actions et ETF cotés sur les marchés US
- `crypto` : cryptomonnaies (BTC, ETH) via Alpaca
- `option` : options (non tradées pour l'instant)

### ATR — Average True Range (plage réelle moyenne)
Indicateur de volatilité. Mesure l'amplitude moyenne des mouvements de prix sur
N bougies, en tenant compte des gaps entre séances. Un ATR élevé = marché
volatil, un ATR bas = marché calme.

Formule du True Range : `TR = max(high−low, |high−close_préc|, |low−close_préc|)`
L'ATR est la moyenne des TR sur N périodes.

**Utilisation dans le bot :** le filtre `min_atr_pct` de MA Crossover bloque
les signaux quand l'ATR représente moins de X% du prix (marché trop calme pour
couvrir les frais de transaction).

---

## B

### Backfill (remplissage historique)
Téléchargement en masse de données historiques (bougies) pour initialiser la
base de données. Nécessaire avant de lancer un backtest ou pour que les stratégies
disposent de l'historique suffisant dès le premier tick.

### Backtest
Simulation d'une stratégie sur des données historiques **passées** pour estimer
ses performances. Les trades sont simulés bougie par bougie avec une commission
configurable. Ne garantit pas les résultats futurs (biais de survie, overfitting
possible).

**Métriques produites par le backtest :**
- Net PnL, Return, Max Drawdown, Sharpe ratio, Profit Factor, Win rate

### Bar / Bougie (candle)
Unité de données de marché pour une période donnée. Contient :
- **O** (Open) : prix à l'ouverture de la période
- **H** (High) : prix le plus haut atteint
- **L** (Low) : prix le plus bas atteint
- **C** (Close) : prix à la clôture de la période
- **V** (Volume) : nombre d'actions échangées

Une bougie 15m représente 15 minutes d'activité de marché.

### Bracket Order (ordre à deux jambes)
Ordre d'achat auquel sont automatiquement attachés deux ordres de sortie :
- une jambe **stop-loss** (protection contre les pertes)
- une jambe **take-profit** (capture du gain cible)

Les deux jambes sont liées par un mécanisme **OCO** (One Cancels the Other) :
dès que l'une s'exécute, l'autre est annulée par le broker. Avantage majeur :
les sorties survivent à un crash du bot (elles vivent dans le système Alpaca).

### Broker
Intermédiaire entre le bot et les marchés financiers. Le bot utilise **Alpaca**
comme broker. Alpaca offre une API REST pour passer des ordres, consulter les
positions et l'historique de prix.

### Buying Power (pouvoir d'achat)
Montant disponible pour passer de nouveaux ordres. Différent de l'equity : si
vous avez des positions ouvertes, le buying power peut être inférieur à l'equity
(le broker gèle une partie du cash comme marge).

---

## C

### CLOSE (signal de clôture)
Type de signal interne. Équivalent fonctionnel de SELL — déclenche la clôture
de la position ouverte sans condition de prix. Utilisé par certaines stratégies
pour sortir après N bougies ou en fin de journée.

### Cooldown (période de refroidissement)
Nombre de bougies à attendre avant d'émettre un nouveau signal après un signal
précédent. Évite d'entrer et sortir de position en rafale (overtrading). Paramètre
`cooldown_bars` dans la stratégie Breakout.

### Commission (frais de courtage)
Coût de transaction prélevé par le broker. Alpaca propose du **zero-commission**
sur les actions US. Dans le backtest, une commission configurable (défaut 0.1%)
est appliquée pour simuler les frais réels (spread bid/ask, slippage).

---

## D

### Day Trade (trade intraday)
Trade ouvert **et** fermé dans la même session de marché (même journée de
trading). Soumis à la règle PDT aux États-Unis.

### Death Cross (croix de la mort)
Croisement baissier : la moyenne mobile courte passe **sous** la longue.
Signal de vente dans la stratégie MA Crossover. Inverse du Golden Cross.

### Decimal (type décimal exact)
Type Python utilisé pour **tous les montants monétaires** dans le bot. Contrairement
au type `float`, `Decimal` n'introduit pas d'erreurs d'arrondi lors des calculs
financiers (ex : 0.1 + 0.2 = 0.3 exactement).

### Drawdown (retrait)
Perte maximale depuis un pic de performance. Exprimé en % du capital au pic.
Un drawdown de -10% signifie que le compte a perdu 10% depuis son plus haut niveau.

**Max Drawdown :** le pire drawdown observé sur la période de backtest. Indicateur
de risque plus représentatif que la volatilité.

---

## E

### EMA — Exponential Moving Average (moyenne mobile exponentielle)
Moyenne mobile qui donne plus de poids aux données récentes qu'aux données
anciennes. Réagit plus rapidement aux changements de prix qu'une SMA (simple).

Formule : `EMA_t = prix_t × α + EMA_{t-1} × (1 − α)` où `α = 2/(span+1)`

### Entry Price (prix d'entrée)
Prix auquel la position a été ouverte. Pour les ordres market en paper trading,
c'est le prix bid/ask au moment de l'exécution simulée par Alpaca.

### Equity — voir Account Equity

### ETF — Exchange-Traded Fund (fonds coté en bourse)
Panier d'actifs coté en bourse comme une action. SPY, QQQ et IWM sont des ETF :
- **SPY** : réplique le S&P 500 (500 plus grandes entreprises US)
- **QQQ** : réplique le Nasdaq 100 (100 plus grandes entreprises tech)
- **IWM** : réplique le Russell 2000 (petites et moyennes capitalisations US)

---

## F

### Fill (exécution d'ordre)
Confirmation qu'un ordre a été exécuté par le broker. Un ordre peut être
partiellement rempli (partial fill) si le volume disponible est insuffisant.

### Flat (position plate)
Synonyme de "pas de position ouverte" sur un symbole. `qty = 0`.

### Fractional Share (action fractionnaire)
Alpaca permet d'acheter des fractions d'actions (ex : 0.5 SPY). Utile quand
la quantité calculée par le position sizer n'est pas un entier.

---

## G

### Gap (trou de prix)
Discontinuité entre le prix de clôture d'une bougie et le prix d'ouverture
de la suivante. Fréquent entre la clôture du marché la veille et l'ouverture
du lendemain, ou sur des annonces de résultats. Le buffer ×2 sur le lookback
existe pour absorber les gaps dans le calcul des indicateurs.

### Gate (filtre / validation)
Terme utilisé pour le Risk Manager : "gater un signal" = faire passer le signal
par toutes les vérifications de risque avant de soumettre l'ordre.

### Golden Cross (croisement doré)
Croisement haussier : la moyenne mobile courte passe **au-dessus** de la longue.
Signal d'achat dans la stratégie MA Crossover.

---

## H

### Hard Stop (arrêt permanent)
Limite qui, une fois atteinte, ne se réinitialise pas automatiquement. Dans le
bot : le hard stop mensuel à 50 € n'est réinitialisé que le 1er du mois suivant.
Opposé à un arrêt journalier qui se remet à zéro chaque matin.

### Healthcheck
Vérification que le broker est accessible et que le marché est ouvert. Le bot
appelle l'endpoint `/clock` d'Alpaca au début de chaque tick.

---

## I

### Idempotent (idempotent)
Propriété d'une opération qui produit le même résultat qu'on l'appelle une ou
plusieurs fois. Dans le bot : chaque `OrderRequest` a un `client_order_id` UUID
unique, donc re-soumettre le même ordre ne double pas la position — Alpaca
rejette le doublon.

### Instrument
Objet représentant un actif tradable : symbole + classe d'actif + nom du broker.
Exemple : `Instrument(symbol="SPY", asset_class=equity, provider_name="alpaca")`.

---

## K

### Kill Switch (coupe-circuit)
Mécanisme qui stoppe **immédiatement** toute activité de trading et **liquide
automatiquement** toutes les positions ouvertes via des ordres market SELL.

**Déclenchement automatique :** perte mensuelle ≥ 50 €  
**Déclenchement manuel :** via la DB ou le dashboard  
**Scope global :** bloque toutes les stratégies  
**Scope stratégie :** pause uniquement la stratégie concernée (journalière, non permanent)

Une fois engagé, le kill switch global ne peut être réinitialisé que manuellement
— le bot ne le relâche jamais de lui-même.

---

## L

### Latency (latence)
Délai entre la décision de trader et l'exécution effective. En paper trading,
la latence est virtuellement nulle. En live, les appels REST Alpaca prennent
50–200 ms depuis un Raspberry Pi avec une bonne connexion.

### Limit Order (ordre à cours limité)
Ordre qui ne s'exécute qu'à un prix **spécifié ou meilleur**. Opposé à l'ordre
market. Les take-profit dans les brackets sont des limit orders.

### Live Trading (trading réel)
Mode où les ordres sont soumis avec de l'argent réel sur le compte live Alpaca.
Activé en mettant `mode: live` dans le YAML de la stratégie et en utilisant les
clés API live.

### Lookback (fenêtre historique)
Nombre de bougies historiques chargées pour calculer les indicateurs. Si
`lookback: 250` et `timeframe: 15m`, le bot charge 250 bougies de 15 minutes
(≈ 2.5 jours de marché). Doit être suffisant pour calculer l'indicateur le
plus lent de la stratégie (ex : MA200 nécessite lookback ≥ 200).

---

## M

### Market Order (ordre au marché)
Ordre exécuté **immédiatement** au meilleur prix disponible. Garanti d'être
rempli, mais le prix d'exécution peut différer du dernier prix affiché (slippage).
C'est le type d'ordre principal utilisé par le bot pour les entrées.

### Max Drawdown — voir Drawdown

### Mean Reversion (retour à la moyenne)
Théorie selon laquelle les prix tendent à revenir vers leur valeur "normale"
après s'en être éloignés. Base de la stratégie RSI : un RSI < 30 signifie que
le prix a trop baissé et va probablement rebondir.

### Mode (paper vs live)
- **Paper** : ordres simulés chez Alpaca sans argent réel. Idéal pour tester.
- **Live** : ordres réels avec argent réel. Nécessite des clés API live.

---

## O

### OCO — One Cancels the Other (l'un annule l'autre)
Mécanisme broker : deux ordres liés dont l'exécution de l'un annule
automatiquement l'autre. Les brackets utilisent OCO pour les jambes
stop-loss et take-profit.

### Open Position (position ouverte)
Position dont la quantité (`qty`) est non nulle. Concrètement : le bot a
acheté des actions et ne les a pas encore revendues.

### Order Rate Limit (limite de taux d'ordres)
Nombre maximum d'ordres qu'une stratégie peut soumettre par minute. Évite
le spam d'ordres en cas de bug ou de signal répétitif. Configurable via
`max_orders_per_minute` dans le YAML.

### Overtrading (surtrading)
Trop de trades en trop peu de temps. Coûteux en frais, souvent dû à des
paramètres trop sensibles ou un cooldown insuffisant.

---

## P

### Paper Trading (trading papier / simulé)
Mode de trading où les ordres sont soumis à Alpaca mais n'impliquent pas
d'argent réel. Alpaca maintient un compte paper avec 100 000 $ virtuels.
Idéal pour valider une stratégie avant de passer en live.

### PDT — Pattern Day Trader (trader intraday récidiviste)
Règle de la SEC américaine qui s'applique aux comptes avec moins de 25 000 $.
Si un compte effectue **4 day trades ou plus sur 5 jours glissants**, il est
classifié PDT et bloqué pendant 90 jours.

**Protection dans le bot :** le Risk Manager suit les day trades et bloque
tout trade qui dépasserait 3 sur les 5 derniers jours.

### PnL — Profit and Loss (gains et pertes)
- **PnL brut** : différence entre prix de vente et prix d'achat × quantité
- **PnL net** : PnL brut moins les frais de courtage
- **PnL réalisé** : sur les positions fermées
- **PnL non réalisé** : sur les positions encore ouvertes (valeur latente)

### Position
Exposition à un actif. Une position longue = on détient des actions (on
gagne si le prix monte). Une position courte = on a vendu des actions
empruntées (on gagne si le prix baisse — non utilisé par défaut dans ce bot).

### Position Sizer (dimensionneur de position)
Module qui calcule la quantité d'actions à acheter selon l'equity, le prix
d'entrée et le risque acceptable par trade. Formule :
`qty = (equity × risk_pct/100) / (entry_price × stop_loss_pct/100)`

### Profit Factor (facteur de profit)
Ratio entre la somme des gains des trades gagnants et la somme des pertes
des trades perdants. Un PF > 1 = stratégie profitable. PF = 1 = neutre.
PF = 1.5 signifie que pour chaque dollar perdu, 1,50 $ est gagné.

---

## R

### Rate Limit (limitation de débit)
Nombre maximum d'appels API autorisés par unité de temps. Alpaca limite à
200 ordres par minute. Le Risk Manager applique une limite par stratégie
(`max_orders_per_minute`).

### Realised PnL — voir PnL réalisé

### Risk Manager (gestionnaire de risque)
Module central qui valide chaque ordre avant soumission. Applique 8 niveaux
de contrôle (kill switch, limites de pertes, PDT, rate limit...). Décrit en
détail dans le guide d'utilisation.

### RSI — Relative Strength Index (indice de force relative)
Oscillateur entre 0 et 100 qui mesure la vitesse et l'amplitude des variations
de prix récentes.
- RSI > 70 : actif en **surachat** (overbought) — potentiel de baisse
- RSI < 30 : actif en **survente** (oversold) — potentiel de hausse
- RSI ≈ 50 : neutre

Formule : `RSI = 100 − 100 / (1 + RS)` où `RS = gain_moyen / perte_moyenne`
sur les N dernières périodes.

### Round Trip (aller-retour)
Terme pour un trade complet : entrée (BUY) + sortie (SELL). Le PnL réalisé
n'existe que sur un round trip.

---

## S

### Scheduler (planificateur)
Composant qui déclenche les ticks à heure fixe (APScheduler dans le bot).
Le scheduler tourne dans la même boucle asyncio que le reste du bot.

### Session (session de trading)
Une journée de bourse US (9h30–16h00 ET). Les positions ouvertes et fermées
dans la même session constituent des day trades.

### Sharpe Ratio (ratio de Sharpe)
Mesure du rendement ajusté au risque. Formule :
`Sharpe = (rendement_annualisé − taux_sans_risque) / volatilité_annualisée`

- < 1 : performance insuffisante par rapport au risque pris
- 1–2 : bon
- > 2 : excellent
- > 3 : exceptionnel (rare sur des données longues)

Le bot annualise correctement selon le timeframe :
- 15m → ≈ 6552 bougies/an
- 1h → ≈ 1638 bougies/an
- 1d → 252 jours/an

### Signal
Décision émise par une stratégie : `BUY`, `SELL` ou `CLOSE`. Le signal contient
aussi la raison (`reason`) et un snapshot des indicateurs (`context`).

### Slippage (glissement)
Différence entre le prix espéré et le prix d'exécution effectif d'un ordre
market. En paper trading Alpaca, le slippage est simulé (spread bid/ask).
En live, il dépend de la liquidité du marché.

### SMA — Simple Moving Average (moyenne mobile simple)
Moyenne arithmétique des N derniers prix de clôture. Moins réactive que l'EMA.

### Stop-Loss (ordre de protection)
Ordre SELL déclenché automatiquement si le prix descend sous un seuil prédéfini.
Protège contre les pertes excessives. Dans les brackets, c'est une jambe enfant
de l'ordre principal.

### StrategyContext (contexte stratégie)
Objet passé à `generate_signal()` contenant toutes les informations dont la
stratégie a besoin : params YAML, instrument, position actuelle, equity,
heure courante.

---

## T

### Take-Profit (prise de bénéfice)
Ordre SELL limit déclenché si le prix monte au-dessus d'un seuil prédéfini.
Permet de capturer automatiquement le gain cible. Dans les brackets, c'est
la deuxième jambe enfant.

### Tick
Terme ambigu — dans le bot, désigne un **cycle d'évaluation** (toutes les 15
minutes), pas un mouvement de prix. À chaque tick, toutes les stratégies actives
sont évaluées.

En finance, "tick" désigne aussi la variation minimale de prix d'un actif.

### Timeframe (échelle de temps)
Durée représentée par une bougie. Valeurs supportées : `1m`, `5m`, `15m`,
`30m`, `1h`, `4h`, `1d`. Le bot utilise `15m` par défaut pour toutes les
stratégies.

### Trade
Round-trip complet enregistré en base de données : prix d'entrée, prix de
sortie, quantité, PnL net, durée. Un signal BUY crée une position ouverte ;
un signal SELL/CLOSE clôture la position et crée un Trade.

### Trailing Stop (stop suiveur)
Variante du stop-loss dont le seuil se déplace avec le prix. Si le prix monte,
le stop monte aussi (protège le gain). Si le prix descend, le stop reste fixe
(limite la perte). Mentionné dans le YAML de MA Crossover mais non implémenté
dans la version actuelle.

---

## U

### Universe (univers de trading)
Liste des symboles qu'une stratégie peut trader. Définie dans le YAML de la
stratégie (`universe:`). RSI_MR trade SPY, QQQ, IWM ; MA Crossover trade QQQ,
AAPL, MSFT.

### Unrealized PnL — voir PnL non réalisé

---

## V

### Volatility / Volatilité
Amplitude des variations de prix sur une période. Mesurée par l'ATR dans le bot.
Une forte volatilité = mouvements amples, opportunités de gains (et de pertes)
plus importantes.

### Volume
Nombre d'actions échangées pendant une période. Un signal de breakout n'est
valide que si le volume dépasse `volume_multiplier × volume_moyen` — évite les
faux breakouts sur faible liquidité.

---

## W

### Whipsaw (coup de fouet)
Série de faux signaux alternés BUY/SELL rapprochés, souvent causée par un
marché en range sur une stratégie de tendance. Coûteux en frais et en PnL.
Le filtre ATR de MA Crossover existe précisément pour limiter ce phénomène.

### Win Rate (taux de réussite)
Pourcentage de trades gagnants sur l'ensemble des trades. Un win rate de 50%
avec un ratio R:R de 1:2 (stop = moitié du TP) est profitable. Un win rate
élevé ne garantit pas la profitabilité si les pertes sont plus grandes que
les gains.
