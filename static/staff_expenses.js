(function(){
  async function postJSON(url, data){
    const r = await fetch(url, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data) });
    const j = await r.json().catch(()=>({}));
    if (!r.ok || !j.ok) throw new Error(j.error||'Request failed');
    return j;
  }

  document.addEventListener('DOMContentLoaded', function(){
    const btn = document.getElementById('btn-exp-save');
    if (!btn) return;
    btn.addEventListener('click', async function(){
      try{
        const payload = {
          date: (document.getElementById('exp-date')?.value||'').trim()||null,
          category: (document.getElementById('exp-category')?.value||'').trim(),
          description: (document.getElementById('exp-desc')?.value||'').trim()||null,
          amount: parseFloat(document.getElementById('exp-amount')?.value||'0')||0,
        };
        await postJSON('/api/staff/expenses', payload);
        window.location.href = '/staff/expenses';
      }catch(e){ alert(e.message||'Save failed'); }
    });

    async function uploadFiles(){
      const input = document.getElementById('exp-files');
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
      const c = document.getElementById('exp-uploaded');
      if (!c) return;
      c.innerHTML = '';
      list.forEach(f => {
        const a = document.createElement('a');
        a.href = f.url; a.textContent = f.filename; a.target = '_blank';
        a.style.display = 'block';
        c.appendChild(a);
      });
    }

    async function onExtract(){
      try{
        const anchors = Array.from(document.getElementById('exp-uploaded').querySelectorAll('a'));
        const ids = anchors.map(a => a.textContent);
        if (!ids.length) { alert('Upload a file first.'); return; }
        const j = await postJSON('/api/staff/expenses/extract', { file_ids: ids });
        const data = j.data||{}; const items = data.expenses||[];
        if (data.date && !document.getElementById('exp-date').value){ document.getElementById('exp-date').value = data.date; }
        if (items.length){
          // If single item, populate fields; else show summary prompt
          if (items.length === 1){
            const it = items[0];
            if (it.category) document.getElementById('exp-category').value = it.category;
            if (it.description) document.getElementById('exp-desc').value = it.description;
            if (typeof it.amount === 'number') document.getElementById('exp-amount').value = it.amount;
          } else {
            alert('AI extracted multiple expenses. Please enter them individually.');
          }
        }
      }catch(e){ alert(e.message||'AI extract failed'); }
    }

    async function onParseText(){
      try{
        const text = (document.getElementById('exp-ai-text')?.value||'').trim();
        if (!text) return;
        const j = await postJSON('/api/staff/expenses/ai-parse-text', { text });
        const data = j.data||{}; const items = data.expenses||[];
        if (data.date && !document.getElementById('exp-date').value){ document.getElementById('exp-date').value = data.date; }
        if (items.length){
          if (items.length === 1){
            const it = items[0];
            if (it.category) document.getElementById('exp-category').value = it.category;
            if (it.description) document.getElementById('exp-desc').value = it.description;
            if (typeof it.amount === 'number') document.getElementById('exp-amount').value = it.amount;
          } else {
            alert('AI parsed multiple expenses. Please enter them individually.');
          }
        }
      }catch(e){ alert(e.message||'AI parse failed'); }
    }

    const up = document.getElementById('btn-exp-upload');
    if (up) up.addEventListener('click', async () => { try{ const files = await uploadFiles(); renderUploaded(files); }catch(e){ alert(e.message); } });
    const ex = document.getElementById('btn-exp-extract');
    if (ex) ex.addEventListener('click', onExtract);
    const txt = document.getElementById('btn-exp-parse-text');
    if (txt) txt.addEventListener('click', onParseText);
  });
})();


