(() => {
  const el=id=>document.getElementById(id);
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt=(v,d=2)=>Number(v).toLocaleString('pl-PL',{maximumFractionDigits:d});
  let data, limit=25;
  function render() {
    if (!data) return;
    const fresh=data.fresh && Date.now()-data.updatedAt <= 180000;
    el('scanner-status').textContent=fresh?'SKANOWANIE AKTYWNE':'DANE NIEDOSTĘPNE';
    el('scanner-meta').textContent=fresh?`${data.activeCount} aktywnych par Spot · ${data.rankedCount} z aktualnymi statystykami · Odczyt: ${new Date(data.updatedAt).toLocaleTimeString('pl-PL')} · Odświeżanie co 60 s`:(data.error||'Oczekiwanie na aktualne dane Binance…');
    const inputs=['scanner-trades','scanner-spread'].map(el);
    if (!fresh || inputs.some(i=>!i.checkValidity()||i.value==='')) {
      el('scanner-leaders').replaceChildren();el('scanner-rows').innerHTML='<tr><td colspan="9">'+(fresh?'Podaj poprawne wartości filtrów.':'Ranking wstrzymany do odświeżenia danych.')+'</td></tr>';
      el('scanner-count').textContent='';el('scanner-more').hidden=true;return;
    }
    const quote=el('scanner-quote').value, search=el('scanner-search').value.toUpperCase().replace(/[\s/]/g,''), minTrades=Number(inputs[0].value), maxSpread=Number(inputs[1].value), sort=el('scanner-sort').value;
    const rows=data.rows.filter(r=>(quote==='ALL'||r.quote===quote)&&r.symbol.toUpperCase().includes(search)&&r.trades>=minTrades&&r.spreadPct!==null&&r.spreadPct<=maxSpread);
    rows.sort((a,b)=>(sort==='fall'?a.changePct-b.changePct:b[sort]-a[sort])||a.symbol.localeCompare(b.symbol));
    el('scanner-leaders').innerHTML=rows.slice(0,3).map((r,i)=>`<article class="signal-card"><p>#${i+1} w bieżącym sortowaniu</p><h3>${esc(r.base)} / ${esc(r.quote)}</h3><strong>${fmt(r.score)} / 100</strong><p>Zakres ${fmt(r.rangePct)}% · Spread ${fmt(r.spreadPct,4)}%</p><button class="button" type="button" data-market="${esc(r.symbol)}">Wykres i sygnały</button></article>`).join('');
    el('scanner-count').textContent=`${rows.length} par spełnia filtry · widoczne ${Math.min(limit,rows.length)} · wybierz „Wszystkie”, aby objąć wszystkie waluty kwotowane`;
    el('scanner-rows').innerHTML=rows.slice(0,limit).map(r=>`<tr><td>${esc(r.base)} / ${esc(r.quote)}</td><td>${fmt(r.score)}</td><td>${fmt(r.price,10)}</td><td class="${r.changePct<0?'negative':'positive'}">${fmt(r.changePct)}%</td><td>${fmt(r.rangePct)}%</td><td>${fmt(r.quoteVolume)} ${esc(r.quote)}</td><td>${fmt(r.trades,0)}</td><td>${fmt(r.spreadPct,4)}%</td><td><button class="button" type="button" data-market="${esc(r.symbol)}">Otwórz</button></td></tr>`).join('')||'<tr><td colspan="9">Brak par spełniających filtry. Zmień walutę, wyszukiwanie lub progi.</td></tr>';
    el('scanner-more').hidden=rows.length<=limit;
  }
  ['scanner-search','scanner-quote','scanner-trades','scanner-spread','scanner-sort'].forEach(id=>el(id).addEventListener('input',()=>{limit=25;render();}));
  el('scanner-more').addEventListener('click',()=>{limit+=25;render();});
  document.querySelector('.scanner-panel').addEventListener('click',event=>{
    const button=event.target.closest('[data-market]');if(button)window.openMarket(button.dataset.market);
  });
  async function poll() {
    try {
      const response=await fetch('/api/scanner',{signal:AbortSignal.timeout(20000)});
      if(!response.ok)throw Error('Skaner niedostępny. Ponawiam automatycznie.');
      data=await response.json();
      const select=el('scanner-quote'),previous=select.value;
      const quotes=[...new Set(data.markets.map(m=>m.quote))].sort();
      if(quotes.length && [...select.options].map(o=>o.value).join(',')!==['ALL',...quotes].join(',')){
        select.replaceChildren(new Option('Wszystkie','ALL'),...quotes.map(q=>new Option(q,q)));
        select.value=quotes.includes(previous)||previous==='ALL'?previous:'ALL';
      }
      window.setMarketCatalog(data.markets);render();
    }catch(error){data={...data,fresh:false,error:'Brak połączenia ze skanerem. Ponawiam automatycznie.'};render();}
    finally{setTimeout(poll,30000);}
  }
  setInterval(render,5000);
  poll();
})();
