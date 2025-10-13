(function(){
  const $ = sel => document.querySelector(sel);
  const $$ = sel => Array.from(document.querySelectorAll(sel));

  function fmt(n){ n = Number(n||0); return isNaN(n)? '0.00' : n.toFixed(2); }

  function addRow(li){
    const tbody = $('#lines tbody');
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><input class="desc" type="text" value="${(li && (li.description||''))||''}"/></td>
      <td><input class="unit" type="text" value="${(li && (li.unit||''))||''}" placeholder="yd3"/></td>
      <td><input class="qty" type="number" step="0.01" value="${(li && (li.qty||li.quantity||0))||0}"/></td>
      <td><input class="unit_price" type="number" step="0.01" value="${(li && (li.unit_price||li.price||''))||''}"/></td>
      <td class="line_total">${fmt((li && (li.line_total))||0)}</td>
      <td><button class="btn btn-del" type="button">✕</button><input type="hidden" class="line_id" value="${(li && (li.id||''))||''}"></td>
    `;
    tbody.appendChild(tr);
    bindRow(tr);
    recompute();
  }

  function bindRow(tr){
    const qty = tr.querySelector('.qty');
    const up = tr.querySelector('.unit_price');
    const del = tr.querySelector('.btn-del');
    function update(){
      const q = parseFloat(qty.value||0) || 0;
      const p = parseFloat(up.value||0) || 0;
      const lt = q * p;
      tr.querySelector('.line_total').textContent = fmt(lt);
      recompute();
    }
    qty.addEventListener('input', update);
    up.addEventListener('input', update);
    if (del) del.addEventListener('click', () => { tr.remove(); recompute(); });
  }

  function getLines(){
    return $$('#lines tbody tr').map(tr => ({
      id: (function(v){ v = tr.querySelector('.line_id')?.value||''; return v? Number(v): undefined; })(),
      description: tr.querySelector('.desc').value.trim(),
      unit: tr.querySelector('.unit').value.trim() || 'yd3',
      qty: parseFloat(tr.querySelector('.qty').value||0) || 0,
      unit_price: (function(v){ v = parseFloat(v); return isNaN(v) ? undefined : v; })(tr.querySelector('.unit_price').value),
      line_total: (function(t){ t = t.textContent||'0'; const n = parseFloat(t); return isNaN(n) ? undefined : n; })(tr.querySelector('.line_total')),
    })).filter(li => li.description && li.qty > 0);
  }

  function recompute(){
    const sum = getLines().reduce((acc, li) => acc + (Number(li.line_total||0)), 0);
    $('#subtotal').textContent = fmt(sum);
    const tax = parseFloat($('#tax').value||0) || 0;
    $('#total').textContent = fmt(sum + tax);
  }

  async function postJSON(url, data){
    const r = await fetch(url, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data) });
    const j = await r.json().catch(()=>({}));
    if (!r.ok || !j.ok) throw new Error(j.error || r.statusText);
    return j;
  }

  async function uploadFiles(){
    const input = $('#files');
    const files = Array.from(input.files||[]);
    if (!files.length) return [];
    const fd = new FormData();
    files.forEach(f => fd.append('file', f));
    const resp = await fetch('/api/uploads', { method:'POST', body: fd });
    const j = await resp.json();
    if (!resp.ok || !j.ok) throw new Error(j.error||'Upload failed');
    return j.files || [];
  }

  function renderUploaded(list){
    const c = $('#uploaded');
    c.innerHTML = '';
    list.forEach(f => {
      const a = document.createElement('a');
      a.href = f.url; a.textContent = f.filename; a.target = '_blank';
      a.style.display = 'block';
      c.appendChild(a);
    });
  }

  function mergeAIData(data){
    if (data.supplier_name) $('#supplier_name').value = data.supplier_name;
    if (data.invoice_number) $('#invoice_number').value = data.invoice_number;
    if (data.invoice_date) $('#invoice_date').value = data.invoice_date;
    const lines = data.lines||[];
    lines.forEach(addRow);
    recompute();
  }

  async function onExtract(){
    const anchors = Array.from($('#uploaded').querySelectorAll('a'));
    const ids = anchors.map(a => a.textContent);
    if (!ids.length) { alert('Upload the bill first.'); return; }
    try{
      const j = await postJSON('/api/staff/purchases/extract', { file_ids: ids });
      mergeAIData(j.data||{});
    }catch(e){ alert('AI extract failed: '+ e.message); }
  }

  async function onParseText(){
    const text = ($('#ai_text').value||'').trim();
    if (!text) return;
    try{
      const j = await postJSON('/api/staff/purchases/ai-parse-text', { text });
      mergeAIData(j.data||{});
    }catch(e){ alert('AI parse failed: '+ e.message); }
  }

  async function onSave(){
    const lines = getLines();
    if (!lines.length) { alert('Add at least one line'); return; }
    try{
      const payload = {
        id: (window.__INVOICE__ && window.__INVOICE__.id) ? window.__INVOICE__.id : undefined,
        supplier_name: ($('#supplier_name').value||'').trim() || null,
        invoice_number: ($('#invoice_number').value||'').trim() || null,
        invoice_date: ($('#invoice_date').value||'').trim() || null,
        currency: 'TTD',
        uploaded_files: Array.from($('#uploaded').querySelectorAll('a')).map(a => a.textContent),
        status: 'draft',
        tax: parseFloat($('#tax').value||0)||0,
        lines,
      };
      const j = await postJSON('/api/staff/purchases', payload);
      window.location.href = '/staff/purchases';
    }catch(e){ alert('Save failed: '+ e.message); }
  }

  // Bind
  document.addEventListener('DOMContentLoaded', function(){
    // Prepopulate if editing
    try{
      if (window.__INVOICE__ && window.__INVOICE__.id){
        const inv = window.__INVOICE__;
        if (inv.supplier_name) $('#supplier_name').value = inv.supplier_name;
        if (inv.invoice_number) $('#invoice_number').value = inv.invoice_number;
        if (inv.invoice_date) $('#invoice_date').value = inv.invoice_date;
        const lines = inv.lines || [];
        lines.forEach(addRow);
        recompute();
      }
    }catch(_){ }
    $('#btn-add-line').addEventListener('click', () => addRow());
    $('#btn-upload').addEventListener('click', async () => {
      try{ const files = await uploadFiles(); renderUploaded(files); }catch(e){ alert(e.message); }
    });
    $('#btn-extract').addEventListener('click', onExtract);
    $('#btn-parse-text').addEventListener('click', onParseText);
    $('#btn-save').addEventListener('click', onSave);
    $('#tax').addEventListener('input', recompute);
  });
})();


