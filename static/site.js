const menuButton = document.querySelector('.index-button');
const index = document.querySelector('.record-index');
menuButton?.addEventListener('click', () => {
  const open = index?.classList.toggle('open');
  menuButton.setAttribute('aria-expanded', String(Boolean(open)));
});
document.querySelectorAll('.record-index a').forEach(a => a.addEventListener('click', () => {
  index?.classList.remove('open');
  menuButton?.setAttribute('aria-expanded', 'false');
}));

if ('IntersectionObserver' in window) {
  const reveal = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add('is-visible');
        reveal.unobserve(entry.target);
      }
    });
  }, {threshold: .16});
  document.querySelectorAll('.reveal-object').forEach(el => reveal.observe(el));

  const stamps = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add('stamp-hit');
        stamps.unobserve(entry.target);
      }
    });
  }, {threshold: .65});
  document.querySelectorAll('.ink-stamp').forEach(el => stamps.observe(el));
} else {
  document.querySelectorAll('.reveal-object').forEach(el => el.classList.add('is-visible'));
}

// Mobile navigation usability: close the battalion index when tapping outside
// or pressing Escape, and keep aria-expanded synchronized.
document.addEventListener('click', (event) => {
  if (!index?.classList.contains('open')) return;
  if (index.contains(event.target) || menuButton?.contains(event.target)) return;
  index.classList.remove('open');
  menuButton?.setAttribute('aria-expanded', 'false');
});
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape' || !index?.classList.contains('open')) return;
  index.classList.remove('open');
  menuButton?.setAttribute('aria-expanded', 'false');
  menuButton?.focus();
});

// Headquarters flash messages should confirm an action, not permanently occupy
// the right side of the screen. They remain manually dismissible and successful
// action confirmations clear automatically after a short reading window.
function dismissDispatch(el) {
  if (!el || el.classList.contains('is-dismissing')) return;
  el.classList.add('is-dismissing');
  window.setTimeout(() => el.remove(), 280);
}
document.querySelectorAll('[data-dispatch]').forEach((dispatch) => {
  dispatch.querySelector('.dispatch-close')?.addEventListener('click', () => dismissDispatch(dispatch));
  const isDanger = dispatch.classList.contains('danger');
  const delay = isDanger ? 10000 : 4500;
  window.setTimeout(() => dismissDispatch(dispatch), delay);
});


// Logged workspace smart-back control.
document.querySelectorAll('[data-smart-back]').forEach(btn=>btn.addEventListener('click',()=>{
  if(document.referrer && document.referrer.startsWith(location.origin)){ history.back(); } else { location.href='/'; }
}));
const rosterSearch=document.getElementById('s1-roster-search');
if(rosterSearch){ rosterSearch.addEventListener('input',()=>{ const q=rosterSearch.value.trim().toLowerCase(); document.querySelectorAll('#s1-roster-list [data-roster-search]').forEach(row=>{ row.hidden=q && !row.dataset.rosterSearch.toLowerCase().includes(q); }); }); }


// STAFF UX OVERHAUL
(() => {
  const drawer=document.querySelector('[data-soldier-drawer-panel]');
  const backdrop=document.querySelector('[data-soldier-drawer-backdrop]');
  const content=drawer?.querySelector('[data-soldier-drawer-content]');
  const closeDrawer=()=>{if(drawer)drawer.classList.remove('open');if(backdrop)backdrop.classList.remove('open');if(drawer)drawer.setAttribute('aria-hidden','true');document.body.classList.remove('staff-drawer-open');};
  const openDrawer=async(url)=>{
    if(!drawer||!content||!url)return;
    drawer.classList.add('open');backdrop?.classList.add('open');drawer.setAttribute('aria-hidden','false');document.body.classList.add('staff-drawer-open');
    content.innerHTML='<div class="drawer-loading"><b>OPENING PERSONNEL FILE…</b><span>Loading current assignment, weapon, and actions.</span></div>';
    try{
      const res=await fetch(url,{headers:{'X-Requested-With':'XMLHttpRequest'}});
      if(!res.ok)throw new Error(`HTTP ${res.status}`);
      content.innerHTML=await res.text();
    }catch(err){content.innerHTML='<div class="drawer-load-error"><b>PERSONNEL FILE COULD NOT BE LOADED.</b><span>Open the staff snapshot or retry.</span></div>';console.error(err);}
  };
  document.addEventListener('click',e=>{
    const trigger=e.target.closest('[data-soldier-action-url]');
    if(trigger){e.preventDefault();openDrawer(trigger.dataset.soldierActionUrl);return;}
    const legacy=e.target.closest('[data-soldier-drawer]');
    if(legacy?.dataset.record){e.preventDefault();openDrawer(legacy.dataset.record);}
  });
  document.querySelector('[data-soldier-drawer-close]')?.addEventListener('click',closeDrawer);
  backdrop?.addEventListener('click',closeDrawer);
  document.addEventListener('keydown',e=>{if(e.key==='Escape')closeDrawer();});
  const auto=document.querySelector('[data-auto-open-soldier]'); if(auto?.dataset.autoOpenSoldier) window.setTimeout(()=>openDrawer(auto.dataset.autoOpenSoldier),120);

  const cards=[...document.querySelectorAll('[data-personnel-card]')];
  const text=document.querySelector('[data-personnel-text-filter]'); let mode='all';
  const apply=()=>cards.forEach(card=>{const hay=(card.dataset.name||'').toLowerCase();const q=(text?.value||'').toLowerCase();const readiness=parseInt(card.dataset.readiness||'0',10);const activity=(card.dataset.activity||'').toUpperCase();let ok=!q||hay.includes(q);if(mode==='not-ready')ok=ok&&readiness<80;if(mode==='inactive')ok=ok&&(activity.includes('WATCH')||activity.includes('INACTIVE')||activity.includes('REVIEW'));if(mode==='promotion')ok=ok&&readiness>=80;card.classList.toggle('staff-filter-hidden',!ok);});
  document.querySelectorAll('[data-personnel-filter]').forEach(b=>b.addEventListener('click',()=>{mode=b.dataset.personnelFilter||'all';apply();}));text?.addEventListener('input',apply);

  const replacementRows=[...document.querySelectorAll('[data-replacement-row]')];
  const replacementSearch=document.querySelector('[data-replacement-search]'); let replacementStage='ALL';
  const applyReplacement=()=>{const q=(replacementSearch?.value||'').trim().toLowerCase();replacementRows.forEach(row=>{const stage=(row.dataset.stage||'').toUpperCase();const hay=(row.dataset.search||'').toLowerCase();row.hidden=(replacementStage!=='ALL'&&stage!==replacementStage)||!!(q&&!hay.includes(q));});};
  document.querySelectorAll('[data-replacement-stage]').forEach(btn=>btn.addEventListener('click',()=>{replacementStage=(btn.dataset.replacementStage||'ALL').toUpperCase();document.querySelectorAll('[data-replacement-stage]').forEach(x=>x.classList.toggle('active',x===btn));applyReplacement();}));
  replacementSearch?.addEventListener('input',applyReplacement);

  const routeMap={PERSONNEL:'S-1 Personnel',PROMOTION:'S-1 → Command',ASSIGNMENT:'S-1 Personnel',APPOINTMENT:'S-1 → Command',LEAVE:'S-1 Personnel',TRAINING:'S-3 Training',QUALIFICATION:'S-3 Training',MOS:'S-3 Training',OPERATION:'S-3 Operations',WEAPON:'S-4 Arms Room',EQUIPMENT:'S-4 Supply',LOGISTICS:'S-4 Supply','COMMAND REVIEW':'Battalion Headquarters'};
  document.querySelectorAll('[data-progressive-action-form]').forEach(form=>{const type=form.querySelector('[data-action-type]');const help=form.querySelector('[data-action-route-help]');const update=()=>{if(help&&type)help.textContent=`RECOMMENDED ROUTE: ${routeMap[type.value]||'Responsible staff office'} • Complete the details, review the action, then submit.`;};type?.addEventListener('change',update);update();});
})();


// S-3 operation form presets — website remains authoritative; Clerk executes tracking.
document.addEventListener('click', (event) => {
  const btn=event.target.closest('[data-op-template]'); if(!btn) return;
  const form=document.querySelector('[data-operation-wizard]'); if(!form) return;
  const preset=btn.dataset.opTemplate;
  const set=(name,val)=>{const el=form.querySelector(`[name="${name}"]`); if(el) el.value=val;};
  if(preset==='standard'){set('duration_minutes','90');set('credit_threshold_minutes','45');set('rounds_per_soldier','180');set('reminder_minutes','1440,120,30');set('formation_scope','BATTALION');}
  if(preset==='company'){set('duration_minutes','90');set('credit_threshold_minutes','45');set('rounds_per_soldier','180');set('reminder_minutes','1440,120,30');set('formation_scope','COMPANY');}
  if(preset==='training'){set('duration_minutes','60');set('credit_threshold_minutes','30');set('rounds_per_soldier','90');set('reminder_minutes','120,30');set('formation_scope','BATTALION');}
});

// CAV65 public-home tactile action feedback. Navigation remains native.
document.addEventListener('pointerdown', function (event) {
  const action = event.target.closest('.cav65-action, .cav65-staff-button, .cav65-float-cta');
  if (!action) return;
  action.classList.add('is-pressed');
  const clear = () => action.classList.remove('is-pressed');
  window.addEventListener('pointerup', clear, { once: true });
  window.addEventListener('pointercancel', clear, { once: true });
});


// 2026-08-22 — 1965 interactive battalion awards record.
(() => {
  const parseJson = id => { try { const el=document.getElementById(id); return el ? JSON.parse(el.textContent||'[]') : []; } catch(e){ return []; } };
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  const publicData=parseJson('public-ribbon-catalog-json');
  const publicModal=document.querySelector('[data-award-public-modal]');
  const openPublic=code => {
    if(!publicModal) return; const r=publicData.find(x=>x.ribbon_code===code); if(!r) return;
    const img=publicModal.querySelector('[data-award-detail-image]');
    if(img){ img.src=r.image_filename ? `/static/art/ribbons/${r.image_filename}` : ''; img.alt=r.ribbon_name||''; img.hidden=!r.image_filename; }
    publicModal.querySelector('[data-award-detail-name]').textContent=r.ribbon_name||'';
    publicModal.querySelector('[data-award-detail-type]').textContent=r.award_type_label||String(r.automation_mode||'').replaceAll('_',' ');
    publicModal.querySelector('[data-award-detail-description]').textContent=r.description_text||r.requirement_text||'';
    publicModal.querySelector('[data-award-detail-earning]').textContent=r.earning_text||r.requirement_text||'';
    publicModal.querySelector('[data-award-detail-requirement]').textContent=r.requirement_text||'';
    publicModal.setAttribute('aria-hidden','false'); document.body.classList.add('award-modal-open');
  };
  document.querySelectorAll('[data-award-public-card]').forEach(el=>{
    el.addEventListener('click',()=>openPublic(el.dataset.ribbonCode));
    el.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();openPublic(el.dataset.ribbonCode);}});
  });
  document.querySelectorAll('[data-award-public-close]').forEach(el=>el.addEventListener('click',()=>{if(publicModal)publicModal.setAttribute('aria-hidden','true');document.body.classList.remove('award-modal-open');}));

  const memberData=parseJson('member-ribbon-details-json');
  const memberModal=document.querySelector('[data-member-award-modal]');
  const openMember=code => {
    if(!memberModal) return; const r=memberData.find(x=>x.ribbon_code===code); if(!r) return;
    const img=memberModal.querySelector('[data-member-award-image]');
    if(img){img.src=r.image_filename?`/static/art/ribbons/${r.image_filename}`:'';img.alt=r.ribbon_name||'';img.hidden=!r.image_filename;}
    memberModal.querySelector('[data-member-award-name]').textContent=r.ribbon_name||'';
    memberModal.querySelector('[data-member-award-type]').textContent=r.award_type_label||String(r.automation_mode||'').replaceAll('_',' ');
    memberModal.querySelector('[data-member-award-status]').textContent=r.earned ? `AUTHORIZED • ${r.earned_at||'DATE ON FILE'}${r.is_worn?' • WORN':' • NOT WORN'}` : 'NOT YET AUTHORIZED';
    memberModal.querySelector('[data-member-award-device]').textContent=r.earned ? `${r.award_count||1} AWARD${Number(r.award_count||1)===1?'':'S'} • ${r.device_label||'NO DEVICE'}` : '';
    memberModal.querySelector('[data-member-award-progress]').textContent=r.progress_detail||r.requirement_text||'';
    const bar=memberModal.querySelector('[data-member-award-progress-bar]'); if(bar) bar.style.width=`${Math.max(0,Math.min(100,Number(r.progress_percent||0)))}%`;
    memberModal.querySelector('[data-member-award-description]').textContent=r.description_text||r.requirement_text||'';
    memberModal.querySelector('[data-member-award-earning]').textContent=r.earning_text||r.requirement_text||'';
    memberModal.querySelector('[data-member-award-requirement]').textContent=r.requirement_text||'';
    const hist=memberModal.querySelector('[data-member-award-history]');
    if(hist){
      if(r.history&&r.history.length) hist.innerHTML=r.history.map((h,i)=>`<p><b>${i+1}${i===0?'ST':i===1?'ND':i===2?'RD':'TH'} AWARD • ${esc(h.award_date||'DATE ON FILE')}</b><span>${esc(h.order_number||'')}</span><small>${esc(h.citation||'CITATION NOT ENTERED')}</small></p>`).join('');
      else if(r.earned) hist.innerHTML=`<p><b>1ST AWARD • ${esc(r.earned_at||'DATE ON FILE')}</b><span>AUTHORIZATION FILED IN 201 RECORD</span><small>${esc(r.progress_detail||r.requirement_text||'')}</small></p>`;
      else hist.innerHTML='<p><b>NO AWARD ON FILE</b><small>Qualification progress is shown above.</small></p>';
    }
    memberModal.setAttribute('aria-hidden','false');document.body.classList.add('award-modal-open');
  };
  document.querySelectorAll('[data-member-ribbon]').forEach(el=>el.addEventListener('click',e=>{e.preventDefault();e.stopPropagation();openMember(el.dataset.memberRibbon);}));
  document.querySelectorAll('[data-member-award-close]').forEach(el=>el.addEventListener('click',()=>{if(memberModal)memberModal.setAttribute('aria-hidden','true');document.body.classList.remove('award-modal-open');}));
  document.addEventListener('keydown',e=>{if(e.key==='Escape'){[publicModal,memberModal].forEach(m=>m&&m.setAttribute('aria-hidden','true'));document.body.classList.remove('award-modal-open');}});
})();
