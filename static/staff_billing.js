(function(){
  const $ = sel => document.querySelector(sel);
  const $$ = sel => Array.from(document.querySelectorAll(sel));

  const KEY = 'staff_cart';

  function getStaffCart(){
    try { return JSON.parse(localStorage.getItem(KEY)) || []; }
    catch(_) { return []; }
  }
  function setStaffCart(items){
    localStorage.setItem(KEY, JSON.stringify(items || []));
  }
  function clearStaffCart(){
    try { localStorage.removeItem(KEY); } catch(_) {}
  }
  function addToStaffCart(name, price, qty){
    const items = getStaffCart();
    const q = Number(qty || 0);
    const p = Number(price || 0);
    if (!name || q <= 0 || p < 0) return;
    const existing = items.find(i => i.productName === name && Number(i.price) === p);
    if (existing) existing.quantity = Number(existing.quantity || 0) + q;
    else items.push({ productName: name, price: p, quantity: q });
    setStaffCart(items);
  }

  function fmt(n){ n = Number(n||0); return isNaN(n) ? '0.00' : n.toFixed(2); }

  function renderBill(){
    const lines = getStaffCart().map(i => {
      const name = String(i.productName||'');
      const unit = name.toLowerCase().includes('(bag)') ? 'bag' : 'yd3';
      return {
        item_name: name.replace(/ \((bag|yd)\)$/i, ''),
        unit,
        quantity: Number(i.quantity||0),
        unit_price: Number(i.price||0),
        line_total: Number(i.price||0) * Number(i.quantity||0),
      };
    }).filter(li => li.quantity>0 && li.unit_price>=0);

    const c = $('#bill-lines'); if (c) c.innerHTML = '';
    let subtotal = 0;
    lines.forEach(li => {
      subtotal += li.line_total;
      if (!c) return;
      const row = document.createElement('div');
      row.style.display = 'flex';
      row.style.justifyContent = 'space-between';
      row.innerHTML = `<span>${li.item_name} x ${li.quantity} ${li.unit}</span><span>$${fmt(li.line_total)}</span>`;
      c.appendChild(row);
    });
    const sub = $('#bill-subtotal'); if (sub) sub.textContent = fmt(subtotal);
    return lines;
  }

  async function saveAndPrint(){
    const lines = renderBill();
    if (!lines.length){ alert('Bill is empty'); return; }
    const payload = {
      customer_name: ($('#customer_name')?.value||'').trim() || null,
      customer_phone: ($('#customer_phone')?.value||'').trim() || null,
      customer_address: ($('#customer_address')?.value||'').trim() || null,
      customer_lat: (document.getElementById('cust_lat')?.value||null),
      customer_lng: (document.getElementById('cust_lng')?.value||null),
      notes: 'Aggregates',
      lines: lines.map(({item_name, unit, quantity, unit_price}) => ({ item_name, unit, quantity, unit_price }))
    };
    const r = await fetch('/api/staff/receipts', {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)
    });
    const j = await r.json().catch(()=>({}));
    if (!r.ok || !j.ok){ alert(j.error||'Failed to save'); return; }
    const wa = j.wa || {};
    if (wa.sent_text){
      alert('WhatsApp sent to dispatch' + (wa.sent_location_queued ? ' (location queued)' : ''));
    }else{
      alert('Saved. WhatsApp failed' + (wa.error ? `: ${wa.error}` : ''));
    }
    clearStaffCart();
    window.open(`/staff/receipts/${j.id}/print`, '_blank');
  }

  document.addEventListener('DOMContentLoaded', function(){
    // Bind staff product cards (do NOT use window.addToCart/customer cart)
    const cards = Array.from(document.querySelectorAll('.product-card'));
    cards.forEach(card => {
      const qtyInput = card.querySelector('.qty');
      const inc = card.querySelector('.qty-inc');
      const dec = card.querySelector('.qty-dec');
      const add = card.querySelector('.add');
      // Add a simple unit toggle (Yard/Bag) if not present
      let unitSel = card.querySelector('.unit-select');
      if (!unitSel){
        unitSel = document.createElement('select');
        unitSel.className = 'unit-select';
        unitSel.innerHTML = '<option value="yd3">Yard</option><option value="bag">Bag</option>';
        const desc = card.querySelector('.desc') || card;
        desc.parentNode.insertBefore(unitSel, desc.nextSibling);
      }
      const clamp = v => { const n = parseFloat(v); return (isNaN(n) || n <= 0) ? 0.25 : Math.round(n * 100) / 100; };
      if (inc) inc.addEventListener('click', () => { const base = parseFloat(qtyInput.value||'1')||1; qtyInput.value = clamp(base + 0.25); });
      if (dec) dec.addEventListener('click', () => { const base = parseFloat(qtyInput.value||'1')||1; const next = base - 0.25; qtyInput.value = clamp(next < 0.25 ? 0.25 : next); });
      if (add) add.addEventListener('click', () => {
        const name = String(card.getAttribute('data-name')||'');
        const price = Number(card.getAttribute('data-price')||0);
        let qty = clamp(qtyInput ? qtyInput.value : 1);
        const unit = (unitSel && unitSel.value) || 'yd3';
        // If Bag, force integer qty and use bag price overrides
        if (unit === 'bag'){
          qty = Math.max(1, Math.round(qty));
          const lname = name.trim().toLowerCase();
          if (lname === 'sand'){
            addToStaffCart(name + ' (bag)', 35, qty);
          }else if (lname === 'gravel'){
            addToStaffCart(name + ' (bag)', 45, qty);
          }else if (lname === 'sharp sand'){
            addToStaffCart(name + ' (bag)', 50, qty);
          }else{
            addToStaffCart(name + ' (bag)', price, qty);
          }
        }else{
          addToStaffCart(name + ' (yd)', price, qty);
        }
        renderBill();
      });
    });

    renderBill();
    $('#btn-save-receipt')?.addEventListener('click', saveAndPrint);
  });
})();


