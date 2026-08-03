const http=require('http'),fs=require('fs'),path=require('path');
const root=path.resolve(__dirname,'..'),source=fs.readFileSync(path.join(root,'index.html'),'utf8');
const fakeTelegram=`<script>window.Telegram={WebApp:{initData:'mock-init-data',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},isVersionAtLeast(){return false},HapticFeedback:{notificationOccurred(){}}}};</script>`;
const state={registered:true,user:{id:10,name:'Анна К.',role:'Скаут',city_id:1,pay_type:'hourly',pay_amount:350,edit_mode:false},city:{id:1,name:'Краснодар',timezone_offset:3},cities:[{id:1,name:'Краснодар',timezone_offset:3}],active:false,shift:null,last:null,level:{level:3,title:'Велик',tier:'bronze',xp:42,need:100},lifetime:{earned:18500,shifts:8,actions:240},month_earned:9200,period_started_label:'01.08.2026',build_version:'crm-test'};
let status='in_progress';
const task=()=>({task_id:41,city_id:1,work_date:'2026-08-04',title:'Проверить парковку у вокзала',description:'Проверь расстановку байков, поправь мешающие проходу и приложи итоговое фото.',priority:'urgent',status:'published',requires_photo:true,my_status:status,my_status_comment:'',attachments:[{id:1,original_name:'пример.jpg',download_url:'/api/crm/task-attachments/1'}],result_attachments:[]});
const pixel=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=','base64');
function json(res,data,statusCode=200){res.writeHead(statusCode,{'Content-Type':'application/json; charset=utf-8'});res.end(JSON.stringify(data))}
http.createServer((req,res)=>{
  const url=new URL(req.url,'http://127.0.0.1:4188'),p=url.pathname;
  if(p==='/'||p==='/index.html'){
    const opener=`<script>addEventListener('load',()=>setTimeout(()=>{document.querySelector('[data-tab="more"]')?.click();setTimeout(()=>{document.getElementById('btnMyTasks')?.click();${url.searchParams.get('open')==='detail'?"setTimeout(()=>document.querySelector('[data-my-task]')?.click(),250);":''}},180)},250));</script>`;
    res.writeHead(200,{'Content-Type':'text/html; charset=utf-8'});return res.end(source.replace('<script src="https://telegram.org/js/telegram-web-app.js?63"></script>',fakeTelegram).replace('</body>',opener+'</body>'));
  }
  if(p==='/api/state')return json(res,state);
  if(p==='/api/crm/tasks/mine')return json(res,{city:{id:1,name:'Краснодар'},items:[task()]});
  if(p==='/api/crm/task-attachments/1'){res.writeHead(200,{'Content-Type':'image/png'});return res.end(pixel)}
  if(/^\/api\/crm\/tasks\/41\/progress$/.test(p)){let body='';req.on('data',c=>body+=c);req.on('end',()=>{try{status=JSON.parse(body).status||status}catch{}json(res,{ok:true,status})});return}
  if(/^\/api\/crm\/tasks\/41\/attachments$/.test(p))return json(res,{ok:true,items:[]},201);
  json(res,{message:'mock route missing'},404);
}).listen(4188,'127.0.0.1',()=>console.log('Employee tasks mock http://127.0.0.1:4188/?open=detail'));
