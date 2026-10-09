# Mikrostruktura i shadow execution — etap PAPER

## Granice i ochrona istniejącego eksperymentu

Warstwa jest **wyłącznie diagnostyczna**. Nie zmienia zasad wejścia/wyjścia, konfiguracji ani hasha istniejącej strategii. Nie zmienia cash, pozycji, transakcji ani PnL. Sześć strategii bazowych i pliki `engine.py`, `adaptive.py`, `adaptive_config.json` pozostają bez zmian.

Wymagania „nowe filtry blokują wejścia” oraz „shadow nie zmienia eksperymentu” nie mogą jednocześnie dotyczyć tego samego portfela. W tym etapie blokada dotyczy **kwalifikacji diagnostycznej** (`diagnosticDecision`, `entryAllowed`), nie istniejących zleceń PAPER. Włączenie nowych filtrów do wykonania wymaga osobnego wersjonowanego eksperymentu, nie nadpisania obecnej historii. Żadna awaria obserwatora nie wycofuje zatwierdzonej transakcji PAPER.

Obserwator otwiera `adaptive.sqlite3` przez SQLite `mode=ro`. Dane badania są w osobnej `data/microstructure.sqlite3`. Post-commit kolejka telemetrii przekazuje wyłącznie klucze i czasy nowych decyzji. Instrumentacja nie zmienia rekordów starego portfela. Test z rzeczywistym symulowanym BUY i SELL potwierdza identyczność całego snapshotu z obserwatorem i bez niego.

## Dane: REST bootstrap + WebSocket + lokalny cache

- REST pobiera początkowe 1441 świec; potem `startTime` zaczyna się po ostatniej zapisanej świecy. Już poprzednia wersja nie pobierała ponownie całej historii co minutę.
- Nowy publiczny kolektor `wss://data-stream.binance.vision/ws` dostarcza zamknięte `kline_1m`, `bookTicker` i `aggTrade`. Nie używa kluczy ani streamów konta.
- Runner najpierw wykorzystuje ciągły fragment zamkniętych świec z cache WebSocket. REST służy tylko do luk i rozgrzewki. Niezamknięte świece nigdy nie trafiają do cache wykonania.
- Świece dotyczą wszystkich aktywnych par USDT. Mikrostruktura obejmuje do 600 par; priorytet mają posiadane pozycje i aktywne obserwacje shadow. Przekroczenie limitu jest jawne w API. Inne waluty pozostają w dotychczasowym skanerze.
- Subskrypcje są dzielone na ramki do 100 nazw / około 3,5 KB. Maksymalnie dwie ramki sterujące na sekundę, z miejscem na pong. Nowe subskrypcje nie wymagają rozłączenia istniejących.
- Reconnect z backoff 1–60 s. Luka `aggTrade` jest wykrywana po ID; duplikaty są ignorowane. ID `bookTicker` nie musi rosnąć o dokładnie 1 — brak takiej ciągłości nie jest błędnie raportowany jako utrata aktualizacji.

## Mikrostruktura

`microstructure_config.json` jest niezależny od konfiguracji portfela. Wagi mają charakter startowych założeń, **nie optymalnych parametrów**. Zmiana konfiguracji tworzy nowy hash kohorty; konfiguracje są zachowywane w bazie, raport domyślnie pokazuje bieżącą kohortę.

Spread: bieżący bid/ask i ilości, mid, spreadAbs, spreadPct, mediany 1/5/15m, P90/P95/min/max/std z ostatnich 15m, liczba próbek, pokrycie czasowe i stosunek bieżącego spreadu do mediany 5m. Są to statystyki próbkowania maksymalnie raz na sekundę, nie dokładna mediana wszystkich ticków ani średnia ważona czasem.

Imbalance: `(bidQty - askQty)/(bidQty + askQty)`, średnie 1m i 3m. Filtr imbalance domyślnie wyłączony. `aggTrade.m=true` oznacza agresywnego sprzedającego (kupujący był makerem). Wolumeny są w aktywie bazowym, liczby transakcji wynikają z zakresu `f..l`, osobno liczona jest liczba zdarzeń agregowanych.

Trade flow dotyczy ostatniej zamkniętej minuty. Początek subskrypcji w środku minuty, reconnect i luki oznaczają niepełne pokrycie. Brak danych nie jest raportowany jako neutralny flow.

MicrostructureScore łączy jakościowo spread, jego stabilność, imbalance, flow, nominał najlepszych poziomów i percentyl zmienności. Nie zastępuje ActivityScore. Wolatility regimes: LOW/NORMAL/HIGH/EXTREME przy percentylach 25/75/95, na wcześniejszych obserwacjach tego samego symbolu, bez przyszłych danych. Minimalnie 30 obserwacji. Dostępne zapisane historyczne wartości zmienności mogą służyć do rozgrzewki; **nie tworzymy dla nich fikcyjnych shadow outcomes**.

BTC regime: SHOCK przy przekroczeniu skonfigurowanego zReturn, TREND_UP/DOWN przy silnym rozdzieleniu EMA względem ATR, inaczej RANGE. Brak świeżego kontekstu oznacza UNKNOWN.

## Shadow: co rzeczywiście mierzymy

Rejestrowany jest każdy nowy kandydat statystycznego wejścia (zReturn lub priceZ), również odrzucony przed etapem edge/risk. Nie generujemy nowych transakcji. Brak odchylenia (`REJECT_NO_DEVIATION`) nie jest nowym sygnałem wejścia.

Zapis: ceny, książka, ilości, ActivityScore/decyl, MicrostructureScore, wskaźniki istniejącego modelu, koszty, edge/cost, reżimy, bazowa decyzja i osobne odrzucenia diagnostyczne. Nieznane wartości są `null`.

- Horyzonty wykonania: 1, 2, 5, 10, 30, 60 s.
- Horyzonty outcome: 1, 3, 5, 10, 20, 30 min.
- Pomiar zaczyna się w momencie odebrania sygnału przez obserwator po zatwierdzeniu PAPER. Oba czasy są zapisane. Nie udajemy znajomości wcześniejszej ścieżki pomiędzy zamknięciem świecy a otrzymaniem decyzji.
- Obserwacje zapisują termin, rzeczywisty czas odczytu i wiek. Tolerancja harmonogramu wynosi 2 s; spóźnione terminy są CENSORED, nie zastępowane późniejszą ceną.
- TOUCH to rzeczywista transakcja po cenie limitu; TRADE_THROUGH to transakcja poniżej limitu BUY. Dodatkowo `bookTouch` dotyczy ask osiągającego limit. Żaden touch nie jest automatycznie fill.
- MFE/MAE liczone są względem początkowego mid ze ścieżki obserwowanych kwotowań/transakcji. Nie są dokładną ścieżką ekonomiczną rzeczywiście posiadanej pozycji.
- Restart, luka strumienia danego symbolu, utrata subskrypcji i brak świeżej książki powodują cenzurowanie. Nie imputujemy zer ani nie zakładamy braku wykonania.

`estimatedMakerFillProbability` jest **niekalibrowanym empirycznym proxy przejścia przez limit**, z minimalną liczbą 30 prób i przedziałem Wilsona. To nie model rzeczywistej kolejki. Raport rozdziela spread bucket, odległość od mid, decyl aktywności, zmienność, imbalance i wiek zlecenia. Touch i trade-through raportowane są niezależnie. Cenzurowane ścieżki nie wchodzą do mianownika; wynik jest warunkowy względem dostępnych danych i może podlegać selection bias.

## Raporty i koszty

FILTER EFFECTIVENESS pokazuje oddzielnie filtry BASELINE i DIAGNOSTIC oraz każdy horyzont. Hipotetyczny wynik to mid-to-mid markout dla 100 USDT minus konfigurowany koszt round-trip. **Nie uwzględnia pewnego wykonania, stopa, kolejki, finansowania ani faktycznej ścieżki portfela**. To scenariusz, nie wynik transakcji. Kilka filtrów może odrzucić ten sam sygnał — ich sum nie należy dodawać jako rozłącznych grup ani interpretować przyczynowo.

EDGE/COST, ACTIVITY DECILES, VOLATILITY REGIMES i MARKET REGIMES pokazują wyniki **rzeczywistych transakcji bazowego modelu PAPER**, tylko dla sygnałów objętych nowym badaniem. Historyczny PnL nie jest przeliczany. Fill rate PAPER ma mianownik bazowych zaakceptowanych limitów, nie wszystkich odrzuconych kandydatów. Brak transakcji oznacza nieznaną expectancy/PF, nie dowód braku edge. Drawdown grupy to spadek skumulowanego zrealizowanego PnL w walucie kwotowanej, nie procentowy drawdown całego portfela.

FeeSchedule rozdziela makerFeePct, takerFeePct, spread, slippage, totalExecutionCost, costConsumption i costAsPctOfGrossEdge. Można później podmienić provider taryfy; obecna wersja nie pobiera informacji prywatnych. Koszty hipotetyczne są procentami nominału; skopiowane koszty zamkniętych PAPER trades są wartościami w USDT. Nie zmieniamy prowizji starego eksperymentu.

## Latencja i jakość

`bookTicker` Spot nie dostarcza timestamp giełdowego. Dla książki zapisujemy czas odbioru i jawne `marketDataTimestamp=null`; nie wymyślamy opóźnienia sieci.

Dla świec WS zapisujemy giełdowy czas zdarzenia i lokalny czas odbioru. Dla nowych decyzji mierzymy czas po obliczeniu decyzji i czas utworzenia zamiaru limitu PAPER. Różnice są dostępne tylko, jeżeli obserwowana świeca przybyła przed decyzją. Latencja giełda–aplikacja jest korygowana oszacowanym offsetem zegara z publicznego `/time`; surowa różnica zachowana jest osobno. Offset z REST sam ma niepewność opóźnienia sieci. REST lub historyczne decyzje bez takich pomiarów mają `null`. To latencja aplikacji PAPER, nie opóźnienie przyjęcia zlecenia przez giełdę.

Monitor: missingCandles, websocketReconnects, staleBookTicker, tradeStreamGap, duplikaty, błędne zdarzenia i clockDriftMs. „collecting” oznacza pracę kolektora, a nie gotowość każdego symbolu do wejścia. Każdy symbol ma osobną listę odrzuceń diagnostycznych.

## SQLite i retention

Nowe tabele w **osobnej bazie**:

- `market_microstructure`: minutowe snapshoty statystyk; 7 dni.
- `book_samples`: maksymalnie próbka/s/symbol; 20 minut.
- `trade_flow`: agregaty minutowe, 7 dni; brak surowego zapisu wszystkich transakcji.
- `ws_candles`: zamknięte świece cache, 2880 minut.
- `stream_state`, `quality_events`, `micro_meta`: ID, pokrycie, liczniki, konfiguracje i checkpoint odczytu PAPER; zdarzenia jakości 7 dni.
- `feature_history`: wcześniejsza zmienność, maksymalnie 1440 minut.
- `execution_timestamps`: nowe pomiary czasów, 7 dni; istotne wartości kopiowane do rekordów sygnałów.
- `shadow_signals`, `shadow_observations`, `rejected_signals`, `execution_quality`: kompaktowe dane walidacyjne, zachowywane bez automatycznego kasowania.
- `shadow_pending`: tylko aktywne obserwacje do 30 min, stan zapisywany przy zmianie horyzontu, nie przy każdym ticku.

Rekordy walidacyjne rosną wraz z liczbą sygnałów; produkcyjny wielomiesięczny eksperyment będzie wymagał archiwizacji tych rekordów. Nie ma nieograniczonego archiwum surowych ticków. Same agregaty spreadu nie pozwalają odtworzyć dokładnej pełnej kolejki.

## API i UI

- `GET /api/microstructure`: jakość, cechy symboli, postęp i raport shadow.
- `GET /api/shadow/report`: raport walidacyjny aktualnej kohorty.

Endpointy są tylko do odczytu. W Laboratorium paper dodano MICROSTRUCTURE, SHADOW EXECUTION, MAKER FILL ESTIMATE, FILTER EFFECTIVENESS, EDGE/COST, ACTIVITY DECILES, VOLATILITY REGIMES, MARKET REGIMES, EXECUTION QUALITY i LATENCY.

## Uruchomienie i testy

Python 3.12+, `python -m pip install -r requirements.txt`, następnie `python server.py`. Lokalnie można użyć `.venv`; Docker instaluje przypiętą zależność automatycznie. Konfiguracja nie przyjmuje kluczy. `adaptive_live.py` nie jest importowany do serwera.

`python -m unittest discover -s tests -v`

Testy obejmują historię spreadu, imbalance, flow/agresora, touch vs trade-through, shadow rejection/outcomes, kubełki kosztów, stale data, reconnect, limit ramek, duplikaty, restart, cache i inkrementalną naprawę REST, read-only SQLite, retention, reżimy, latencję, identyczność wyników PAPER oraz odporność na awarię obserwatora. Wszystkie używają atrap transportu — nie wysyłają zleceń.

Przed LIVE nadal potrzeba kalibracji na wiarygodnych wykonaniach, głębszego order book/queue model, rozliczenia częściowych wykonań, ochronnych zleceń, fee schedule konta, kompletnej rekoncyliacji, walidacji walk-forward/OOS i odpowiednio dużej próby (w tym wymaganego raportu 1000 paper trades). Dodatni markout/proxy fill nie dowodzi dodatniego realnego edge.

**PAPER READY: YES — dla zbierania i analizy diagnostycznej; nie jest to potwierdzenie rentowności.**

**LIVE READY: NO.**

Źródła protokołu: [Binance public WebSocket streams](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md), [websockets sync client](https://websockets.readthedocs.io/en/stable/reference/sync/client.html).
