(()=>{
  const box=document.getElementById('adaptive-matrix');
  if(!box)return;
  const f=(x,n=4)=>x==null?'—':Number(x).toFixed(n);
  async function refresh(){
    try{
      const d=await fetch('/api/adaptive-matrix',{cache:'no-store'}).then(r=>r.json());
      const rows=(d.variants||[]).slice(0,10);
      box.innerHTML=`<div class="section-title"><div><p class="eyebrow">ADAPTIVE EXPERIMENT MATRIX</p>
        <h2>Champion: ${d.champion||'—'}</h2></div><span class="badge">PAPER ONLY · ${d.variantCount||0} wariantów</span></div>
        <p class="muted">Wspólny snapshot rynku · osobny kapitał 100 USDT na wariant · LIVE wyłączony.</p>
        <div class="scroll"><table><thead><tr><th>#</th><th>Wariant</th><th>Status</th><th>Trades</th><th>Trades/h</th>
        <th>Net PnL</th><th>Expectancy</th><th>PF</th><th>Max DD</th><th>Stability</th><th>Score</th></tr></thead>
        <tbody>${rows.map(v=>`<tr><td>${v.rank}</td><td><b>${v.variantId}</b><br><span class="muted">${v.name}</span></td>
        <td>${v.status}</td><td>${v.metrics.trades??0}</td><td>${f(v.metrics.tradesPerHour,2)}</td>
        <td>${f(v.metrics.netPnL,4)}</td><td>${f(v.metrics.expectancyNet,6)}</td><td>${f(v.metrics.profitFactor,3)}</td>
        <td>${f(v.metrics.maxDrawdown,2)}%</td><td>${f(v.scoreParts.stabilityFactor,3)}</td><td>${f(v.score,2)}</td></tr>`).join('')}</tbody></table></div>
        <p class="muted">Pareto front: ${(d.paretoFront||[]).join(', ')||'brak wystarczającej próbki'} ·
        cykl Matrix ${f(d.cycle?.matrixCycleMs,0)} ms.</p>`;
    }catch(e){box.innerHTML='<p class="muted">Adaptive Matrix: oczekiwanie na dane.</p>'}
  }
  refresh();setInterval(refresh,30000);
})();
