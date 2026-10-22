"""Проверяет форму подписки реальным JS-движком без запуска бота."""
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / "crm.html").read_text(encoding="utf-8")
start = source.index("function renderSubscriptionCard(data){")
end = source.index("async function loadMore()", start)
functions = source[start:end]
script = r"""
const vm=require('vm'),assert=require('assert');
const functions=FUNCTIONS;
const requests=[],messages=[];
let failed=true;
const fields={
  '[data-billing-confirm]':{checked:false},
  '[data-billing-months]':{value:'1'},
  '[data-billing-reason]':{value:'Подтверждена оплата города'}
};
const row={dataset:{subscriptionCity:'2'},querySelector:key=>fields[key]};
const button={dataset:{},closest:()=>row,disabled:false};
const launch={disabled:false},launchConfirm={checked:false};
const context={
  crypto:require('crypto').webcrypto,console,
  esc:value=>String(value).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;'),
  fmtDate:value=>String(value),API:{subscriptions:'/api/admin/crm/subscriptions'},state:{cache:{}},
  toast:message=>messages.push(message),
  $:id=>id==='billingLaunch'?launch:id==='billingLaunchConfirm'?launchConfirm:null,
  loadMore:async()=>{},loadSubscriptionBanner:()=>{},
  request:async(path,options)=>{requests.push({path,body:JSON.parse(options.body)});if(failed)throw Error('Соединение прервано');return {ok:true}}
};
vm.createContext(context);vm.runInContext(functions,context);
const item={city_id:2,city_name:'<город>',state:'grace',enabled:true,employees:37,price_rub:500,amount_rub:18500,message:'Предупреждение',history:[]};
const data={can_manage:true,company_amount_rub:18500,items:[item]};
const owner=context.renderSubscriptionCard(data);
assert(owner.includes('data-billing-grant'));
assert(owner.includes('37 чел. × 500 ₽'));
assert(owner.includes('18'));
assert(owner.includes('&lt;город>'));
const manager=context.renderSubscriptionCard({...data,can_manage:false});
assert(!manager.includes('data-billing-grant'));
assert(!manager.includes('data-billing-confirm'));
assert(!manager.includes('billingLaunch'));
assert(context.renderSubscriptionCard({...data,items:[{...item,enabled:false,state:'disabled'}]}).includes('billingLaunchConfirm'));
context.wireSubscriptions({querySelectorAll:()=>[button]});
(async()=>{
  await button.onclick();assert.equal(requests.length,0);
  fields['[data-billing-confirm]'].checked=true;
  await button.onclick();assert.equal(requests.length,1);assert.equal(button.disabled,false);
  const key=requests[0].body.request_id;assert(key.length>=16);
  failed=false;await button.onclick();assert.equal(requests.length,2);
  assert.equal(requests[1].body.request_id,key,'повтор после сетевой ошибки использует тот же ключ');
  assert.equal(requests[1].body.city_id,2);assert.equal(requests[1].body.months,1);assert.equal(requests[1].body.confirm,true);
  await launch.onclick();assert.equal(requests.length,2);
  launchConfirm.checked=true;await launch.onclick();assert.equal(requests.length,3);assert.equal(requests[2].body.confirm,true);
  console.log('PASS city billing UI: quote, owner controls, confirmation and retry idempotency');
})().catch(error=>{console.error(error);process.exitCode=1});
""".replace("FUNCTIONS", json.dumps(functions))
node = shutil.which("node")
if not node:
    raise SystemExit("Для проверки нужен node")
subprocess.run([node, "-e", script], check=True)
