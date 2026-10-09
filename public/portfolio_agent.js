(()=>{
  const box=document.getElementById('portfolio-agent');
  if(!box)return;
  async function refresh(){
    try{
      const response=await fetch('/api/portfolio-agent',{cache:'no-store'});
      if(!response.ok)throw new Error('unavailable');
      const data=await response.json();
      box.replaceChildren();
      const title=document.createElement('div');title.className='section-title';
      const group=document.createElement('div');
      const eyebrow=document.createElement('p');eyebrow.className='eyebrow';eyebrow.textContent='AUTONOMOUS PORTFOLIO AGENT · PAPER ONLY';
      const heading=document.createElement('h2');heading.textContent=data.status;
      group.append(eyebrow,heading);
      const badge=document.createElement('span');badge.className='badge';badge.textContent=`${data.agentVersion} · ${data.provider}`;
      title.append(group,badge);box.appendChild(title);
      const summary=document.createElement('p');summary.className='muted';
      summary.textContent=`Automatyczna ocena PAPER bez ręcznej akceptacji. Potwierdzone rodziny: ${data.promotedFamilies.length}; otwarte pozycje: ${Object.keys(data.positions).length}; zamknięte transakcje: ${data.completedTrades}. LIVE wyłączony.`;
      box.appendChild(summary);
      const decision=document.createElement('p');decision.className='muted';
      const last=data.lastDecision;
      decision.textContent=last?`Ostatnia decyzja: ${last.action} · ${last.symbol||'cały portfel'} · ${new Date(last.time).toLocaleString('pl-PL')} · powody: ${(last.reasonCodes||[]).join(', ')}`:'Oczekiwanie na pierwszy zamknięty cykl rynku.';
      box.appendChild(decision);
    }catch(_){box.textContent='Portfolio Agent: dane chwilowo niedostępne.'}
  }
  refresh();setInterval(refresh,30000);
})();
