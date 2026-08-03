const http=require('http'),fs=require('fs'),path=require('path');
const root=path.resolve(__dirname,'..'),html=fs.readFileSync(path.join(root,'crm.html'),'utf8');
const context={ok:true,user:{id:1,name:'Тест Руководитель'},role:'network_admin',role_scope:null,can_write:true,cities:[{id:1,name:'Краснодар'}],default_city_id:1};
const shifts=[{shift_id:1,user_id:10,name:'Анна К.',role:'Скаут',status:'active',date:'2026-08-03',start_time:'08:00',end_time:null,worked_minutes:385,district:'Центр',source:'bot',on_lunch:false,actions_total:31,actions_per_hour:4.8}];
const task={task_id:1,city_id:1,work_date:'2026-08-03',title:'Проверить парковку',description:'Проверить состояние байков',priority:'high',status:'published',progress:{total:2,assigned:0,seen:0,in_progress:0,submitted:1,accepted:1,blocked:0},attachments:[],assignees:[{user_id:10,full_name_snap:'Анна К.',role_snap:'Скаут',status:'submitted',status_comment:'Готово'},{user_id:11,full_name_snap:'Иван П.',role_snap:'Скаут',status:'accepted'}]};
function json(res,data,status=200){res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(data))}
http.createServer((req,res)=>{
  const url=new URL(req.url,'http://127.0.0.1:4187'),p=url.pathname;
  if(p==='/'||p==='/crm.html'){
    const requested=url.searchParams.get('route')||'today',route=requested==='taskmodal'?'tasks':requested,open=requested==='taskmodal'?'task':(url.searchParams.get('open')||'');
    const seed=`<script>localStorage.setItem('bb_crm_admin_token','test');localStorage.setItem('bb_crm_context',JSON.stringify(${JSON.stringify(context)}));addEventListener('load',()=>setTimeout(()=>{document.querySelector('[data-route="${route}"]')?.click();${open==='task'?"setTimeout(()=>document.querySelector('[data-create-task]')?.click(),350);":""}},300));</script>`;
    res.writeHead(200,{'Content-Type':'text/html; charset=utf-8'});return res.end(html.replace('</head>',seed+'</head>'));
  }
  if(p==='/api/admin/crm/context')return json(res,context);
  if(p==='/api/admin/crm/overview')return json(res,{city:{id:1,name:'Краснодар'},generated_at:new Date().toISOString(),totals:{employees:1,shifts:1,worked_minutes:385,actions:31,actions_per_hour:4.8},current:{active:1,scheduled:0,on_lunch:0},tasks:{assignees:2,done:1},data_quality:{manual_reports_waiting:1,long_open_shifts:0}});
  if(p==='/api/admin/crm/shifts')return json(res,{items:shifts,page:{total:1}});
  if(p==='/api/admin/crm/tasks/assignee-preview')return json(res,{count:2,items:[{user_id:10,full_name:'Анна К.',role:'Скаут'},{user_id:11,full_name:'Иван П.',role:'Скаут'}]});
  if(p==='/api/admin/crm/tasks')return req.method==='POST'?json(res,{ok:true,task:{...task,task_id:2,status:'draft'}},201):json(res,{items:[task],page:{total:1}});
  if(/^\/api\/admin\/crm\/tasks\/\d+$/.test(p))return json(res,{task});
  if(p.includes('/api/admin/crm/tasks/'))return json(res,{ok:true,task});
  if(p==='/api/admin/crm/data-quality')return json(res,{counts:{manual_report_waiting:1},items:[{type:'manual_report_waiting',severity:'medium',report:{sender_name:'Анна К.'}}]});
  if(p==='/api/admin/crm/calendar')return json(res,{days:{'2026-08-03':{planned:1,came:1,missed:0,ambiguous:0,unplanned:0}},planned:[{plan_id:1,work_date:'2026-08-03',start_time:'08:00',end_time:'18:00',user_id:10,user_name:'Анна К.',role:'Скаут',district:'Центр',status:'scheduled',match_status:'вышел',actual_shifts:shifts}],actual_unplanned:[]});
  if(p==='/api/admin/crm/employees')return json(res,{items:[{user_id:10,name:'Анна К.',role:'Скаут',shifts:4,worked_minutes:2300,actions_total:180,actions_per_hour:4.7,has_open_shift:true,on_lunch:false}],page:{total:1}});
  if(/^\/api\/admin\/crm\/employees\/\d+$/.test(p))return json(res,{employee:{user_id:10,name:'Анна К.',role:'Скаут'},totals:{shifts:4,worked_minutes:2300,actions:180,actions_per_hour:4.7},shifts});
  if(p==='/api/admin/crm/trends')return json(res,{range:{from:'2026-07-25',to:'2026-08-03'},series:[{date:'2026-08-03',shifts:1,employees:1,worked_minutes:385,actions:31,actions_per_hour:4.8}]});
  if(p==='/api/admin/crm/planned-shifts')return json(res,{ok:true,plan:{id:2}},201);
  json(res,{message:'mock route missing'},404);
}).listen(4187,'127.0.0.1',()=>console.log('CRM mock http://127.0.0.1:4187/crm.html'));
