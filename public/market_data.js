(()=>{
  const state=document.getElementById('market-data-state');
  const summary=document.getElementById('market-data-summary');
  const table=document.getElementById('market-data-hosts');
  if(!state||!summary||!table)return;
  const when=value=>value?new Date(value).toLocaleString('pl-PL'):'—';
  async function refresh(){
    try{
      const response=await fetch('/api/market-data/health',{cache:'no-store',signal:AbortSignal.timeout(15000)});
      if(!response.ok)throw Error('HTTP '+response.status);
      const data=await response.json();
      state.textContent=data.state;
      summary.textContent=data.likelyLocalNetworkBlock
        ?'All hosts blocked locally. Sprawdź zaporę Windows i dostęp Python do sieci; handel PAPER jest wstrzymany.'
        :'Aktywny host: '+data.activeHost+' · Przełączenia: '+data.failoverCount+
          (data.retryInSeconds?' · Ponowna próba za '+data.retryInSeconds+' s':'');
      table.replaceChildren(...data.hosts.map(host=>{
        const row=document.createElement('tr');
        for(const value of [host.host,host.state,host.lastLatencyMs==null?'—':host.lastLatencyMs+' ms',when(host.lastSuccess)]){
          const cell=document.createElement('td');cell.textContent=value;row.append(cell);
        }
        return row;
      }));
    }catch(error){state.textContent='UNAVAILABLE';summary.textContent='Nie można odczytać stanu źródeł danych.';}
  }
  refresh();setInterval(refresh,15000);
})();
