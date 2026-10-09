(()=>{
  const box=document.getElementById('edge-lab');
  if(!box)return;
  const format=(v,d=4)=>v==null?'—':Number(v).toFixed(d);
  const cell=(row,value)=>{const td=document.createElement('td');td.textContent=String(value);row.appendChild(td)};
  async function refresh(){
    try{
      const [response,researchResponse,candidatesResponse]=await Promise.all([
        fetch('/api/edge-lab',{cache:'no-store'}),
        fetch('/api/edge-lab/status',{cache:'no-store'}),
        fetch('/api/edge-lab/candidates',{cache:'no-store'})]);
      if(!response.ok||!researchResponse.ok||!candidatesResponse.ok)throw new Error('EDGE LAB unavailable');
      const data=await response.json();
      const research=await researchResponse.json();
      const candidateData=await candidatesResponse.json();
      box.replaceChildren();
      const title=document.createElement('div');title.className='section-title';
      const label=document.createElement('div');
      const eyebrow=document.createElement('p');eyebrow.className='eyebrow';eyebrow.textContent='EDGE LAB · PAPER ONLY';
      const heading=document.createElement('h2');heading.textContent=`Futures Edge Lab: ${research.state}`;
      label.append(eyebrow,heading);
      const badge=document.createElement('span');badge.className='badge';
      badge.textContent=`${research.activeCandidates} ACTIVE PAPER · ${research.barCount} świec`;
      title.append(label,badge);box.appendChild(title);
      const note=document.createElement('p');note.className='muted';
      note.textContent=`Runner: ${research.runner} · proposals: ${research.signalCount} · ostatnia świeca: ${research.lastCycle==null?'brak':new Date(research.lastCycle).toLocaleString('pl-PL')} · ${research.lastError||research.note}`;
      box.appendChild(note);
      const researchScroll=document.createElement('div');researchScroll.className='scroll';
      const researchTable=document.createElement('table');const researchHead=document.createElement('thead');
      const researchHeadRow=document.createElement('tr');
      ['Model','Kierunek','Train','Validation','OOS','Shadow','Net exp.','CI low','PF','Cost +25%','Status'].forEach(x=>{const th=document.createElement('th');th.textContent=x;researchHeadRow.appendChild(th)});
      researchHead.appendChild(researchHeadRow);researchTable.appendChild(researchHead);
      const researchBody=document.createElement('tbody');
      for(const c of candidateData.candidates||[]){
        const row=document.createElement('tr');
        [c.modelId,c.direction,c.trainTrades,c.validationTrades,c.oosTrades,c.shadowTrades,
          format(c.netExpectancy,6),format(c.ciLow,6),format(c.profitFactor,2),
          format(c.costStress25,6),c.status].forEach(v=>cell(row,v));
        researchBody.appendChild(row);
      }
      researchTable.appendChild(researchBody);researchScroll.appendChild(researchTable);box.appendChild(researchScroll);
      const legacyTitle=document.createElement('h3');legacyTitle.textContent='Starsza historia — diagnostyka, bez promocji';
      box.appendChild(legacyTitle);
      const legacyNote=document.createElement('p');legacyNote.className='muted';
      legacyNote.textContent='Dawne transakcje Spot/Adaptive/Matrix nie stanowią niezależnego dowodu Futures OOS. Cel x5 nie jest gwarantowany.';
      box.appendChild(legacyNote);
      const scroll=document.createElement('div');scroll.className='scroll';
      const table=document.createElement('table');const head=document.createElement('thead');const hr=document.createElement('tr');
      ['Strategia','Kierunek','Reżim','Trades','Eff. trades','Gross exp.','Koszty','Net exp.','CI low','CI high','PF','DD','OOS','Cost +25%','Fill 75%','Selection bias','Status'].forEach(x=>{const th=document.createElement('th');th.textContent=x;hr.appendChild(th)});
      head.appendChild(hr);table.appendChild(head);
      const body=document.createElement('tbody');
      for(const c of data.candidates||[]){
        const row=document.createElement('tr');
        [c.strategy,c.direction,c.regime,c.trades,c.effectiveTrades,format(c.grossExpectancy,6),format(c.costs,4),format(c.netExpectancy,6),format(c.ciLow,6),format(c.ciHigh,6),format(c.profitFactor,2),format(c.maxDrawdown,3),format(c.oos,6),format(c.costStress25,6),format(c.fillStress75,6),c.selectionBiasRisk,c.status].forEach(v=>cell(row,v));
        body.appendChild(row);
      }
      table.appendChild(body);scroll.appendChild(table);box.appendChild(scroll);
    }catch(_){box.textContent='EDGE LAB: dane chwilowo niedostępne.'}
  }
  refresh();setInterval(refresh,30000);
})();
