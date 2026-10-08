# Activity-Filtered Adaptive Mean Reversion

## Status wdrożenia

Nowy, odseparowany eksperyment **PAPER**, kapitał 100 USDT. Nie zmienia kont ani historii sześciu dotychczasowych strategii. Panel: **Laboratorium paper**, API tylko do odczytu: `/api/adaptive`. Stan przechowywany w `data/adaptive.sqlite3`, nie w repozytorium.

To wdrożenie części PAPER specyfikacji, **nie kompletny produkcyjny bot LIVE**. Kod adaptera `adaptive_live.py` jest odseparowany i nie jest importowany przez serwer. Nie skonfigurowano ani nie użyto prywatnego klucza Binance.

## Dane i ranking

- Skaner główny nadal odkrywa wszystkie waluty kwotowane Binance Spot.
- Nowy portfel handluje wyłącznie parami **USDT**. Wielowalutowy portfel i konwersje kapitału nie są zaimplementowane; nie porównujemy nominałów BTC i USDT.
- Dla wszystkich par USDT z aktualnymi statystykami skanera pobieramy 1441 zamkniętych świec 1m. Świeżo notowane pary bez 24h historii są wykluczone do zakończenia rozgrzewki.
- Okna 15m/1h/4h/24h: zakres względem pierwszego open, suma quote turnover, liczba transakcji, odchylenie standardowe procentowych zwrotów 1m. Percentyle i wagi zgodne z konfiguracją modelu; ranking deterministyczny, remisy rozstrzyga symbol. Do wejść dopuszczony TOP 20, następnie filtry płynności i percentyla.
- Spread w każdym oknie to **bieżący pomiar**, nie średni historyczny spread. Publiczne świece nie zawierają historii bid/ask; rekonstrukcja takiej historii wymaga osobnego kolektora order book.
- Docelowy cykl: 60 s. Pierwsze pobieranie może trwać kilka minut. Przy niepełnych/starych danych lub rozjechanym zegarze wejścia są zablokowane. Panel pokazuje postęp, czas cyklu i błędy. Wspólny limit publicznych zapytań i cooldown dla HTTP 418/429.
- Wskaźniki mają stałe okno 100 świec, więc odtworzenie nie zależy od wielkości cache. Zamknięte 3m/5m/15m są agregowane do granic UTC; niepełne interwały odpadają.

## Sygnał i ryzyko

Konfiguracja: `adaptive_config.json`. Zmiana parametrów strategii wymaga nowej bazy eksperymentu — hash zapobiega cichej zmianie zasad w trakcie testu. Sama liczba wątków pobierania nie zmienia hasha.

LONG wymaga odchylenia zReturn lub priceZ, wolumenu, bezpiecznego trendu, ceny **poniżej** VWAP, dodatniego edge po kosztach i miejsca w portfelu. Brak shortów, dźwigni i dokupowania tej samej pozycji.

Stop ma wymiar ceny: `max(ATR × mnożnik, entry × minimumStopPct / 100)`. Sizing uwzględnia koszt potencjalnego wyjścia, zmienność, spread, aktywność i drawdown. Domyślny bazowy budżet ryzyka 0,3%; filtry redukują faktyczne ryzyko. Limity obejmują oczekujące zlecenia, rezerwację gotówki, ekspozycję łączną i korelację 60 zwrotów. Nieokreślona korelacja jest traktowana konserwatywnie jako 1.

Ekstremalny zReturn BTC blokuje nowe wejścia na altcoinach przez 5 minut. Dzienny limit straty resetuje się o 00:00 UTC. Blokada max drawdown pozostaje zatrzaśnięta; nie ma publicznego przycisku jej resetowania.

Ręczny kill switch: utwórz pusty plik `data/adaptive.kill` (w Dockerze obok bazy w `/data`). Blokuje nowe wejścia; istniejące pozycje nadal podlegają wyjściom. Usunięcie tego konkretnego pliku wznawia tylko blokadę ręczną, nie resetuje limitu drawdown.

## Model wykonania PAPER — ograniczenia są jawne

1. Sygnał na zamknięciu tworzy nieruchomy limit BUY przy przybliżonym bid.
2. Dopiero kolejna zamknięta świeca może wypełnić limit. Wymagany ścisły trade-through (`low < limit`), nie sam dotyk, i udział nie większy niż 1% wolumenu świecy. Brak przesuwania limitu, wygaśnięcie domyślnie po minucie.
3. To model świecowy, **nie symulacja kolejki maker ani częściowych wykonań**. Nie dowodzi, że rzeczywista giełda wykonałaby zlecenie.
4. Wejście maker jest po cenie limitu, z maker fee. Nie dopisujemy fikcyjnego poślizgu ponad limit. Koszt spreadu/poślizgu na wejściu maker wynosi w modelu 0; oszacowanie edge pozostaje konserwatywne i obejmuje round-trip.
5. Sygnał wyjścia po powrocie do średniej tworzy nieruchomy limit SELL przy przybliżonym ask. Dopiero następna świeca może go wykonać (ścisłe `high > limit`, limit udziału w wolumenie), z maker fee. Po TTL limit jest anulowany i sygnał oceniany ponownie. Stop (również świeca wejścia) ma pierwszeństwo, a luka może wykonać go gorzej niż stop. Time stop zamyka po 20 minutach. Stop i time stop są modelowane jako taker, ze spreadem i poślizgiem.
6. Stop po wejściu nie jest poszerzany. `netPnL = grossPnL − fees − spreadCost − slippageCost`. Koszty są założeniami modelu, nie rzeczywistą taryfą konta Binance.
7. Panel expectancy/PnL/kosztów raportuje **zamknięte transakcje**. Equity dodatkowo wycenia otwarte pozycje i zapłacone prowizje wejścia. Sharpe/Sortino używają dziennych zwrotów UTC (annualizacja 365); brak wystarczających danych oznacza `null`, nie sztuczne zero. Profit factor bez strat jest nieokreślony, nie 0.

## Idempotencja i restart

Klucz decyzji: `binance:symbol:1m:candleCloseTimestamp`. Stan portfela, kursor, decyzje, transakcje i equity są zapisywane w jednej transakcji SQLite. Zapisane zlecenia i wyjścia są odtwarzane na brakujących świecach. **Nie otwieramy historycznych pozycji według dzisiejszego rankingu** — bez historycznego kontekstu logujemy odrzucenie. Podczas catch-up koszt wyjścia przy braku starego spreadu jest przybliżany skonfigurowanym limitem spreadu.

Dziennik obejmuje wskaźniki, hash konfiguracji, kontekst rankingu i powód decyzji. Jeśli brak historii do wskaźników, pola nie są wymyślane. Delisting lub luka w danych może uniemożliwić symulację wyjścia; aplikacja nie udaje wykonania bez danych.

## Walidacja i LIVE — co pozostaje do wykonania

`adaptive_validation.py` daje offline chronologiczny podział train / validation / OOS. Parametry wybiera tylko train według net expectancy z ograniczeniem drawdown. Każdy przedział startuje ze świeżym kapitałem; historię rozgrzewki należy dostarczyć w ramkach danych. Kolejne okna można wywoływać przesuwając granice. Nie pobiera dzisiejszego rankingu jako historycznego i nie generuje sztucznych 1000 transakcji.

Format wejścia JSON: `frames` (każda ramka: `now`, `histories`, `ranking`, opcjonalne `rankingAsOf`, `contextOk`), `candidates` (pełne konfiguracje), `trainEnd`, `validationEnd`, `oosEnd` — czas w ms UTC. Uruchomienie: `python adaptive_validation.py dataset.json`. Dataset punkt-w-czasie trzeba przygotować oddzielnie; automatyczny eksport pełnego datasetu nie jest jeszcze dostępny.

Adapter LIVE zawiera podpisywanie HMAC, LIMIT_MAKER, podstawowe filtry ilości/ceny/nominału, trwały client order ID, anulowanie po TTL i zapytanie o stan. Niejednoznaczny wynik POST blokuje kolejne wysłania zamiast ślepego retry. Trzy warunki: `TRADING_MODE=LIVE`, `ENABLE_LIVE_ORDERS=true`, osobne `CONFIRM LIVE <hash konfiguracji>`, a dodatkowo dodatnia expectancy z co najmniej 1000 paper trades. **Sam serwer nie używa tego adaptera i odrzuca tryb LIVE.**

Przed rzeczywistym LIVE nadal trzeba zaimplementować/integracyjnie sprawdzić:

- wspólny kontroler execution PAPER/LIVE, aktualizację portfela z częściowych wykonań i przeliczenie prowizji w innych aktywach;
- ochronne zlecenia giełdowe, rozliczenie anulowania w wyścigu z wykonaniem, pełną rekoncyliację po awarii;
- komplet aktualnych filtrów giełdy i account-specific fee schedule;
- historyczny spread/order-book, rzeczywisty dataset walk-forward i raport OOS;
- minimum 1000 faktycznie zebranych zamkniętych paper trades oraz raport kosztów, expectancy, PF i drawdown.

`liveReady` pozostaje **false**. Spełnienie progu paper nie oznacza samo w sobie gotowości do LIVE ani gwarancji zysku.

## Testy i źródła API

`python -m unittest discover -s tests -v`

Testy LIVE używają wyłącznie atrapy transportu. Żadne zlecenie nie jest wysyłane podczas testowania.

- [Oficjalne Binance Spot REST API — świece i pola wolumenu](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#klinecandlestick-data)
- [Zlecenia i anulowanie](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/trading-endpoints)
- [Filtry giełdowe](https://developers.binance.com/docs/binance-spot-api-docs/filters)
