/* Diagnostic read-only sidecar. Never changes the existing PAPER portfolio. */
(() => {
 const root=document.createElement('section');root.className='panel';root.id='microstructure-panel';
 document.getElementById('view-paper').append(root);
 const node=(tag,text='')=>{const n=document.createElement(tag);n.textContent=text;return n;};
 const fmt=x=>x===null||x===undefined?'—':Number(x).toLocaleString('pl-PL',{maximumFractionDigits:4});
 function table(headers,rows){const wrap=node('div');wrap.className='scroll';const t=node('table'),h=node('thead'),r=node('tr'),b=node('tbody');headers.forEach(s=>r.append(node('th',s)));h.append(r);rows.forEach(row=>{const tr=node('tr');row.forEach(s=>tr.append(node('td',s??'—')));b.append(tr);});t.append(h,b);wrap.append(t);return wrap;}
 function section(title,headers,rows){const d=node('details');d.append(node('summary',title),table(headers,rows));return d;}
 let busy=false;
 async function refresh(){
  if(busy)return;busy=true;
  const expanded=[...root.querySelectorAll('details[open] summary')].map(n=>n.textContent);
  try{
   const response=await fetch('/api/microstructure',{signal:AbortSignal.timeout(12000)});
   if(!response.ok)throw Error('Unavailable');
   const d=await response.json(),r=d.diagnostics,q=d.quality;
   root.replaceChildren(node('h2','Mikrostruktura i shadow execution'),node('p','WYŁĄCZNIE DIAGNOSTYKA · brak wpływu na cash, pozycje i PnL · LIVE READY: NO'));
   root.append(node('p',`${d.status.state} · ${q.trackedBooks} strumieni książki · ${r.signals} sygnałów · ${r.paperFilled} wykonań PAPER · ${r.baselineRejected} odrzuceń bazowych · ${r.diagnosticRejected} odrzuceń diagnostycznych · ${r.censored} nieobserwowanych horyzontów`));
   if(d.status.error)root.append(node('p',d.status.error));
   root.append(section('MICROSTRUCTURE',['Para','Spread %','Mediana 5m %','Ratio','Imbalance','Trade flow','Historia próbek','Diagnostyka'],d.markets.map(m=>[m.symbol,fmt(m.spreadPct),fmt(m.spreadMedian5m),fmt(m.spreadRatio),fmt(m.bookImbalance),fmt(m.tradeFlowImbalance),String(m.spreadSamples),m.diagnosticRejections.join(', ')||'DANE GOTOWE'])));
   root.append(section('SHADOW EXECUTION',['Para','Bazowa decyzja','Shadow','Score','Edge / cost','Reżim','Obserwacja'],r.recentSignals.map(s=>[s.symbol,s.baselineDecision,s.diagnosticDecision,fmt(s.microstructureScore),fmt(s.edgeCostRatio),s.volatilityRegime,new Date(s.observationStartTimestamp).toLocaleTimeString('pl-PL')])));
   root.append(section('MAKER FILL ESTIMATE · proxy trade-through, nie kolejka giełdy',['Grupa','Wiek (s)','Próby','Touch rate','Trade-through','Estymata proxy'],(r.makerProbability.spreadBucket||[]).map(p=>[p.bucket,String(p.ageSeconds),String(p.samples),fmt(p.touchRate),fmt(p.tradeThroughRate),fmt(p.estimatedMakerFillProbability)])));
   root.append(section('FILTER EFFECTIVENESS · hipotetyczny markout, nie transakcje',['Filtr','Horyzont s','Odrzucono','Obserwowano','Wygrane / straty','Śr. ruch %','Mediana %','MFE / MAE %','Hipotetyczne NET'],r.filterEffectiveness.map(f=>[f.layer+' / '+f.filter,String(f.horizonSeconds),String(f.signalsRejected),String(f.observed),`${f.hypotheticalWins} / ${f.hypotheticalLosses}`,fmt(f.averageFutureMove),fmt(f.medianFutureMove),`${fmt(f.MFE)} / ${fmt(f.MAE)}`,fmt(f.estimatedNetPnLIfAccepted)])));
   for(const [key,title] of [['edgeCostBucket','EDGE / COST'],['activityDecile','ACTIVITY DECILES'],['volatilityRegime','VOLATILITY REGIMES'],['marketRegime','MARKET REGIMES']]){
    root.append(section(title,['Grupa','Sygnały','Transakcje','Fill rate PAPER','NET','Expectancy','PF','Win rate','DD kwotowy','Koszt / trade','Czas min'],Object.entries(r.groups[key]||{}).map(([bucket,m])=>[bucket,String(m.signals),String(m.trades),fmt(m.paperFillRate),fmt(m.netPnL),fmt(m.netExpectancy),fmt(m.profitFactor),fmt(m.winRate),fmt(m.maxDrawdownQuote),fmt(m.costPerTrade),fmt(m.averageHoldingMinutes)])));
   }
   root.append(section('EXECUTION QUALITY',['Metryka','Wartość'],[['Brakujące świece',q.missingCandles],['Reconnects',q.websocketReconnects],['Stale book tickers',q.staleBookTicker],['Luki aggTrade',q.tradeStreamGap],['Duplikaty',q.duplicateEvents],['Niepoprawne zdarzenia',q.invalidEvents],['Clock drift ms',fmt(q.clockDriftMs)],['Symbole poza limitem obserwacji',d.status.unobservedSymbols],['Exchange latency bookTicker','Niedostępna: brak timestamp giełdy']]));
   root.append(section('LATENCY · ms, kreska oznacza brak pomiaru',['Para','Market data','Decision','Total signal','Observation lag'],r.recentSignals.map(s=>[s.symbol,fmt(s.marketDataLatency),fmt(s.decisionLatency),fmt(s.totalSignalLatency),fmt(s.signalObservationLagMs)])));
   root.append(node('p','Horyzonty shadow liczone są od odczytania sygnału przez obserwator. Braki pokrycia nie są zerami. Statystyki maker są niekalibrowanym proxy; markout nie dowodzi wykonania ani skuteczności przyczynowej filtra.'));
   for(const detail of root.querySelectorAll('details'))detail.open=expanded.includes(detail.querySelector('summary').textContent);
  }catch(_){root.replaceChildren(node('h2','Mikrostruktura'),node('p','Brak aktualnej telemetrii diagnostycznej. Stary eksperyment PAPER pozostaje niezależny.'));}
  finally{busy=false;}
 }
 refresh();setInterval(refresh,10000);
})();
