/* Server is authoritative. This module never mints or renews bearer links. */
const LookupAccess=(()=>{
 const params=new URLSearchParams(location.search),token=params.get('t')||'';
 let active=false,deadline=0,version='',generation=0,timer=null;
 let serverMillis=0,observedAt=0;
 const requests=new Set();
 function error(message){
  active=false;generation++;clearTimeout(timer);
  requests.forEach(c=>c.abort());requests.clear();
  if(typeof resetQuery==='function')resetQuery();
  if(typeof DATA!=='undefined'){DATA.batches=[];DATA.rows={};}
  const app=document.getElementById('app'),gate=document.getElementById('gate');
  if(app)app.hidden=true;
  if(gate){gate.hidden=false;gate.textContent=message;}
 }
 function expired(){error('本链接已过期，请使用今天推送中的查询链接。');}
 function remaining(){return deadline-performance.now();}
 async function call(action,body={}){
  if(!token){error(params.has('d')?'入口已升级，请使用最新推送链接。':'请使用今天推送中的完整查询链接。');throw new Error('missing_token');}
  const epoch=generation,controller=new AbortController();requests.add(controller);
  const started=performance.now();let response;
  const timeout=setTimeout(()=>controller.abort(),15000);
  try{
   response=await fetch(ACCESS_CONFIG.api+'/'+action,{method:'POST',cache:'no-store',credentials:'omit',redirect:'error',
    headers:{'Content-Type':'application/json','Authorization':'Bearer '+token},body:JSON.stringify(body),signal:controller.signal});
   const data=await response.json();
   if(epoch!==generation)throw new Error('stale_response');
   if(!response.ok){
    if(response.status===401||response.status===409)error(data.message||'链接无效，请使用今天推送中的查询链接。');
    throw new Error(data.message||'查询服务暂不可用，请稍后再试。');
   }
   if(!Number.isFinite(data.server_time)||!Number.isFinite(data.expires_at)||data.expires_at<=data.server_time){expired();throw new Error('expired');}
   serverMillis=data.server_time*1000;observedAt=performance.now();
   // Use elapsed monotonic time, never the user's wall clock. Count network time conservatively.
   const next=started+(data.expires_at-data.server_time)*1000;
   deadline=deadline?Math.min(deadline,next):next;
   if(remaining()<=0){expired();throw new Error('expired');}
   clearTimeout(timer);timer=setTimeout(expired,remaining());
   return data;
  } finally {clearTimeout(timeout);requests.delete(controller);}
 }
 async function start(){
  if(!ACCESS_CONFIG.api){error('查询服务暂未启用，请稍后使用最新推送链接。');return;}
  try{
   const result=await call('session');version=result.version;
   if(version!==ACCESS_CONFIG.version){error('页面与查询数据版本不一致，请刷新后重试。');return;}
   const region=ACCESS_CONFIG.region;
   if(region&&!result.regions[region])throw new Error('该地区暂未收录查询名单。');
   if(region&&typeof setQueueChoices==='function')setQueueChoices(result.regions[region].queues);
   document.querySelectorAll('a[data-region]').forEach(a=>{
    const key=a.getAttribute('data-region'),entry=result.regions[key];
    if(!entry){a.remove();return;}
    a.href=entry.file+'?t='+encodeURIComponent(token);
   });
   const date=new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai'}).format(new Date(result.server_time*1000));
   document.getElementById('link-validity').textContent='仅'+date+'当天有效（北京时间，次日00:00失效）';
   if(result.built_at)document.getElementById('link-validity').textContent+='；本数据版本构建于'+new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(result.built_at))+'，名单日期见查询结果。';
   active=true;document.getElementById('gate').hidden=true;document.getElementById('app').hidden=false;
  }catch(e){if(!document.getElementById('gate').textContent)error('查询服务暂不可用，请稍后重试。');}
 }
 function check(){if(!active||remaining()<=0){if(active)expired();return false;}return true;}
 document.addEventListener('visibilitychange',()=>{
  if(!document.hidden&&active){if(!check())return;error('正在重新验证链接…');deadline=0;start();}
 });
 window.addEventListener('pageshow',e=>{if(e.persisted){error('正在重新验证链接…');deadline=0;start();}});
 window.addEventListener('pagehide',()=>{if(active)error('重新打开后需要验证链接。');});
 async function query(number,queue){
  if(!check())throw new Error('请使用今天推送中的查询链接。');
  const result=await call('query',{region:ACCESS_CONFIG.region,apply_no:number,queue,version});
  if(!check())throw new Error('expired');return result.payload;
 }
 function now(){return serverMillis?serverMillis+performance.now()-observedAt:Date.now();}
 return {start,query,check,now};
})();
