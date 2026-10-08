/* Public PAPER telemetry only. No mutation or live activation endpoint. */
(() => {
  const section = document.createElement('section');
  section.className = 'panel';
  section.id = 'adaptive-panel';
  document.getElementById('view-paper').prepend(section);
  const node = (tag,text,className='') => {const n=document.createElement(tag);n.textContent=text;n.className=className;return n;};
  const fmt = x => x === null || x === undefined ? '—' : Number(x).toLocaleString('pl-PL',{maximumFractionDigits:4});
  function table(headers,rows) {
    const scroll=node('div','','scroll'), t=document.createElement('table'),head=document.createElement('thead'),tr=document.createElement('tr');
    headers.forEach(h=>tr.append(node('th',h)));head.append(tr);t.append(head);
    const body=document.createElement('tbody');
    rows.forEach(row=>{const r=document.createElement('tr');row.forEach(v=>r.append(node('td',String(v??'—'))));body.append(r);});
    t.append(body);scroll.append(t);return scroll;
  }
  async function refresh() {
    try {
      const response=await fetch('/api/adaptive',{signal:AbortSignal.timeout(15000)});
      if(!response.ok) throw Error('HTTP');
      const d=await response.json(),m=d.metrics;
      section.replaceChildren(node('p','NOWA STRATEGIA · PAPER · USDT','eyebrow'),node('h2',d.name));
      section.append(node('p',`${d.status.state.toUpperCase()} · Historia ${d.status.processed}/${d.status.total} rynków · Equity ${fmt(d.equity)} USDT · Gotówka ${fmt(d.cash)} USDT`));
      if(d.status.error || d.kill) section.append(node('p',d.kill || d.status.error,'muted'));
      section.append(node('p','24h rozgrzewki dla każdej pary USDT. Pozostałe waluty pozostają w skanerze. Maker: konserwatywna symulacja świecowa, nie rzeczywiste wykonania giełdowe.','muted'));
      const metrics=node('div','','metrics');
      [['Net PnL',m.netPnL],['Gross PnL',m.grossPnL],['Prowizje',m.fees],['Koszt spreadu',m.spreadCosts],['Poślizg',m.slippageCosts],
       ['Expectancy NET / transakcję',m.expectancyNet],['Profit factor',m.profitFactor],['Win rate %',m.winRate],['Średnia wygrana',m.averageWin],
       ['Średnia strata',m.averageLoss],['Max drawdown %',m.maxDrawdown],['Sharpe (dzienne)',m.sharpe],['Sortino (dzienne)',m.sortino],
       ['Transakcje / godzinę',m.tradesPerHour],['Średni czas pozycji (min)',m.averageHoldingMinutes],['Średni koszt',m.averageCost],
       ['Średni edge przed kosztami %',m.averageEdgeBeforePct],['Średni edge po kosztach %',m.averageEdgeAfterPct]].forEach(([k,v])=>{
         const box=node('div');box.append(node('span',k),node('strong',fmt(v)));metrics.append(box);
       });
      section.append(metrics,node('p',`Zamknięte transakcje: ${m.trades} / ${d.config.MIN_PAPER_TRADES}. LIVE ZABLOKOWANY. Dodatnia expectancy w paper nie stanowi gwarancji wyniku live.`,'muted'));
      const top=node('details');top.append(node('summary','Ranking wielookienkowy TOP 20'));
      top.append(table(['Para','Activity','Percentyl','Range / spread','Transakcje/min'],d.ranking.map(r=>[r.symbol,fmt(r.activityScore),fmt(r.activityPercentile),fmt(r.rangeToSpread),fmt(r.tradeIntensity)])));section.append(top);
      const pos=node('details');pos.append(node('summary',`Pozycje: ${Object.keys(d.positions).length} · Limity oczekujące: ${Object.keys(d.pending).length}`));
      pos.append(table(['Para','Stan','Ilość','Wejście','Stop'],[...Object.entries(d.positions).map(([s,p])=>[s,'OPEN',fmt(p.qty),fmt(p.entry),fmt(p.stop)]),...Object.entries(d.pending).map(([s,p])=>[s,'LIMIT_MAKER',fmt(p.qty),fmt(p.entry),fmt(p.stop)])]));section.append(pos);
      const log=node('details');log.append(node('summary','Ostatnie 50 decyzji i powody odrzucenia'));
      log.append(table(['Czas','Para','Decyzja','zReturn','priceZ','Koszt %','Edge %'],d.decisions.map(r=>[new Date(r.timestamp).toLocaleString('pl-PL'),r.symbol,r.decision,fmt(r.zReturn),fmt(r.priceZ),fmt(r.expectedCost),fmt(r.expectedEdge)])));section.append(log);
      const breakdown=node('details');breakdown.append(node('summary','Net PnL: para / decyl aktywności / z-score / zmienność'));
      for(const [key,groups] of Object.entries(m.breakdown)){breakdown.append(node('h3',key),table(['Grupa','Net PnL'],Object.entries(groups).map(([k,v])=>[k,fmt(v)])));}section.append(breakdown);
    } catch (_) {section.replaceChildren(node('h2','Adaptive Mean Reversion'),node('p','Brak aktualnej telemetrii. Nie traktuj poprzednich wyników jako bieżących.'));}
  }
  refresh();setInterval(refresh,30000);
})();
