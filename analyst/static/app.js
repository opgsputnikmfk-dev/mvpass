'use strict';
const $=s=>document.querySelector(s), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const n=(x,d=2)=>x==null?'—':Number(x).toLocaleString('ru-RU',{maximumFractionDigits:d,minimumFractionDigits:d});
const dt=x=>x?new Date(x).toLocaleString('ru-RU',{timeZone:'UTC'})+' UTC':'—';
let state=null,draft=null,dirty=false,tab='overview';
async function api(url,data){const r=await fetch(url,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json','X-Requested-With':'Analyst'}:{},body:data?JSON.stringify(data):undefined});const result=await r.json();if(!r.ok)throw new Error(result.error||'Ошибка запроса');return result;}
function error(e){$('#error').textContent=e.message;$('#error').classList.remove('hidden');}
const login=$('#login');
if(login){login.addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/login',{password:$('#password').value});location.reload();}catch(e){error(e);}});}
else{
 document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>{tab=b.dataset.tab;document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('hidden',x.id!==tab));document.querySelectorAll('.nav').forEach(x=>x.classList.toggle('active',x===b));$('#page-title').textContent=b.textContent;}));
 $('#refresh').onclick=refresh;$('#pause').onclick=async()=>{try{await api('/api/pause',{});dirty=false;await refresh();}catch(e){error(e);}};
 $('#save-filters').onclick=()=>save(draft);$('#save-json').onclick=()=>{try{save(JSON.parse($('#config-json').value));}catch(e){error(e);}};
 $('#config-json').addEventListener('input',()=>{dirty=true;try{draft=JSON.parse($('#config-json').value);}catch(e){/* Save reports invalid JSON without discarding the draft. */}});
 $('#close-detail').onclick=()=>$('#detail').close();refresh();setInterval(refresh,15000);
}
async function save(c){try{await api('/api/config',c);dirty=false;await refresh();}catch(e){error(e);}}
async function refresh(){try{state=await api('/api/state');$('#error').classList.add('hidden');if(!dirty){draft=structuredClone(state.config);$('#config-json').value=JSON.stringify(draft,null,2);}render();}catch(e){error(e);}}
function table(headers,rows){return rows.length?'<table><thead><tr>'+headers.map(h=>'<th>'+esc(h)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+r.map(c=>'<td>'+c+'</td>').join('')+'</tr>').join('')+'</tbody></table>':'<div class="empty">Пока нет данных для этой серии.</div>';}
function render(){
 const key=state.generation+':active',a=state.accounts[key],s=a?.stats||{};
 $('#status').textContent=state.dashboard?.status_label||state.status;$('#pause').textContent=state.config.paused?'Продолжить':'Пауза';
 $('#notice').textContent=state.mode==='SYNTHETIC_DEMO'?'ДЕМОНСТРАЦИЯ · Искусственные свечи для проверки программы. Эти результаты не показывают доходность на рынке.':'PAPER · Все сделки учебные. Расчёт использует модель исполнения; преимущество стратегии пока не подтверждено.';
 $('#updated').textContent='Данные: '+dt(state.updated_at);$('#series-label').textContent=state.generation.slice(0,8);$('#export').href='/api/export?account='+encodeURIComponent(key);
 $('#metrics').innerHTML=[['Учебный капитал',n(s.equity)+' USDT','Начальный: '+n(state.config.risk.initial_equity)],['Результат',n(s.return_pct)+'%','С учётом модели расходов'],['Просадка',n(s.max_drawdown_pct)+'%','По минутной переоценке'],['Закрытые сделки',n(s.trades,0),'Прибыльных: '+n(s.win_rate,1)+'%']].map(([label,value,note])=>'<div class="metric"><label>'+esc(label)+'</label><strong>'+esc(value)+'</strong><small>'+esc(note)+'</small></div>').join('');
 const curve=a?.curve||[];if(curve.length>1){const yy=curve.map(x=>x[1]),lo=Math.min(...yy),hi=Math.max(...yy),w=700,h=180;const pts=yy.map((y,i)=>(i/(yy.length-1)*w).toFixed(1)+','+(h-12-(y-lo)/(hi-lo||1)*(h-24)).toFixed(1)).join(' ');$('#equity').innerHTML='<svg viewBox="0 0 700 190" role="img" aria-label="График учебного капитала"><polyline points="'+pts+'" fill="none" stroke="#78e5c2" stroke-width="2"/></svg>';}else $('#equity').innerHTML='<p class="empty">График появится после начала наблюдения.</p>';
 $('#market').innerHTML=Object.entries(state.market).map(([sym,m])=>'<div class="market-row"><div><b>'+esc(sym)+'</b><small>'+esc(m.detail||m.status)+'</small></div><div>'+n(m.price,4)+'<small>'+(m.direction===1?'Восходящее':m.direction===-1?'Нисходящее':'Неопределённое')+'</small></div></div>').join('')||'<p class="empty">Ожидание биржевых данных…</p>';
 $('#positions').innerHTML=table(['Монета','Сторона','Вход','Стоп','Цель','Риск USDT'],(a?.positions||[]).map(p=>[esc(p.symbol),p.side===1?'LONG':'SHORT',n(p.entry,6),n(p.stop,6),n(p.target,6),n(p.planned_risk)]));
 $('#trades').innerHTML=table(['Монета','Выход UTC','Итог USDT','Результат R','Причина'],(a?.trades||[]).slice(-10).reverse().map(t=>[esc(t.symbol),esc(dt(t.exit_ts)),'<span class="'+(t.net_pnl>=0?'positive':'negative')+'">'+n(t.net_pnl)+'</span>',n(t.net_r),esc(t.reason)]));
 renderReport();
 renderFilters();
 $('#entry-rejections').innerHTML=table(['Время UTC','ID сценария','Причина'],(a?.rejections||[]).slice(-15).reverse().map(r=>[esc(dt(r.ts)),esc(r.candidate_id.slice(0,10)),esc(r.reason)]));
 $('#signal-list').innerHTML=state.candidates.map(c=>{const d=c.decisions.find(x=>x.account===key);return '<article class="panel signal"><div><b>'+esc(c.symbol)+' · '+(c.side===1?'LONG':'SHORT')+'</b><small>'+esc(state.setup_names[c.setup]||c.setup)+' · '+esc(dt(c.ts))+'</small><p>'+esc(d?(d.accepted?'Допущен к проверке исполнения':d.reasons.join('; ')):'Другая серия настроек')+'</p></div><button class="button secondary" data-candidate="'+esc(c.id)+'">Разобрать ↗</button></article>';}).join('')||'<div class="panel empty">Найденные ситуации появятся здесь, включая отклонённые.</div>';
 document.querySelectorAll('[data-candidate]').forEach(b=>b.onclick=()=>details(b.dataset.candidate));
 $('#logs').innerHTML=state.logs.map(l=>'<div class="log-row"><span class="muted">'+esc(dt(l.ts))+' · '+esc(l.level)+'</span><br>'+esc(l.message)+'</div>').join('')||'<p class="muted">Журнал пуст.</p>';
}
function renderFilters(){
 const labels={on:'Включён',observe:'Наблюдение',off:'Выключен'};
 const attribution=state.dashboard?.filter_attribution||{};
 const group=k=>{const b=attribution[k];return b||{pass:{n:0,mean_r:null},fail:{n:0,mean_r:null}}};
 const sample=b=>b.n?n(b.mean_r,2)+' R · '+b.n+' сделок':'Пока нет сделок';
 $('#filter-grid').innerHTML=table(['Что проверяет фильтр','Режим','Когда пройден','Когда не пройден'],Object.entries(state.filter_names).map(([k,name])=>[esc(name),'<button class="mode '+draft.filters[k]+'" data-filter="'+k+'">'+labels[draft.filters[k]]+'</button>',sample(group(k).pass),sample(group(k).fail)]));
 document.querySelectorAll('[data-filter]').forEach(b=>b.onclick=()=>{const k=b.dataset.filter;draft.filters[k]={off:'observe',observe:'on',on:'off'}[draft.filters[k]];dirty=true;renderFilters();});
 $('#setups').innerHTML=Object.entries(state.setup_names).map(([k,v])=>'<label><input type="checkbox" data-setup="'+k+'" '+(draft.setups.includes(k)?'checked':'')+'> '+esc(v)+'</label>').join('');
 document.querySelectorAll('[data-setup]').forEach(b=>b.onchange=()=>{draft.setups=Array.from(document.querySelectorAll('[data-setup]:checked')).map(x=>x.dataset.setup);dirty=true;});
 $('#dirty').textContent=dirty?'Есть несохранённые изменения.':'Сохранение создаёт новую серию. Открытые сделки сохраняют прежние правила.';
}
function details(id){const c=state.candidates.find(x=>x.id===id);if(!c)return;$('#detail-body').innerHTML='<h2>'+esc(c.symbol)+' · '+(c.side===1?'LONG':'SHORT')+'</h2><p>'+esc(c.explanation)+'</p><p>Предварительный вход '+n(c.reference,6)+' · Стоп '+n(c.stop,6)+' · Цель '+n(c.target,6)+'</p><img src="/api/chart?candidate='+encodeURIComponent(c.id)+'" alt="График цены с уровнями и Фибоначчи"><div class="scroll">'+table(['Проверка','Результат','Объяснение'],Object.entries(c.checks).map(([k,ch])=>[esc(state.filter_names[k]),ch.passed==null?'Нет данных':ch.passed?'<span class="positive">Пройдена</span>':'<span class="negative">Не пройдена</span>',esc(ch.detail)]))+'</div>';$('#detail').showModal();}

let selectedSeries=null,selectedAccount=null;
function renderReport(){
 const d=state.dashboard;if(!d)return;
 const blocked=state.status==='DATA_ERROR';
 $('#bot-health').innerHTML='<div class="panel-heading"><h2>'+esc(d.status_label)+'</h2><span class="pill">'+(state.mode==='SYNTHETIC_DEMO'?'Демонстрация':'Учебный режим')+'</span></div><p>'+(blocked?'Биржа не отдаёт данные. Новые сигналы не формируются. Нулевые результаты сейчас не означают, что фильтры плохие.':state.config.paused?'Новые входы приостановлены. Открытые учебные сделки продолжают сопровождаться.':d.fresh?'Данные обновляются. Бот ждёт подходящие условия входа.':'Свежий анализ ещё не получен. Бот подключается или готовит историю.')+'</p>'+(blocked&&d.last_error?'<p class="muted">Причина: '+esc(d.last_error.message)+'</p>':'');
 $('#coin-count').textContent=d.coins.length+' монеты';
 $('#coins-table').innerHTML=table(['Монета','Состояние','Последняя проверка','Найдено ситуаций','Цена'],d.coins.map(c=>[esc(c.symbol),esc(c.status),esc(dt(c.last_analysis)),n(c.activity.candidates,0),n(c.market.price,5)]));
 const groups=[...new Set(d.comparisons.map(c=>c.group))];
 if(!groups.includes(selectedSeries))selectedSeries=state.generation;
 $('#series-choice').innerHTML=groups.map(g=>{const c=d.comparisons.find(c=>c.group===g);return '<option value="'+esc(g)+'" '+(g===selectedSeries?'selected':'')+'>Серия '+c.series+(g===state.generation?' · текущая':' · архив')+'</option>'}).join('');
 $('#series-choice').onchange=e=>{selectedSeries=e.target.value;selectedAccount=null;renderReport()};
 const rows=d.comparisons.filter(c=>c.group===selectedSeries);
 $('#comparison').innerHTML=table(['Набор','Включённые фильтры','Проверено / допущено','Сделки','Итог USDT','Прибыльных','Просадка'],rows.map(c=>[esc(c.name),esc(c.filters.join(' + ')||'Только базовые сценарии'),n(c.activity.checked,0)+' / '+n(c.activity.accepted,0),n(c.stats.trades,0),n(c.realized_pnl),n(c.stats.win_rate,1)+'%',n(c.stats.max_drawdown_pct)+'%']));
 if(!rows.some(c=>c.key===selectedAccount))selectedAccount=rows.find(c=>c.key.endsWith(':active'))?.key||rows[0]?.key;
 $('#account-choice').innerHTML=rows.map(c=>'<option value="'+esc(c.key)+'" '+(c.key===selectedAccount?'selected':'')+'>'+esc(c.name)+'</option>').join('');
 $('#account-choice').onchange=e=>{selectedAccount=e.target.value;renderReport()};
 const c=rows.find(c=>c.key===selectedAccount);if(!c)return;
 const breakdown=obj=>table(['Название','Сделки','Итог USDT','Удачных'],Object.entries(obj).map(([k,v])=>[esc(k),n(v.trades,0),n(v.net_pnl),n(v.trades?100*v.wins/v.trades:null,1)+'%']));
 $('#account-detail').innerHTML='<p><b>Состав:</b> '+esc(c.filters.join(' + ')||'Без дополнительных фильтров')+'</p><p><b>Наблюдение без влияния на вход:</b> '+esc(c.observed.join(', ')||'нет')+'</p><p><b>Сценарии:</b> '+esc(c.setups.join(', ')||'выключены')+'</p><p class="muted">Наблюдение за ситуациями: '+esc(dt(c.activity.start))+' — '+esc(dt(c.activity.end))+'</p><p>'+esc(c.sample_note)+'</p><p>Закрытые сделки: '+n(c.stats.trades,0)+' · результат '+n(c.stats.return_pct)+'% · средняя сделка '+n(c.stats.mean_r,3)+' R</p><a class="button secondary" href="/api/export?account='+encodeURIComponent(c.key)+'" download="combination.csv">Скачать сделки этого набора</a><h3>По монетам</h3><div class="scroll">'+breakdown(c.by_symbol)+'</div><h3>По сценариям входа</h3><div class="scroll">'+breakdown(c.by_setup)+'</div>';
}
