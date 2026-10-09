/* Autonomous deterministic PAPER experiment. This view only reads local results. */
(() => {
  const host=document.getElementById('view-paper');
  const root=document.createElement('section');root.className='panel';root.id='directional-paper';host.append(root);
  const el=(tag,value='')=>{const e=document.createElement(tag);e.textContent=String(value);return e;};
  const fmt=(v,d=3)=>v===null||v===undefined||!Number.isFinite(Number(v))?'—':Number(v).toLocaleString('pl-PL',{maximumFractionDigits:d});
  const table=(title,headers,rows)=>{
    const box=el('details'),summary=el('summary',title),wrap=el('div'),grid=el('table'),thead=el('thead'),tr=el('tr'),tbody=el('tbody');
    wrap.className='scroll';headers.forEach(h=>tr.append(el('th',h)));thead.append(tr);
    rows.forEach(row=>{const line=el('tr');row.forEach(cell=>line.append(el('td',cell===null||cell===undefined?'—':cell)));tbody.append(line);});
    grid.append(thead,tbody);wrap.append(grid);box.append(summary,wrap);return box;
  };
  let busy=false;
  async function refresh(){
    if(busy)return;busy=true;
    const expanded=[...root.querySelectorAll('details[open] summary')].map(x=>x.textContent);
    try{
      const response=await fetch('/api/directional',{signal:AbortSignal.timeout(12000)});
      if(!response.ok)throw Error('HTTP '+response.status);
      const d=await response.json(),s=d.statistics,m=d.monteCarlo,e=d.exposure;
      root.replaceChildren(el('h2','DIRECTIONAL ADAPTIVE PAPER'),el('p','LONG · syntetyczny SHORT · FLAT | 100 USDT osobnego kapitału | LIVE READY: NO'));
      root.append(el('p',`Stan: ${d.status.state} · Equity ${fmt(d.equity)} USDT · NET ${fmt(d.netPnL)} USDT · ${d.tradeCount} transakcji · LONG ${d.longTrades} · SHORT ${d.shortTrades} · otwarte ${Object.keys(d.positions).length}`));
      if(d.status.error)root.append(el('p',d.status.error));
      root.append(table('PORTFEL I RYZYKO',['Kapitał','Gotówka','LONG exposure','SHORT exposure','Gross','Net','Kill switch'],[[fmt(d.capital),fmt(d.cash),fmt(e.long),fmt(e.short),fmt(e.gross),fmt(e.net),d.kill||'aktywny']]));
      root.append(table('STATISTICAL EDGE · bootstrap 95%',['Próba','NET expectancy','CI lower','CI median','CI upper','PF','Win rate','Max DD','Status'],[[s.trades,fmt(s.netExpectancy),fmt(s.expectancyLower),fmt(s.expectancyMedian),fmt(s.expectancyUpper),fmt(s.profitFactor),fmt(s.winRate),fmt(s.maxDrawdown),s.status]]));
      root.append(el('p',`Zrealizowany PnL ${fmt(d.realizedPnL)} USDT · niezrealizowany PnL ${fmt(d.unrealizedPnL)} USDT · koncentracja: rynek ${fmt(d.concentration.symbol)}, reżim ${fmt(d.concentration.regime)}, kierunek ${fmt(d.concentration.direction)}.`));
      root.append(table('BEST OPPORTUNITIES',['Rynek','Reżim','P UP','P DOWN','P FLAT','Ruch %','Koszt %','EV LONG %','EV SHORT %','Akcja','Confidence','Edge/cost','Wynik'],d.opportunities.slice(0,20).map(x=>[x.symbol,x.regime,fmt(x.pUp),fmt(x.pDown),fmt(x.pFlat),fmt(x.expectedUpMovePct),fmt(x.expectedCostPct),fmt(x.evLongPct),fmt(x.evShortPct),x.action,fmt(x.confidence),fmt(x.edgeCostRatio),x.orderResult||x.rejection||'—'])));
      root.append(table('LONG VS SHORT',['Kierunek','Trades','NET expectancy','CI lower','CI upper','PF','Win rate','DD'],Object.entries(d.bySide).map(([k,x])=>[k,x.trades,fmt(x.netExpectancy),fmt(x.expectancyLower),fmt(x.expectancyUpper),fmt(x.profitFactor),fmt(x.winRate),fmt(x.maxDrawdown)])));
      root.append(table('REGIMES',['Reżim','Trades','Expectancy','CI lower','CI upper','PF','DD','Status'],Object.entries(d.byRegime).map(([k,x])=>[k,x.trades,fmt(x.netExpectancy),fmt(x.expectancyLower),fmt(x.expectancyUpper),fmt(x.profitFactor),fmt(x.maxDrawdown),x.status])));
      root.append(table('EDGE CLASSES',['Klasa','Trades','Expectancy','CI lower','CI upper','PF','Status'],Object.entries(d.edgeClasses).sort((a,b)=>(b[1].netExpectancy??-1e9)-(a[1].netExpectancy??-1e9)).slice(0,30).map(([k,x])=>[k,x.trades,fmt(x.netExpectancy),fmt(x.expectancyLower),fmt(x.expectancyUpper),fmt(x.profitFactor),x.status])));
      root.append(table('KALIBRACJA 15M',['Wskaźnik','Wartość'],[['Próbka',d.predictionQuality.samples],['Accuracy',fmt(d.predictionQuality.directionAccuracy)],['LONG precision',fmt(d.predictionQuality.longPrecision)],['SHORT precision',fmt(d.predictionQuality.shortPrecision)],['LONG recall',fmt(d.predictionQuality.longRecall)],['SHORT recall',fmt(d.predictionQuality.shortRecall)],['Brier',fmt(d.predictionQuality.brierScore)],['Walk-forward ready',d.walkForward.ready?'TAK':'NIE']]));
      root.append(table('KALIBRACJA PRAWDOPODOBIEŃSTW',['Bucket','N','Deklarowane P','Obserwowane'],Object.entries(d.predictionQuality.calibration).map(([k,x])=>[k,x.count,fmt(x.predicted),fmt(x.actual)])));
      root.append(table('MONTE CARLO · empiryczny resampling',['N','P zysk vs teraz','P zysk vs start','Mediana equity','P5','P95','Śr. Max DD','P ruin'],Object.entries(m.horizons).map(([n,x])=>[n,fmt(x.probabilityAboveCurrent),fmt(x.probabilityAboveStarting),fmt(x.medianFinalEquity),fmt(x.p5Equity),fmt(x.p95Equity),fmt(x.expectedMaxDrawdown),fmt(x.probabilityOfRuin)])));
      root.append(el('p',`Szacowane transakcje do dodatniego wyniku: 80% ${m.estimatedTradesToPositive['0.8']??'—'}, 90% ${m.estimatedTradesToPositive['0.9']??'—'}, 95% ${m.estimatedTradesToPositive['0.95']??'—'} · do odrobienia ${fmt(m.estimatedTradesToRecover)}. Status: ${m.status}.`));
      root.append(table('COST STRESS',['Koszty bazowe','+25%','+50%','+100%'],[[fmt(d.costStress['1']),fmt(d.costStress['1.25']),fmt(d.costStress['1.5']),fmt(d.costStress['2'])]]));
      root.append(table('SHADOW I JAKOŚĆ WYKONANIA',['Sygnały','60 s obserwacje','Pełne','Cenzurowane','Trade-through','Status'],[[d.shadow.signals,d.shadow.observations60s,d.shadow.observed,d.shadow.censored,fmt(d.fillStress.observedTradeThroughRate),d.fillStress.status]]));
      root.append(table('SHADOW SIGNALS · 60 s',['Rynek','Side','Odrzucenie','Limit','EV LONG','EV SHORT','Obserwacja','Touch','Trade-through','NET markout %'],d.recentShadow.map(x=>[x.symbol,x.direction,x.rejection||'—',fmt(x.proposedPrice),fmt(x.evLongPct),fmt(x.evShortPct),x.outcome60s?.status||'OCZEKIWANIE',x.outcome60s?.touch==null?'—':String(x.outcome60s.touch),x.outcome60s?.tradeThrough==null?'—':String(x.outcome60s.tradeThrough),fmt(x.outcome60s?.netMarkoutPct)])));
      root.append(table('ODRZUCONE SYGNAŁY · hipotetyczny ruch 15m, nie fill',['Filtr','Obserwacje','Śr. NET ruch %','Dodatni odsetek'],Object.entries(d.filterEffectiveness).map(([key,x])=>[key,x.observed,fmt(x.hypotheticalMeanNetMovePct),fmt(x.hypotheticalPositiveRate)])));
      root.append(table('POZYCJE I ZLECENIA',['Rynek','Stan','Side','Qty','Cena','Stop','Target','Reżim'],[
        ...Object.entries(d.positions).map(([k,x])=>[k,'OPEN',x.side,fmt(x.qty,8),fmt(x.entry),fmt(x.stop),fmt(x.target),x.regime]),
        ...Object.entries(d.orders).map(([k,x])=>[k,'PENDING',x.side,fmt(x.qty,8),fmt(x.price),fmt(x.stop),fmt(x.target),x.regime])]));
      root.append(table('ZAMKNIĘTE TRANSAKCJE',['Rynek','Side','NET','Gross','Koszty','Wejście','Wyjście','Powód'],d.trades.slice().reverse().map(x=>[x.symbol,x.side,fmt(x.netPnL),fmt(x.grossPnL),fmt(x.totalCost),fmt(x.entryPrice),fmt(x.exitPrice),x.reason])));
      root.append(el('p','Prawdopodobieństwa kierunku są początkowo niekalibrowane. Statystyki i Monte Carlo pozostają puste do wymaganej próby. Maker fill to konserwatywne przybliżenie trade-through świecy, bez symulacji kolejki. Funding bez danych historycznych = 0, oznaczony jako niemodelowany.'));
      root.querySelectorAll('details').forEach(x=>x.open=expanded.includes(x.querySelector('summary').textContent));
    }catch(_){root.replaceChildren(el('h2','DIRECTIONAL ADAPTIVE PAPER'),el('p','Brak odpowiedzi modułu PAPER.'));}
    finally{busy=false;}
  }
  refresh();setInterval(refresh,12000);
})();
