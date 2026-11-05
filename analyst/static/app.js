'use strict';
const $=s=>document.querySelector(s), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const n=(x,d=2)=>x==null?'—':Number(x).toLocaleString('ru-RU',{maximumFractionDigits:d,minimumFractionDigits:d});
const dt=x=>x?new Date(x).toLocaleString('ru-RU',{timeZone:'UTC'})+' UTC':'—';
let openPosition=null;
let state=null,draft=null,dirty=false,tab='overview',chartPeriod='all';
async function api(url,data){const r=await fetch(url,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json','X-Requested-With':'Analyst','X-Account':state?.user?.username||''}:{},body:data?JSON.stringify(data):undefined});const result=await r.json();if(!r.ok)throw new Error(result.error||'Ошибка запроса');return result;}
function error(e){$('#error').textContent=e.message;$('#error').classList.remove('hidden');}
const login=$('#login');
if(login){login.addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/login',{username:$('#username').value,password:$('#password').value});location.reload();}catch(e){error(e);}});}
else{
 document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>{tab=b.dataset.tab;document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('hidden',x.id!==tab));document.querySelectorAll('.nav').forEach(x=>x.classList.toggle('active',x===b));$('#page-title').textContent=tab==='overview'?'Обзор рынка':b.querySelector('span:last-child').textContent.trim();}));
 document.querySelectorAll('[data-open-tab]').forEach(b=>b.onclick=()=>document.querySelector('[data-tab="'+b.dataset.openTab+'"]').click());
 document.querySelectorAll('[data-period]').forEach(b=>b.onclick=()=>{chartPeriod=b.dataset.period;renderEquity();});
 $('#logout').onclick=async()=>{await api('/api/logout',{});location.reload();};
 $('#refresh').onclick=refresh;$('#pause').onclick=async()=>{try{await api('/api/pause',{});dirty=false;await refresh();}catch(e){error(e);}};
 $('#save-filters').onclick=()=>save(draft);$('#save-json').onclick=()=>{try{save(JSON.parse($('#config-json').value));}catch(e){error(e);}};
 $('#config-json').addEventListener('input',()=>{dirty=true;try{draft=JSON.parse($('#config-json').value);}catch(e){/* Save reports invalid JSON without discarding the draft. */}});
 $('#close-detail').onclick=()=>{openPosition=null;$('#detail').close();};
 document.addEventListener('click',e=>{const b=e.target.closest('[data-position]')||e.target.closest('tr')?.querySelector('[data-position]');if(b)positionDetails(b.dataset.account,b.dataset.position);});refresh();setInterval(refresh,15000);
}
async function save(c){try{await api('/api/config',c);dirty=false;await refresh();}catch(e){error(e);}}
async function refresh(){try{const next=await api('/api/state');if(state?.user?.username!==next.user?.username){dirty=false;draft=null;}state=next;$('#error').classList.add('hidden');if(!dirty){draft=structuredClone(state.config);$('#config-json').value=JSON.stringify(draft,null,2);}render();if(openPosition&&$('#detail').open)positionDetails(openPosition.account,openPosition.id,true);}catch(e){error(e);}}
function table(headers,rows){return rows.length?'<table><thead><tr>'+headers.map(h=>'<th>'+esc(h)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+r.map(c=>'<td>'+c+'</td>').join('')+'</tr>').join('')+'</tbody></table>':'<div class="empty">Пока нет данных для этой серии.</div>';}
function render(){
 $('#identity').textContent=state.user.username+' · '+(state.user.telegram_linked?'Администратор · Telegram':'Личный аккаунт · без Telegram');
 const key=state.generation+':active',a=state.accounts[key],s=a?.stats||{};
 $('#status').textContent=state.dashboard?.status_label||state.status;$('#pause').textContent=state.config.paused?'Продолжить':'Пауза';
 $('#notice').textContent=state.mode==='SYNTHETIC_DEMO'?'ДЕМОНСТРАЦИЯ · Искусственные свечи для проверки программы. Эти результаты не показывают доходность на рынке.':'PAPER · Все сделки учебные. Расчёт использует модель исполнения; преимущество стратегии пока не подтверждено.';
 if(state.config.market==='spot_reference')$('#notice').textContent='Binance Spot · уровни для учебных LONG/SHORT. Funding и открытый интерес недоступны. Для ручной сделки сверяйте цену своего фьючерса; учебный SHORT не означает продажу спота без займа.';
 $('#source-label').textContent=state.data_source||'Демонстрация';
 $('#updated').textContent='Данные: '+dt(state.updated_at);$('#series-label').textContent=state.generation.slice(0,8);$('#export').href='/api/export?account='+encodeURIComponent(key);
 const activePositions=Object.entries(state.accounts).filter(([k])=>k.endsWith(':active')).flatMap(([,v])=>v.positions||[]);
 $('#metrics').innerHTML=[['Учебный капитал',n(s.equity)+' USDT','Начальный: '+n(state.config.risk.initial_equity)],['Открыто сделок',activePositions.length+' / '+state.config.risk.max_positions,'Во всех ваших сериях'],['Результат',n(s.return_pct)+'%','Текущая серия · после расходов'],['Монеты',String(state.config.symbols.length),'Мониторинг выбранных монет']].map(([label,value,note],i)=>'<div class="metric"><label>'+esc(label)+'</label><strong class="'+(i===2?(s.return_pct>=0?'positive':'negative'):'')+'">'+esc(value)+'</strong><small>'+esc(note)+'</small></div>').join('');
 renderEquity();renderOverview(activePositions);
 const risk=state.config.risk,capital=s.equity||risk.initial_equity,usedRisk=activePositions.reduce((sum,p)=>sum+(p.planned_risk||0),0);
 const slotRisk=Math.min(risk.risk_pct,risk.max_open_risk_pct/risk.max_positions,risk.max_side_risk_pct/risk.max_positions);
 $('#risk-summary').innerHTML='<div class="panel-heading"><h2>Бюджет риска</h2><span class="pill">До '+risk.max_positions+' позиций · без плеча</span></div><p class="muted small">Риск на вход: до '+n(capital*slotRisk)+' USDT ('+n(slotRisk*100,3)+'%). Общий лимит: '+n(capital*risk.max_open_risk_pct)+' USDT; занято по открытым сделкам: '+n(usedRisk)+' USDT. Одно направление: до '+n(capital*risk.max_side_risk_pct)+' USDT. Объём одного нового входа: до '+n(capital*risk.max_notional_equity/risk.max_positions)+' USDT. Дневной стоп: '+n(risk.daily_loss_pct*100,1)+'%; стоп по просадке: '+n(risk.max_drawdown_pct*100,1)+'%. Размер уменьшается при нехватке бюджета. Гэп и проскальзывание могут увеличить фактический убыток.</p>';
 $('#equity-note').textContent=state.config.market==='spot_reference'?'Учебный капитал после расходов. Для спотовых ориентиров funding не начисляется.':'Учебный капитал после расчётных комиссий, funding и переоценки позиций.';
 $('#market').innerHTML=Object.entries(state.market).map(([sym,m])=>'<div class="market-row"><div><b>'+esc(sym)+'</b><small>'+esc(m.detail||m.status)+'</small></div><div>'+n(m.price,4)+'<small>'+(m.direction===1?'Восходящее':m.direction===-1?'Нисходящее':'Неопределённое')+'</small></div></div>').join('')||'<p class="empty">Ожидание биржевых данных…</p>';
 $('#positions').innerHTML=positionsTable(activePositions);
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
function details(id){const c=state.candidates.find(x=>x.id===id);if(!c)return;$('#detail-body').innerHTML='<h2>'+esc(c.symbol)+' · '+(c.side===1?'LONG':'SHORT')+'</h2><p>'+esc(c.explanation)+'</p><div class="signal-levels"><div><small>Вход · ТВХ</small><strong>'+n(c.reference,6)+'</strong></div><div class="target"><small>TP · Цель</small><strong>'+n(c.target,6)+'</strong></div><div><small>SL · Стоп</small><strong>'+n(c.stop,6)+'</strong></div></div><p class="muted">Предварительные уровни сценария; исполнение подтверждается в разделе «Мои сделки».</p><img src="/api/chart?candidate='+encodeURIComponent(c.id)+'" alt="График цены с уровнями и Фибоначчи"><div class="scroll">'+table(['Проверка','Результат','Объяснение'],Object.entries(c.checks).map(([k,ch])=>[esc(state.filter_names[k]),ch.passed==null?'Нет данных':ch.passed?'<span class="positive">Пройдена</span>':'<span class="negative">Не пройдена</span>',esc(ch.detail)]))+'</div>';$('#detail').showModal();}

let selectedSeries=null,selectedAccount=null;
function renderReport(){
 const d=state.dashboard;if(!d)return;
 const blocked=state.status==='DATA_ERROR';
 $('#bot-health').innerHTML='<div class="panel-heading"><h2>'+esc(d.status_label)+'</h2><span class="pill">'+(state.mode==='SYNTHETIC_DEMO'?'Демонстрация':'Учебный режим')+'</span></div><p>'+(blocked?'Биржа не отдаёт данные. Новые сигналы не формируются. Нулевые результаты сейчас не означают, что фильтры плохие.':state.config.paused?'Новые входы приостановлены. Открытые учебные сделки продолжают сопровождаться.':d.fresh?'Данные обновляются. Бот ждёт подходящие условия входа.':'Свежий анализ ещё не получен. Бот подключается или готовит историю.')+'</p>'+(blocked&&d.last_error?'<p class="muted">Причина: '+esc(d.last_error.message)+'</p>':'');
 $('#coin-count').textContent=d.coins.length+' монет';
 $('#universe-note').textContent=state.universe?.label||'Список выбран вручную';
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
 $('#account-detail').innerHTML+='<h3>Открытые сделки этого набора</h3><div class="scroll">'+positionsTable(state.accounts[c.key]?.positions||[])+'</div><p class="muted">PnL — оценка при закрытии сейчас, после расчётных комиссий и проскальзывания. Процент от суммы входа, без плеча.</p>';
}

function positionsTable(positions){
 const money=x=>x==null?'—':(x>0?'+':'')+n(x);
 return table(['Монета','Сторона','Вход · ТВХ','Цена сейчас','PnL USDT ≈','PnL % ≈','SL · Стоп','TP · Цель','Время цены'],positions.map(p=>[
  '<button class="button secondary" data-position="'+esc(p.id)+'" data-account="'+esc(Object.keys(state.accounts).find(k=>(state.accounts[k].positions||[]).some(x=>x.id===p.id&&x.config_version===p.config_version))||'')+'">'+esc(p.symbol)+' ↗</button>',p.side===1?'На рост · LONG':'На падение · SHORT',n(p.entry,6),n(p.current_price,6),
  '<span class="'+(p.estimated_pnl==null?'muted':p.estimated_pnl>=0?'positive':'negative')+'">'+money(p.estimated_pnl)+'</span>',money(p.estimated_pnl_pct)+'%',
  n(p.stop,6),n(p.target,6),'<span class="'+(p.price_stale?'negative':'muted')+'">'+esc(dt(p.price_ts))+(p.price_kind==='minute_close'?' · свеча':'')+(p.price_stale?' · устарела':'')+'</span>'
 ]));
}

function renderEquity(){
 const a=state.accounts[state.generation+':active'];let curve=a?.curve||[];
 if(chartPeriod!=='all'){const cutoff=Date.now()-Number(chartPeriod)*86400000;curve=curve.filter(p=>p[0]>=cutoff);}
 document.querySelectorAll('[data-period]').forEach(b=>b.classList.toggle('active',b.dataset.period===chartPeriod));
 if(curve.length<2){$('#equity').innerHTML='<div class="empty"><span class="empty-icon">⌁</span><b>Начинаем наблюдение</b><small>График появится после записи первых точек капитала.</small></div>';return;}
 const values=curve.map(p=>p[1]),min=Math.min(...values),max=Math.max(...values),pad=Math.max((max-min)*.15,Math.abs(max)*.0005,1),lo=min-pad,hi=max+pad;
 const w=700,h=210,left=70,right=16,top=12,bottom=32,pw=w-left-right,ph=h-top-bottom;
 const points=curve.map((p,i)=>[(left+i/(curve.length-1)*pw).toFixed(2),(top+(hi-p[1])/(hi-lo)*ph).toFixed(2)]);
 const line=points.map(p=>p.join(',')).join(' '),last=points[points.length-1];
 const grid=[0,.25,.5,.75,1].map(f=>{const y=top+f*ph;return '<line x1="'+left+'" y1="'+y+'" x2="'+(w-right)+'" y2="'+y+'" stroke="#2a2d2f"/><text x="'+(left-10)+'" y="'+(y+4)+'" text-anchor="end">'+esc(n(hi-f*(hi-lo),0))+'</text>';}).join('');
 const date=t=>new Date(t).toLocaleDateString('ru-RU',{day:'numeric',month:'short',timeZone:'UTC'});
 $('#equity').innerHTML='<svg viewBox="0 0 700 210" role="img" aria-label="Изменение учебного капитала"><defs><linearGradient id="equity-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#8aefbb" stop-opacity=".2"/><stop offset="100%" stop-color="#8aefbb" stop-opacity="0"/></linearGradient></defs>'+grid+'<polygon points="'+left+','+(top+ph)+' '+line+' '+(w-right)+','+(top+ph)+'" fill="url(#equity-fill)"/><polyline points="'+line+'" fill="none" stroke="#8aefbb" stroke-width="2.5"/><circle cx="'+last[0]+'" cy="'+last[1]+'" r="4" fill="#a1f4c6"/><text x="'+left+'" y="204">'+esc(date(curve[0][0]))+'</text><text x="'+(w-right)+'" y="204" text-anchor="end">'+esc(date(curve[curve.length-1][0]))+'</text></svg>';
}
function renderOverview(positions){
 const current=state.generation+':active';
 const candidate=state.candidates.find(c=>c.decisions.some(d=>d.account===current&&d.accepted));
 if(candidate){$('#latest-signal').innerHTML='<div class="signal-symbol"><b>'+esc(candidate.symbol)+'</b><span class="side '+(candidate.side===1?'positive':'negative')+'">'+(candidate.side===1?'LONG':'SHORT')+'</span></div><p class="muted">'+esc(dt(candidate.ts))+'</p><div class="signal-levels"><div><small>Вход · ТВХ</small><strong>'+n(candidate.reference,6)+'</strong></div><div class="target"><small>TP · Цель</small><strong>'+n(candidate.target,6)+'</strong></div><div><small>SL · Стоп</small><strong>'+n(candidate.stop,6)+'</strong></div></div><p class="small muted">Последний допущенный сценарий. Его вход мог быть отменён или сделка уже закрыта.</p><button class="text-button" id="spotlight-detail">Разобрать сценарий ↗</button>';$('#spotlight-detail').onclick=()=>{document.querySelector('[data-tab="signals"]').click();details(candidate.id);};}
 else $('#latest-signal').innerHTML='<div class="empty"><span class="empty-icon">◎</span><b>Ищем точку входа</b><small>Здесь появятся ТВХ, TP и SL, когда сценарий пройдёт ваши фильтры.</small></div>';
 $('#overview-positions').innerHTML=table(['Монета','Вход','TP','SL','PnL ≈'],positions.map(p=>['<button class="button secondary" data-position="'+esc(p.id)+'" data-account="'+esc(Object.keys(state.accounts).find(k=>k.endsWith(':active')&&(state.accounts[k].positions||[]).some(x=>x.id===p.id&&x.config_version===p.config_version))||'')+'">'+esc(p.symbol)+' ↗</button> <small class="'+(p.side===1?'positive':'negative')+'">'+(p.side===1?'LONG':'SHORT')+'</small>',n(p.entry,4),n(p.target,4),n(p.stop,4),'<span class="'+(p.price_stale?'muted':p.estimated_pnl>=0?'positive':'negative')+'">'+(p.estimated_pnl>0?'+':'')+n(p.estimated_pnl)+(p.price_stale?' · устар.':'')+'</span>']));
 const comparisons=(state.dashboard?.comparisons||[]).filter(c=>c.current);
 $('#overview-combinations').innerHTML=table(['Набор','Сделок','Удачных','Итог USDT'],comparisons.map(c=>[esc(c.name),n(c.stats.trades,0),n(c.stats.win_rate,0)+'%','<span class="'+(c.realized_pnl>=0?'positive':'negative')+'">'+n(c.realized_pnl)+'</span>']));
 const labels={on:'Включён',observe:'Наблюдение',off:'Выключен'};
 const chosen=Object.entries(state.config.filters).filter(([,v])=>v==='on');
 $('#filter-summary').innerHTML=(chosen.length?chosen.slice(0,5):Object.entries(state.config.filters).slice(0,5)).map(([k,v])=>'<div class="filter-summary-row"><span>'+esc(state.filter_names[k])+'</span><span class="mini-mode '+v+'" aria-label="'+labels[v]+'">'+(v==='on'?'●':v==='observe'?'◐':'○')+'</span></div>').join('')+'<p class="small muted">Включено '+chosen.length+' из '+Object.keys(state.config.filters).length+'. Изменить режимы можно в разделе «Фильтры».</p>';
}

async function positionDetails(account,id,updating=false){
 openPosition={account,id};
 if(!updating){$('#detail-body').innerHTML='<p>Загружаем подробности сделки…</p>';if(!$('#detail').open)$('#detail').showModal();}
 try{
 const data=await api('/api/position?account='+encodeURIComponent(account)+'&id='+encodeURIComponent(id));
 if(openPosition?.id!==id||openPosition?.account!==account)return;
 const p=data.position,c=data.candidate,r=p.rules,price=p.current_price;
 const rows=[['Статус','Открыта · учебная'],['Сценарий',state.setup_names[p.setup]||p.setup],['Вход UTC',dt(p.entry_ts)],['Время в сделке',n((Date.now()-p.entry_ts)/60000,1)+' мин'],['Объём монет',n(p.qty,8)],['Сумма входа',n(p.qty*p.entry)+' USDT'],['Запланированный риск',n(p.planned_risk)+' USDT'],['Текущий результат R',n(p.estimated_pnl==null?null:p.estimated_pnl/p.planned_risk,3)],['PnL до расходов',n(p.unrealized_gross)+' USDT'],['PnL после расходов ≈',n(p.estimated_pnl)+' USDT · '+n(p.estimated_pnl_pct)+'%'],['Комиссия входа',n(p.entry_fee,4)+' USDT'],['Ставка комиссии',n(r.fee_rate*100,3)+'%'],['Проскальзывание модели',n(r.slippage_bps,1)+' bps'],['Funding',state.config.market==='spot_reference'?'Не применяется для спотового источника':n(p.funding_paid,4)+' USDT'],['Первоначальный SL',n(p.initial_stop,8)],['До SL от текущей цены',price==null?'—':n(Math.abs(price-p.stop)/price*100,3)+'%'],['До TP от текущей цены',price==null?'—':n(Math.abs(price-p.target)/price*100,3)+'%'],['Лимит времени',n(r.max_hold_minutes,0)+' мин'],['Котировка',dt(p.price_ts)+' · '+(p.price_kind||'—')+(p.price_stale?' · УСТАРЕЛА':'')],['Серия',account],['ID сделки',p.id]];
 $('#detail-body').innerHTML='<h2>'+esc(p.symbol)+' · '+(p.side===1?'LONG':'SHORT')+'</h2><p class="muted">Учебная позиция · данные обновляются каждые 15 секунд. График по завершённым свечам 5м, время UTC.</p><div class="signal-levels"><div><small>Фактический вход · ТВХ</small><strong>'+n(p.entry,8)+'</strong></div><div class="target"><small>TP · Цель</small><strong>'+n(p.target,8)+'</strong></div><div><small>SL · Стоп</small><strong>'+n(p.stop,8)+'</strong></div></div><p>Цена сейчас: '+n(price,8)+' · PnL ≈ <b class="'+(p.estimated_pnl>=0?'positive':'negative')+'">'+n(p.estimated_pnl)+' USDT</b></p><img src="/api/position-chart?account='+encodeURIComponent(account)+'&id='+encodeURIComponent(id)+'&t='+Date.now()+'" alt="Свечной график сделки с уровнями входа, TP, SL и текущей цены"><div class="scroll">'+table(['Параметр','Значение'],rows.map(row=>row.map(esc)))+'</div><h3>Условия в момент сигнала</h3>'+(c?'<p>'+esc(c.explanation)+'</p><div class="scroll">'+table(['Фильтр','Результат','Значение','Объяснение'],Object.entries(c.checks).map(([k,ch])=>[esc(state.filter_names[k]||k),ch.passed==null?'Нет данных':ch.passed?'Пройден':'Не пройден',esc(JSON.stringify(ch.value)),esc(ch.detail)]))+'</div>':'<p>Исходный разбор сигнала недоступен. Уровни и параметры исполнения сохранены.</p>');
 }catch(e){if(!updating)$('#detail-body').textContent=e.message;}
}
