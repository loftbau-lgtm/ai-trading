/* No exchange API credentials or account tokens are persisted in the browser. */
(() => {
  const el = id => document.getElementById(id);
  const number = (v, digits = 4) => Number(v).toLocaleString('pl-PL', {maximumFractionDigits: digits});
  const escape = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let snapshot, accountGeneration = 0;
  function selectedBars() { return snapshot?.terminal?.markets[el('market-select').value] || []; }
  function selectedSignals() { return snapshot?.terminal?.signals[el('market-select').value]; }
  function clearAccount() {
    accountGeneration++;
    el('account-token').value = '';
    el('account-balances').replaceChildren(); el('account-orders').replaceChildren();
    el('account-data').hidden = true;
    el('account-status').textContent = 'ZABLOKOWANE';
    el('account-message').textContent = 'Dane ukryte. Odczytaj konto ponownie, aby je wyświetlić.';
    el('account-refresh').disabled = false;
  }
  document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => {
    document.querySelectorAll('[data-view]').forEach(b => b.setAttribute('aria-pressed', String(b === button)));
    document.querySelectorAll('.workspace-view').forEach(view => view.hidden = view.id !== 'view-' + button.dataset.view);
    if (button.dataset.view !== 'account') clearAccount();
  }));
  document.addEventListener('visibilitychange', () => { if (document.hidden) clearAccount(); });
  el('account-lock').addEventListener('click', clearAccount);
  el('account-form').addEventListener('submit', async event => {
    event.preventDefault();
    const token = el('account-token').value;
    clearAccount();
    const generation = accountGeneration;
    el('account-refresh').disabled = true;
    el('account-message').textContent = 'Odczytywanie konta…';
    try {
      const response = await fetch('/api/exchange/account', {headers: {Authorization: 'Bearer ' + token}, signal: AbortSignal.timeout(50000)});
      const data = await response.json();
      if (generation !== accountGeneration) return;
      if (response.status === 401) throw Error('Panel zablokowany. Konfigurację dostępu ustawimy na serwerze.');
      if (!response.ok) throw Error(data.message || 'Nie udało się odczytać konta.');
      if (data.state !== 'connected') {
        el('account-status').textContent = 'NIEPODŁĄCZONE';
        el('account-message').textContent = 'Brak konfiguracji API Binance na serwerze. Ustawimy ją później.';
        return;
      }
      el('account-status').textContent = 'POŁĄCZONO · ODCZYT';
      el('account-message').textContent = 'Odczyt konta: ' + new Date(data.updatedAt).toLocaleString('pl-PL');
      el('account-balances').innerHTML = data.balances.map(b => `<tr><td>${escape(b.asset)}</td><td>${escape(b.free)}</td><td>${escape(b.locked)}</td></tr>`).join('') || '<tr><td colspan="3">Brak niezerowych sald.</td></tr>';
      el('account-orders').innerHTML = data.orders.map(o => `<tr>${['symbol','side','type','price','origQty','executedQty'].map(k => `<td>${escape(o[k])}</td>`).join('')}</tr>`).join('') || '<tr><td colspan="6">Brak otwartych zleceń na obsługiwanych rynkach.</td></tr>';
      el('account-data').hidden = false;
    } catch (error) {
      if (generation === accountGeneration) { el('account-status').textContent = 'BRAK POŁĄCZENIA'; el('account-message').textContent = error.name === 'TimeoutError' ? 'Upłynął czas połączenia. Spróbuj ponownie.' : error.message; }
    } finally { if (generation === accountGeneration) el('account-refresh').disabled = false; }
  });
  function estimate() {
    const bars = selectedBars();
    if (!bars.length || !selectedSignals()?.fresh) { el('estimate-output').textContent = 'Brak aktualnej ceny do obliczeń.'; return; }
    const inputs = ['estimate-budget','estimate-fee','estimate-slip'].map(el);
    if (inputs.some(i => !i.checkValidity() || i.value === '')) { el('estimate-output').textContent = 'Podaj poprawne wartości kalkulatora.'; return; }
    const [budget, fee, slip] = inputs.map(i => Number(i.value));
    const price = bars.at(-1).close * (1 + slip/100), net = budget / (1 + fee/100);
    el('estimate-output').innerHTML = [['Cena z poślizgiem',number(price)+' USDT'],['Ilość orientacyjna',number(net/price,8)],['Prowizja wejścia',number(budget-net)+' USDT'],['Łączny koszt',number(budget)+' USDT']].map(([label,value]) => `<div><span>${label}</span><b>${value}</b></div>`).join('');
  }
  function render() {
    if (!snapshot) return;
    const symbol = el('market-select').value, bars = selectedBars();
    el('market-title').textContent = symbol.replace('USDT',' / USDT');
    if (bars.length) {
      const price = bars.at(-1).close, change = (price/bars[0].close-1)*100;
      el('market-price').textContent = number(price) + ' USDT';
      el('market-change').textContent = (change >= 0 ? '+' : '') + number(change,2) + '% w widocznym oknie';
      el('market-time').textContent = 'Ostatnie zamknięcie: ' + new Date(bars.at(-1).end).toLocaleString('pl-PL');
      const values = bars.map(b => b.close), low = Math.min(...values), high = Math.max(...values), span = high-low || high*.001;
      const points = bars.map((b,i) => `${30+i/Math.max(1,bars.length-1)*740},${190-(b.close-low)/span*160}`).join(' ');
      el('price-chart').innerHTML = `<svg viewBox="0 0 800 220" role="img" aria-label="${symbol}: ceny zamknięcia ostatnich ${bars.length} świec"><line x1="30" x2="770" y1="190" y2="190" stroke="#29344a"/><polyline points="${points}" fill="none" stroke="#a493ff" stroke-width="2"/><text x="30" y="215" fill="#97a6c0" font-size="11">${number(low)} – ${number(high)} USDT · ${bars.length} świec</text></svg>`;
    } else { el('market-price').textContent = '—'; el('price-chart').textContent = 'Oczekiwanie na świece…'; el('market-time').textContent = ''; el('market-change').textContent = ''; }
    const signals = selectedSignals();
    el('signal-time').textContent = signals?.fresh ? 'Aktualne · zamknięte świece' : 'Dane nieaktualne — sygnały wstrzymane';
    const labels = {BUY:'Warunek wejścia',SELL:'Warunek wyjścia',HOLD:'Brak nowego sygnału',UNAVAILABLE:'Oczekiwanie na aktualne dane'};
    el('signal-cards').innerHTML = (signals?.items || []).filter(s => s.symbol === symbol).map(s => `<article class="signal-card"><h3>${escape(s.strategy)}</h3><span class="signal-tag ${s.signal === 'BUY' ? 'positive' : s.signal === 'SELL' ? 'negative' : 'muted'}">${s.signal === 'UNAVAILABLE' ? '—' : escape(s.signal)}</span><p>${labels[s.signal] || '—'}</p></article>`).join('');
    estimate();
  }
  el('market-select').addEventListener('change', render);
  el('estimate-form').addEventListener('input', estimate);
  el('estimate-form').addEventListener('submit', event => event.preventDefault());
  window.renderTerminal = data => {
    snapshot = data;
    const symbols = Object.keys(data.terminal?.markets || {}), select = el('market-select');
    if (symbols.length && [...select.options].map(o => o.value).join(',') !== symbols.join(',')) {
      const previous = select.value;
      select.replaceChildren(...symbols.map(s => new Option(s.replace('USDT', ' / USDT'), s)));
      if (symbols.includes(previous)) select.value = previous;
    }
    render();
  };
  window.markTerminalOffline = () => {
    Object.values(snapshot?.terminal?.signals || {}).forEach(signals => {
      signals.fresh = false; signals.items.forEach(s => s.signal = 'UNAVAILABLE');
    });
    render();
  };
})();
