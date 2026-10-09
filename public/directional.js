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
  const edgeChart=points=>{
    const box=el('div'),label=el('p','GROSS EQUITY · NET EQUITY · CUMULATIVE COSTS');label.className='eyebrow';box.append(label);
    if(points.length<2){box.append(el('p','Krzywe pojawią się po co najmniej 2 zamkniętych transakcjach.'));return box;}
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 600 150');
    svg.setAttribute('width','100%');svg.setAttribute('height','150');
    svg.setAttribute('role','img');svg.setAttribute('aria-label','Gross equity, net equity i koszty skumulowane');
    const values=points.flatMap(p=>[p.grossEquity,p.netEquity,p.cumulativeCosts]),min=Math.min(...values),max=Math.max(...values);
    const y=value=>140-(value-min)/(max-min||1)*130;
    for(const [key,color] of [['grossEquity','#65d4b3'],['netEquity','#a493ff'],['cumulativeCosts','#fb889c']]){
      const line=document.createElementNS('http://www.w3.org/2000/svg','polyline');
      line.setAttribute('points',points.map((p,i)=>`${10+i*580/(points.length-1)},${y(p[key])}`).join(' '));
      line.setAttribute('fill','none');line.setAttribute('stroke',color);line.setAttribute('stroke-width','2');svg.append(line);
    }
    box.append(svg);return box;
  };
  let busy=false;
  async function refresh(){
    if(busy)return;busy=true;
    const expanded=[...root.querySelectorAll('details[open] summary')].map(x=>x.textContent);
    try{
      const response=await fetch('/api/directional',{signal:AbortSignal.timeout(12000)});
      if(!response.ok)throw Error('HTTP '+response.status);
      const d=await response.json(),s=d.statistics,m=d.monteCarlo,e=d.exposure;
      let health=null;
      try{const r=await fetch('/api/directional/edge-health',{signal:AbortSignal.timeout(12000)});if(r.ok)health=await r.json();}catch(_){}
      root.replaceChildren(el('h2','DIRECTIONAL ADAPTIVE PAPER'),el('p','LONG · syntetyczny SHORT · FLAT | 100 USDT osobnego kapitału | LIVE READY: NO'));
      root.append(el('p',`Stan: ${d.status.state} · Equity ${fmt(d.equity)} USDT · NET ${fmt(d.netPnL)} USDT · ${d.tradeCount} transakcji · LONG ${d.longTrades} · SHORT ${d.shortTrades} · otwarte ${Object.keys(d.positions).length}`));
      if(d.status.error)root.append(el('p',d.status.error));
      if(health){
        root.append(el('h3','EDGE HEALTH'));
        root.append(el('p',`Status: ${health.status} · zamknięte ${health.completedTrades} · otwarte ${health.openPositions} · zlecenia ${health.orders} · fill ${health.fills} · duplikaty ${health.duplicates}`));
        if(health.alerts.length){const alert=el('p','ALERTY: '+health.alerts.join(' · '));alert.className='negative';root.append(alert);}
        root.append(table('EDGE HEALTH · wynik i koszty',['Net expectancy','CI low','CI high','Rolling 50','Rolling 100','Rolling 250','Gross PnL','Koszty','Net PnL','Cost consumption','Trades/h'],[[
          fmt(health.netExpectancy),fmt(health.expectancyLower),fmt(health.expectancyUpper),
          fmt(health.rollingExpectancy['50']),fmt(health.rollingExpectancy['100']),fmt(health.rollingExpectancy['250']),
          fmt(health.grossPnL),fmt(health.totalCosts),fmt(health.netPnL),fmt(health.costConsumption),fmt(health.tradesPerHour)]]));
        root.append(table('KOSZTY WYKONANIA',['Fees','Spread','Slippage','Funding','Gross/trade','Cost/trade','Net/trade','Median hold','Churn 1m'],[[
          fmt(health.fees),fmt(health.spreadCost),fmt(health.slippageCost),fmt(health.fundingCost),
          fmt(health.grossExpectancy),fmt(health.costPerTrade),fmt(health.netExpectancy),
          fmt(health.medianHoldMinutes),fmt(health.churnRate)]]));
        root.append(table('LONG / SHORT · EDGE',['Kierunek','Trades','Gross','Costs','Net','Expectancy','PF','Win rate','DD'],Object.entries(health.bySide).map(([side,x])=>[
          side,x.trades,fmt(x.grossPnL),fmt(x.totalCosts),fmt(x.netPnL),fmt(x.netExpectancy),fmt(x.profitFactor),fmt(x.winRate),fmt(x.maxDrawdown)])));
        root.append(table('REGIME / SIDE · EDGE',['Reżim / kierunek','Trades','Gross','Costs','Net','Expectancy','PF'],Object.entries(health.regimeBySide).map(([key,x])=>[
          key,x.trades,fmt(x.grossPnL),fmt(x.totalCosts),fmt(x.netPnL),fmt(x.netExpectancy),fmt(x.profitFactor)])));
        root.append(table('PROBABILITY BUCKETS',['Kierunek / P','Trades','Labelled','Accuracy 15m','Gross expectancy','Net expectancy'],Object.entries(health.probabilityBuckets).map(([key,x])=>[
          key,x.trades,x.labelled,fmt(x.actualDirectionAccuracy),fmt(x.grossExpectancy),fmt(x.netExpectancy)])));
        root.append(table('EDGE / COST BUCKETS',['Ratio','Trades','Gross expectancy','Net expectancy'],Object.entries(health.edgeCostBuckets).map(([key,x])=>[
          key,x.trades,fmt(x.grossExpectancy),fmt(x.netExpectancy)])));
        root.append(table('CHURN / TURNOVER',['Re-entry 1m','3m','5m','10m','Turnover/h','Turnover/d','Costs/turnover'],[[
          health.reentryWithinMinutes['1'],health.reentryWithinMinutes['3'],health.reentryWithinMinutes['5'],health.reentryWithinMinutes['10'],
          fmt(health.notionalTurnoverPerHour),fmt(health.notionalTurnoverPerDay),fmt(health.costPerTurnover)]]));
        root.append(edgeChart(health.equityCurve));
        root.append(el('p','Obecna historia to zbiór diagnostyczny, nie dowód poprawy po strojeniu. Brak próbki oznacza brak oceny kalibracji. Nowe wejścia są wstrzymywane automatycznie dopiero przy spełnionym progu statystycznym; istniejące pozycje pozostają zarządzane.'));
      }
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
