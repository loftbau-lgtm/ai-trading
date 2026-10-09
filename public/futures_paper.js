(()=>{
  const box=document.getElementById('futures-paper');
  if(!box)return;
  const fmt=(value,digits=4)=>value==null?'—':Number(value).toFixed(digits);
  const text=(tag,value,className)=>{const node=document.createElement(tag);node.textContent=String(value);if(className)node.className=className;return node};
  const table=(headers,records)=>{
    const scroll=text('div','');scroll.className='scroll';
    const element=document.createElement('table');const head=document.createElement('thead');const hr=document.createElement('tr');
    headers.forEach(h=>hr.appendChild(text('th',h)));head.appendChild(hr);element.appendChild(head);
    const body=document.createElement('tbody');
    records.forEach(values=>{const row=document.createElement('tr');values.forEach(v=>row.appendChild(text('td',v)));body.appendChild(row)});
    element.appendChild(body);scroll.appendChild(element);return scroll;
  };
  async function refresh(){
    try{
      const response=await fetch('/api/futures-paper',{cache:'no-store'});
      if(!response.ok)throw new Error('Futures PAPER unavailable');
      const d=await response.json();box.replaceChildren();
      const title=text('div','');title.className='section-title';
      const group=text('div','');group.append(text('p','BINANCE USDⓈ-M FUTURES · PAPER ONLY','eyebrow'),text('h2',d.noConfirmedEdge?'NO CONFIRMED EDGE':'FUTURES PAPER PORTFOLIO'));
      title.append(group,text('span',`${d.status} · LIVE OFF`,'badge'));box.appendChild(title);
      box.appendChild(text('p','Osobny ledger 100 USDT · wykonanie MARKET po BID/ASK · ryzyko i stop po MARK · funding wyłącznie po rozliczeniu. Brak realnych zleceń.','muted'));
      const metrics=[['LONG exposure',d.longExposure],['SHORT exposure',d.shortExposure],['NET exposure',d.netExposure],['GROSS exposure',d.grossExposure],['Funding PnL',d.fundingPnL],['Fees',d.fees],['Realized PnL',d.realizedPnL],['Gross unrealized',d.grossUnrealizedPnL],['Net liquidation PnL',d.netLiquidationPnL],['Net liquidation equity',d.netLiquidationEquity]];
      const grid=text('div','');grid.className='metrics';
      metrics.forEach(([label,value])=>{const item=text('div','');item.append(text('span',label),text('strong',fmt(value)));grid.appendChild(item)});box.appendChild(grid);
      box.appendChild(text('h3','Pozycje'));
      const positions=Object.values(d.positions||{}).map(p=>[p.symbol,p.positionSide,fmt(p.qty,6),fmt(p.entryPrice),fmt(p.markPrice),fmt(p.contractPrice),fmt(p.stop),fmt(p.target),fmt(p.grossUnrealizedPnL),fmt(p.fundingPnL),fmt(p.estimatedExitCost),fmt(p.netLiquidationPnL),fmt(p.probabilityNetProfit,3),fmt(p.EVKeep,4),p.agentDecision]);
      box.appendChild(table(['Symbol','Position Side','Qty','Entry','Mark Price','Contract Price','Stop','Target','Gross PnL','Funding','Exit Cost','Net Liquidation PnL','P Profit','EV Keep','Agent Decision'],positions));
      box.appendChild(text('h3','Dziennik PAPER'));
      const events=(d.events||[]).slice(0,20).map(e=>[new Date(e.time).toLocaleString('pl-PL'),e.action,e.symbol,e.positionSide,e.executionSide,fmt(e.qty,6),fmt(e.price),fmt(e.netPnL)]);
      box.appendChild(table(['Czas','Akcja','Symbol','Position Side','Execution Side','Qty','Cena','Net PnL'],events));
      if(d.error)box.appendChild(text('p',`Źródło danych Futures: ${d.error}. Nowe decyzje wstrzymane.`,'muted'));
    }catch(_){box.textContent='Binance Futures PAPER: dane chwilowo niedostępne.'}
  }
  refresh();setInterval(refresh,30000);
})();
