"use strict";
const get = id => document.getElementById(id);
const esc = x => String(x ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const labels = {Q:"交付量",P:"单位收入代理",C:"单位成本代理",Rv:"车辆收入",Cv:"车辆成本",Ro:"其他收入",Co:"其他成本",RD:"研发费用",SG:"销管费用",OI:"其他经营收益",GP:"毛利",OP:"营业利润",PassengerRetail:"国内乘用车零售",NEVRetail:"国内新能源乘用车零售",PassengerWholesale:"乘用车厂商批发",AutoProduction:"汽车产量",AutoSales:"汽车销量",NEVProduction:"新能源汽车产量",NEVSales:"新能源汽车销量",Cost:"销售成本",Revenue:"营业收入"};
const models = {ma3:"最近三月均值",seasonal:"上年同月",seasonal_growth:"上年同月 × 近期同比"};
const scenarios = {base:"基准情景",weak:"偏弱情景",strong:"偏强情景"};
const scopes = {GROUP:"集团",NIO:"蔚来品牌",ONVO:"乐道",FIREFLY:"萤火虫"};
const origins = {Live:"当前分析日",T0:"季初",T1:"首月已披露",T2:"两月已披露",T3:"交付已披露"};
const splits = {development:"开发期",holdout:"留出期"};
const formulas = {Q:"交付变化 × 两个状态的平均单位毛利",P:"平均交付量 × 单位收入变化",C:"−平均交付量 × 单位成本变化",Ro:"其他收入的变化",Co:"−其他成本的变化",RD:"−研发费用的变化",SG:"−销管费用的变化",OI:"其他经营收益净额的变化"};
const opts = (items, key, names={}) => items.map(x=>'<option value="'+esc(x)+'"'+(x===key?' selected':'')+'>'+esc(names[x]||x)+'</option>').join("");
const unique = a => Array.from(new Set(a));
const empty = msg => '<p class="empty">'+esc(msg)+'</p>';
const num = (v, d=0) => v == null || !Number.isFinite(Number(v)) ? "—" : Number(v).toLocaleString("zh-CN",{minimumFractionDigits:d,maximumFractionDigits:d});
const money = x => num(x == null ? null : x/1e8,2);
const unit = k => k==="Q"?"辆":(["P","C"].includes(k)?"元/辆":"亿元");
const val = (x,k) => ["Q","P","C"].includes(k) ? num(x) : money(x);
const cnDate = (t, time=false) => !Number.isFinite(+t)?"—":new Date((+t+8*3600)*1000).toISOString().slice(0,time?19:10).replace("T"," ");
const env = s => '<p class="note">'+esc(s)+'</p>';
const kpi = (name,value,foot) => '<div class="kpi"><span>'+esc(name)+'</span><strong>'+esc(value)+'</strong><small>'+esc(foot)+'</small></div>';
const table = (head, rows) => rows.length ? '<div class="scroll"><table><thead><tr>'+head.map(x=>'<th scope="col">'+esc(x)+'</th>').join("")+'</tr></thead><tbody>'+rows.map(row=>'<tr>'+row.map(x=>'<td>'+x+'</td>').join("")+'</tr>').join("")+'</tbody></table></div>':empty("该范围暂无记录。");
const card = (name,body,control="") => '<section class="card"><div class="cardhead"><h2>'+esc(name)+'</h2>'+control+'</div>'+body+'</section>';
const picker = (id,label,items,selected,names={})=>'<label>'+esc(label)+'<select id="'+esc(id)+'">'+opts(items,selected,names)+'</select></label>';
const details = (title,body) => '<details><summary>'+esc(title)+'</summary>'+body+'</details>';
const monthShift = (iso,i) => {const d=new Date(iso+"-01T00:00:00Z"); d.setUTCMonth(d.getUTCMonth()+i);return d.toISOString().slice(0,7);};
let d, r, sources, recordMap, tab=0, period,origin,model,scenario,scope="GROUP",bridgeSelection="",driverKey="Q",split="development",scoreMetric="OP",industryKey="";
let visibleM=[],visibleF=[];
function source(id) {
  const s=sources.get(id);
  if(!s)return esc(id);
  if(!/^https?:\/\//i.test(s.url))return esc(s.title);
  return '<a href="'+esc(s.url)+'" target="_blank" rel="noopener noreferrer">'+esc(s.title)+'</a>';
}
function refs(ids) {return unique(ids||[]).map(source).join(" · ");}
function records(ids) {
  return table(["所属期","范围 / 指标","原表数值","记录与定位","来源"],unique(ids||[]).map(id=>{
    const z=recordMap.get(id);
    if(!z)return [esc("—"),"—","—",'<code>'+esc(id)+'</code>',"—"];
    return [esc(z.period),esc((scopes[z.scope]||z.scope)+" / "+(labels[z.metric]||z.metric)),esc(z.raw_value+" "+z.raw_unit),'<code>'+esc(id)+'</code><br>'+esc(z.locator),source(z.source_id)];
  }));
}
function latest(rows,t) {
  const selected=new Map();
  for(const a of rows){
    const key=[a.period,a.scope,a.metric].join("|");
    const b=selected.get(key);
    if(a.available_at<=t && (!b || a.available_at>b.available_at || (a.available_at===b.available_at && a.revision>b.revision))) selected.set(key,a);
  }
  return Array.from(selected.values()).sort((a,b)=>a.period.localeCompare(b.period));
}
function filter() {
  const byPeriod=unique(d.runs.map(q=>q.period)).sort().reverse();
  get("filters").hidden=!byPeriod.length;
  if(!byPeriod.length){r=null;visibleM=latest(d.monthly,d.as_of);visibleF=latest(d.quarterly,d.as_of);return;}
  period=byPeriod.includes(period)?period:byPeriod[0];
  const a=d.runs.filter(q=>q.period===period);
  const times=["Live","T3","T2","T1","T0"].filter(x=>a.some(q=>q.origin===x));
  origin=times.includes(origin)?origin:times[0];
  const b=a.filter(q=>q.origin===origin),methods=unique(b.map(q=>q.model));
  model=methods.includes(model)?model:methods[0];
  const c=b.filter(q=>q.model===model),cases=unique(c.map(q=>q.scenario));
  scenario=cases.includes(scenario)?scenario:cases[0];
  r=c.find(q=>q.scenario===scenario);
  get("period").innerHTML=opts(byPeriod,period);
  get("origin").innerHTML=opts(times,origin,origins);
  get("model").innerHTML=opts(methods,model,models);
  get("scenario").innerHTML=opts(cases,scenario,scenarios);
  visibleM=latest(d.monthly,r.as_of);visibleF=latest(d.quarterly,r.as_of);
}
function monthlyChart(points){
  if(!points.some(p=>p.value!=null))return empty("所选期间暂无月度观测。");
  const peak=Math.max(1,...points.map(p=>p.value??0));
  const order=10**Math.floor(Math.log10(peak));
  const top=Math.ceil(peak/(order/2))*(order/2);
  const tick=v=>v>=10000?num(v/10000,Number.isInteger(v/10000)?0:2)+"万":num(v);
  const legend='<div class="legend"><span><i class="baractual"></i>披露实际</span>'+
    (points.some(m=>m.kind==="forecast")?'<span><i class="barforecast"></i>模型预测</span>':'')+'</div>';
  const bars=points.map((p,i)=>{
    const cls=p.value==null?"missing":(p.kind==="forecast"?"barforecast":"baractual");
    const desc=p.period+" · "+num(p.value)+(p.kind==="forecast"?"（预测）":"");
    const tickText=i===0||p.period.endsWith("-01")?p.period.slice(2).replace("-","/"):p.period.slice(-2);
    return '<div class="col" title="'+esc(desc)+'"><div class="barwell"><i class="'+cls+'" style="height:'+
      (100*(p.value??0)/top)+'%"></i></div><small>'+esc(tickText)+'</small></div>';
  }).join("");
  return legend+'<div class="chart-frame"><div class="scale" aria-hidden="true"><span>'+tick(top)+'</span><span>'+
    tick(top/2)+'</span><span>0</span></div><div class="monthlyplot" role="img" aria-label="'+
    esc(points.map(p=>p.period+" "+num(p.value)).join("，"))+'">'+bars+'</div></div>';
}
function trend() {
  const available=unique(["GROUP",...visibleM.filter(a=>a.metric==="Q" && Object.hasOwn(scopes,a.scope)).map(a=>a.scope)]);
  if(!available.includes(scope))scope="GROUP";
  const monthmap=new Map(visibleM.filter(a=>a.scope===scope && a.metric==="Q").map(a=>[a.period,a]));
  const last=r?r.monthly[2].period:visibleM.at(-1)?.period;
  if(!last)return card("月度交付",empty("暂无月度披露。"));
  const projected=new Map((scope==="GROUP" && r?r.monthly:[]).map(a=>[a.period,a]));
  const points=Array.from({length:15},(_,i)=>{
    const p=monthShift(last,i-14),a=monthmap.get(p),b=projected.get(p);
    return {period:p,value:a?.value??b?.value,kind:a?"actual":b?.kind,source_id:a?.source_id};
  });
  return card("月度交付",env("单位为辆。空白月份表示截至所选时点缺少可展示的数值。")+
    monthlyChart(points)+details("查看月度数值与来源",table(["月份","辆","类型","披露来源"],points.map(p=>[esc(p.period),num(p.value),p.value==null?"待披露":(p.kind==="forecast"?"预测":"实际"),p.source_id?source(p.source_id):"—"]))),
    picker("scope","交付范围",available,scope,scopes));
}
function industry(){
  const items=visibleM.filter(a=>a.metric!=="Q" || !Object.hasOwn(scopes,a.scope));
  if(!items.length)return card("国内市场背景",empty("所选信息时点尚无可用的行业月度观测。"));
  const keys=unique(items.map(a=>[a.scope,a.metric].join(" / ")));
  if(!keys.includes(industryKey))industryKey=keys.find(k=>k==="CN_PASSENGER_RETAIL / NEVRetail")||keys[0];
  const series=items.filter(a=>[a.scope,a.metric].join(" / ")===industryKey).slice(-12);
  const seriesName=k=>d.metric_labels?.[k]||labels[k]||k;
  const scopeName={CN_PASSENGER_RETAIL:"乘联分会 · 国内零售",CN_PASSENGER_WHOLESALE:"乘联分会 · 厂商批发",CAAM_NATIONAL:"中汽协 · 全国"};
  const label=seriesName(series[0].metric),last=series.at(-1),map=new Map(series.map(a=>[a.period,a]));
  const first=series[0].period;
  const points=Array.from({length:12},(_,i)=>monthShift(last.period,i-11)).filter(p=>p>=first).map(p=>({period:p,value:map.get(p)?.value,kind:"actual"}));
  const dropdown=Object.fromEntries(keys.map(k=>{const [scope,metric]=k.split(" / ");return [k,(scopeName[scope]||scope)+" · "+seriesName(metric)];}));
  return card("国内市场背景",env((scopeName[last.scope]||last.scope)+" · "+label+" · 最近一期 "+last.period+" 合计 "+num(last.value)+" 辆。")+
    monthlyChart(points)+details("查看行业数值、原表单位与来源",table(["月份",label,"原表单位","来源"],series.slice().reverse().map(a=>[esc(a.period),esc(a.raw_value),esc(a.raw_unit),source(a.source_id)])))+
    env("行业数据与蔚来全球集团交付的统计范围不同，两者分别展示。"),
    picker("industry","行业序列",keys,industryKey,dropdown));
}
function assumptions() {
  if(!r)return "";
  const a=r.assumptions;
  return card("冻结预测与输入",env("运行 "+r.run_id)+
    table(["输入假设","本次取值"],[
      ["交付模型",esc(models[r.model])],["可比财务期",esc(a.finance_period||"暂无完整财务基期")],
      ["未知月交付压力",num(a.delivery_shock*100,2)+"% · "+esc(a.delivery_shock_rule)],
      ["单位收入压力",num(a.unit_revenue_shock*100,2)+"% · "+esc(a.unit_revenue_shock_rule)]
    ])+details("展开输入记录与原文定位（"+r.input_record_ids.length+" 条）",records(r.input_record_ids))+
    details("本次运行涉及的公告",refs(r.source_ids)));
}
function deliveryPage(){
  const total=r?.values.Q, known=(r?.monthly||[]).filter(x=>x.kind==="actual");
  const actual=known.reduce((v,x)=>v+x.value,0);
  const guide=r?.guidance||[], g=guide.find(x=>x.metric==="Q");
  const peers=d.runs.filter(x=>r && x.period===period && x.origin===origin && x.model===model);
  const qrange=peers.length?num(Math.min(...peers.map(x=>x.values.Q)))+" – "+num(Math.max(...peers.map(x=>x.values.Q))):"—";
  const kpis='<div class="kpis">'+kpi("季度已披露交付",r && known.length?num(actual):"—",known.length+" / 3 个月已披露")+
    kpi(known.length===3?"集团季度实际交付":"集团季度交付估计",num(total),known.length===3?"三个月均已披露 · 辆":(r?scenarios[r.scenario]+" · 辆":"辆"))+
    kpi("公开交付指引",g?(g.low===g.high?"约 "+num(g.low):num(g.low)+"–"+num(g.high)):"未披露","公司口径 · 辆")+
    kpi("营业利润估计",money(r?.values.OP),"合并口径 · 亿元")+'</div>';
  const guidance=guide.length?table(["公开指引","公司披露数值","来源"],guide.map(x=>[esc(labels[x.metric]||x.metric)+"（"+unit(x.metric)+"）",x.low===x.high?"约 "+val(x.low,x.metric):val(x.low,x.metric)+" – "+val(x.high,x.metric),source(x.source_id)])):empty("截至所选时点未收录该季度的公开指引。");
  const remaining=g?env(g.unknown_months && g.low===g.high?"达到公司给出的约数尚需约 "+num(g.remaining_low)+" 辆，剩余月份平均约 "+num(g.required_per_month_low)+" 辆。":g.unknown_months?"达到指引下限尚需 "+num(g.remaining_low)+" 辆，上限尚需 "+num(g.remaining_high)+" 辆。剩余 "+g.unknown_months+" 个月平均需 "+num(g.required_per_month_low)+" 至 "+num(g.required_per_month_high)+" 辆。":"季度三个月的交付均已披露。"):"";
  const hist=r?.history_remaining||[];
  const composition=r?table(["当季月份","交付量（辆）","信息性质"],r.monthly.map(m=>[esc(m.period),num(m.value),m.kind==="actual"?"已披露实际":"模型预测"])):"";
  return kpis+'<div class="grid">'+trend()+card("交付进度与指引",composition+guidance+remaining+env("交付情景范围为 "+qrange+" 辆。")+
    (hist.length?details("往年同季剩余月份实际交付",table(["可比季度","合计（辆）"],hist.map(h=>[esc(h.period),num(h.remaining_total)]))):""))+'</div>'+industry()+assumptions();
}
function water(b){
  let cumulative=b.start_op;
  const rows=[{name:"起点营业利润",from:0,to:cumulative,value:cumulative}];
  for(const [k,effect] of Object.entries(b.effects)){
    rows.push({name:labels[k]||k,key:k,from:cumulative,to:cumulative+effect,value:effect});
    cumulative+=effect;
  }
  rows.push({name:"终点营业利润",from:0,to:b.end_op,value:b.end_op});
  const low=Math.min(0,...rows.flatMap(x=>[x.from,x.to])),high=Math.max(0,...rows.flatMap(x=>[x.from,x.to])),range=high-low||1;
  const pos=x=>100*(x-low)/range;
  return '<div class="water">'+rows.map(x=>{
    const tag=x.key?"button":"div";
    const attrs=x.key?' type="button" data-driver="'+esc(x.key)+'" aria-label="'+esc(x.name+"贡献 "+money(x.value)+"亿元，查看公式和输入")+'"':"";
    return '<'+tag+attrs+' class="waterline"><span>'+esc(x.name)+'</span><span class="axis"><i class="zero" style="left:'+pos(0)+'%"></i><i class="delta '+(!x.key?"total":x.value>=0?"pos":"neg")+'" style="left:'+pos(Math.min(x.from,x.to))+'%;width:'+(100*Math.abs(x.to-x.from)/range)+'%"></i></span><strong>'+(x.key&&x.value>0?"+":"")+money(x.value)+'</strong></'+tag+'>';
  }).join("")+'</div>';
}
function bridgePanel(){
  const historical=d.bridges.filter(b=>b.kind==="quarter_change" && b.input_record_ids.every(id=>recordMap.get(id)?.available_at<=(r?.as_of??d.as_of))).sort((a,b)=>b.period.localeCompare(a.period));
  const base=r?d.runs.find(x=>x.period===r.period && x.origin===r.origin && x.model===r.model && x.scenario==="base"):null;
  const retrospective=base?d.bridges.filter(b=>b.kind==="forecast_error" && b.run_id===base.run_id):[];
  const options=[...historical,...retrospective];
  if(!options.length)return card("营业利润差异桥",empty("当前可比数据不足以生成完整的八项分解。"));
  const key=b=>b.kind+"|"+b.period;
  if(!options.some(b=>key(b)===bridgeSelection))bridgeSelection=key(options[0]);
  const b=options.find(b=>key(b)===bridgeSelection);
  const names=Object.fromEntries(options.map(z=>[key(z),z.kind==="quarter_change"?z.from_period+" → "+z.period:z.period+" 基准情景预测 → 首次实际（事后）"]));
  if(!(driverKey in b.effects))driverKey=Object.keys(b.effects)[0];
  const related={Q:["Q","Rv","Cv"],P:["Q","Rv"],C:["Q","Cv"]}[driverKey]||[driverKey];
  const ids=b.input_record_ids.filter(id=>related.includes(recordMap.get(id)?.metric));
  const difference=b.end_op-b.start_op;
  const note=d.case_notes?.[b.period];
  const commentary=b.kind==="quarter_change" && note && note.comparison===b.from_period &&
    sources.get(note.source_id)?.available_at<=(r?.as_of??d.as_of) ?
    '<h3>公司披露的费用解释</h3><p>'+esc(note.management)+'</p><p class="note">'+esc(note.locator)+" · "+source(note.source_id)+'</p><h3>待核实问题</h3><p>'+esc(note.question)+'</p>':"";
  return card("营业利润差异桥",env("单位为亿元。点击贡献项可查看公式与输入。"+
      (b.kind==="forecast_error"?"事后对照纳入截至报告日已公开的首次实际。":"两期实际选用在本次预测时已公开的资料。"))+
    water(b)+'<div class="callout">'+esc(labels[driverKey])+"贡献 "+money(b.effects[driverKey])+" 亿元。"+esc(formulas[driverKey])+"。"+
    details("查看该项相关输入及原始来源",records(ids))+'</div>'+env("营业利润变化 "+money(difference)+" 亿元，八项贡献合计 "+money(Object.values(b.effects).reduce((a,x)=>a+x,0))+" 亿元。计算残差 "+num(b.residual,4)+" 元。")+commentary,picker("bridge","比较对象",options.map(key),bridgeSelection,names));
}
function financials(){
  const qPeriods=unique(visibleF.filter(x=>x.scope==="GROUP" && x.metric==="OP").map(x=>x.period)).sort().reverse();
  const last=qPeriods[0], prior=qPeriods[1], M=new Map(visibleF.filter(x=>x.scope==="GROUP").map(x=>[x.period+x.metric,x]));
  const keys=["Rv","Ro","Revenue","Cv","Co","Cost","GP","RD","SG","OI","OP"];
  const actual=(p,k)=>M.get(p+k);
  const rows=keys.map(k=>[esc(labels[k]),money(actual(prior,k)?.value),money(actual(last,k)?.value),money(r?.values[k]),actual(last,k)?source(actual(last,k).source_id):"—"]);
  const v=r?.values||{};
  const ratio=(n,z)=>z?num(100*n/z,1)+"%":"—";
  const summary='<div class="kpis">'+kpi("预测合并收入",money(v.Revenue),"亿元")+kpi("预测车辆毛利率",ratio(v.Rv-v.Cv,v.Rv),"车辆收入减车辆成本，占车辆收入")+kpi("预测综合毛利率",ratio(v.GP,v.Revenue),"毛利占总收入")+kpi("预测营业利润",money(v.OP),"亿元 · US GAAP")+'</div>';
  const statement=card("季度经营损益",env("金额单位为亿元。已披露实际按所选信息时点取数，费用按正数列示。")+
    table(["项目",prior?prior+" 实际":"上期实际",last?last+" 实际":"最近实际",r?r.period+" 预测":"预测","最近实际来源"],rows));
  return summary+statement+bridgePanel()+sensitivityPanel();
}
function sensitivityPanel(){
  const x=r?.drivers;
  if(!x || x.P==null)return card("经营敏感性",empty("所选预测缺少完整财务驱动。"));
  const fixed=x.Ro-x.Co-x.RD-x.SG+x.OI,margin=x.P-x.C;
  const levels=[-1000,0,1000];
  const rows=[.9,1,1.1].map(change=>{
    const q=Math.round(x.Q*change);
    return [num(q),...levels.map(k=>money(q*(margin+k)+fixed))];
  });
  const singles=[["交付 +1,000 辆",money(1000*margin)],["单位收入 +1,000 元/辆",money(x.Q*1000)],["单位成本 +1,000 元/辆",money(-x.Q*1000)],["销管费用 +1 亿元","−1.00"]];
  return '<div class="grid">'+card("交付量与单位毛利的敏感性",env("表内为营业利润（亿元）。列为单位毛利（元/辆）；其他收入、其他成本、研发、销管及其他经营收益保持本次预测水平。")+
    table(["交付量（辆）",...levels.map(k=>num(margin+k)+" 元/辆")],rows))+card("单一驱动变化",env("固定其余驱动，列示营业利润相对当前情景的变化。")+table(["假设调整","利润变化（亿元）"],singles))+'</div>';
}
function md(text){
  // Minimal, escaped Markdown subset for the accompanying offline brief.
  return String(text||"").split(/\r?\n/).map(line=>{
    if(!line.trim())return "";
    const fmt=v=>esc(v).replace(/\*\*([^*]+)\*\*/g,"<strong>$1</strong>");
    if(/^#{1,6}\s/.test(line))return '<h3>'+fmt(line.replace(/^#{1,6}\s+/,""))+'</h3>';
    if(/^[-*] /.test(line))return '<p>• '+fmt(line.slice(2))+'</p>';
    return '<p>'+fmt(line)+'</p>';
  }).join("");
}
function reportPage(){
  const phaseKeys=unique(d.performance.map(z=>z.split)).sort();
  if(!phaseKeys.includes(split))split=phaseKeys[0]||"development";
  const h=r?.origin==="Live"?"T"+r.monthly.filter(m=>m.kind==="actual").length:(r?.origin||"T0");
  const metricKey=scoreMetric;
  const matches=d.performance.filter(x=>x.split===split && x.metric===metricKey);
  const perf=table(["时点","方法","季度数","MAE（"+unit(metricKey)+"）",metricKey==="OP"?"利润方向误判":"WAPE","平均有符号误差","落入情景范围"],matches.map(x=>[
    esc(x.origin),esc(models[x.model]),num(x.n),val(x.mae,metricKey),metricKey==="OP"?num(x.direction_misses):num(100*x.wape,1)+"%",val(x.bias,metricKey),num(100*x.scenario_coverage,1)+"%"
  ]));
  const bad=d.failures.filter(x=>x.metric===metricKey && d.scores.some(s=>s.run_id===x.run_id && s.split===split)).sort((a,b)=>b.excess_absolute_error_vs_ma3-a.excess_absolute_error_vs_ma3);
  const failure=bad.length?table(["季度","时点","实际减预测","比三月均值多出的绝对误差"],bad.map(x=>[esc(x.period),esc(x.origin),val(x.error,metricKey),val(x.excess_absolute_error_vs_ma3,metricKey)])):empty("该筛选下没有可展示的候选模型落后案例。");
  const recent=d.scores.filter(x=>x.period===period && x.origin===h && x.model===model && x.metric===metricKey);
  const verdict=recent.length?table(["季度","历史时点","基准情景预测","首次实际","误差","最新可用实际","首次实际来源"],recent.map(s=>[esc(s.period),esc(s.origin),val(s.forecast,s.metric),val(s.actual,s.metric),val(s.error,s.metric),val(s.revised_actual,s.metric),refs(s.source_ids)])):empty("所选季度与时点暂无可评分的首次实际。T3 交付已知，仅评价财务。");
  const g=recent[0]?.guidance_comparison;
  let comparison="";
  if(g){
    const note=g.guidance_type==="approximate_point"?"实际相对公司约数的差额 "+val(g.delta_to_point,metricKey)+" "+unit(metricKey):
      (g.within?"实际落入已披露指引区间":(g.below_low_by?"实际低于下限 "+val(g.below_low_by,metricKey):"实际高于上限 "+val(g.above_high_by,metricKey))+" "+unit(metricKey));
    comparison='<p class="note">'+esc(note)+"。"+source(g.source_id)+'</p>';
  }
  const example=bad[0];
  let reviewCase="";
  if(example){
    const s=d.scores.find(x=>x.run_id===example.run_id && x.metric===metricKey);
    const frozen=d.runs.find(x=>x.run_id===example.run_id);
    const unitName=unit(metricKey);
    reviewCase='<h3>误差复盘 · '+esc(example.period)+" "+esc(example.origin)+'</h3>'+
      '<p>原假设沿用上年同月交付，并按最近三个月同比调整。冻结于 '+cnDate(frozen.as_of,true)+
      '，当期'+esc(labels[metricKey])+"估计为 "+val(s.forecast,metricKey)+" "+unitName+
      "。首次实际为 "+val(s.actual,metricKey)+" "+unitName+"，比三个月均值方法多出 "+val(example.excess_absolute_error_vs_ma3,metricKey)+
      " "+unitName+" 的绝对误差。</p>"+
      '<p class="note">下次月交付披露时，检查新披露月份同比与原窗口的差距，并与均值基准并列更新。季度财务公布后再核对单位收入、单位成本和费用假设。</p>'+
      '<button type="button" class="jump" data-jump="'+esc(example.run_id)+'">回看该次预测与输入</button>';
  }
  const audit=d.audit||{};
  const check={passed:"规则检查通过",template_fallback:"规则检查触发模板回退"}[audit.rule_status]||"规则检查记录未附";
  const human={pending:"待人工复核",approved:"已人工复核"}[audit.human_status]||"人工复核状态未附";
  const caseFacts=(d.facts||[]);
  return card("共同样本滚动验证",env("评价采用截至 "+cnDate(d.as_of)+" 的披露。每个时点和指标使用三种方法均有预测的相同季度。误差方向为实际减预测。")+
    '<div class="duo">'+picker("split","验证区间",phaseKeys,split,splits)+picker("scoreMetric","评价指标",["Q","Revenue","OP"],metricKey,labels)+'</div>'+perf+reviewCase+
    details("候选模型落后季度（"+bad.length+"）",env("单位为"+unit(metricKey)+"。候选模型为上年同月乘以近期同比；比较对象为最近三个月均值。")+failure))+
    card("所选季度的冻结预测复盘",env("取上述季度对应 "+h+" 的基准情景，与首次披露实际比较。单位为"+unit(metricKey)+"。")+verdict+comparison)+
    card("最新经营简报",env("此页简报对应报告截止日 "+cnDate(d.as_of)+"。历史时点通过上方复盘表回看。"+check+" · "+human+"。")+'<article class="brief">'+md(d.brief)+'</article>'+
      details("查看简报事实与引用（"+caseFacts.length+"）",table(["事实编号","期间 / 类型","指标","标准值与单位","公式","来源"],caseFacts.map(x=>[esc(x.fact_id),esc(x.period+" / "+x.kind),esc(labels[x.metric]||x.metric),esc(num(x.value,2)+" "+x.unit),esc(x.formula),refs(idList(x.source_ids))]))))+
    assumptions();
}
function idList(x){if(Array.isArray(x))return x;try{return JSON.parse(x);}catch{return x?[x]:[];}}
function draw(){
  get("context").textContent=r?period+"  ·  "+origins[origin]+"  ·  "+models[model]+"  ·  "+scenarios[scenario]+"  ·  运行截止 "+cnDate(r.as_of,true):"当前信息集暂无可运行的预测。";
  get("panel").setAttribute("aria-labelledby","tab-"+tab);
  get("panel").innerHTML=[deliveryPage,financials,reportPage][tab]();
  for(const btn of document.querySelectorAll(".tabs button")){
    const active=+btn.dataset.tab===tab;
    btn.setAttribute("aria-selected",String(active));btn.tabIndex=active?0:-1;
  }
}
function activate(n){tab=n;draw();}
async function start(){
  if(typeof DecompressionStream!=="function")throw new Error("此浏览器无法解压离线数据。请用较新版本的 Chrome、Edge、Firefox 或 Safari 打开本 HTML。");
  const binary=atob(get("payload").textContent.trim());
  const bytes=Uint8Array.from(binary,ch=>ch.charCodeAt(0));
  d=await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"))).json();
  sources=new Map(d.sources.map(s=>[s.source_id,s]));recordMap=new Map([...d.monthly,...d.quarterly].map(z=>[z.record_id,z]));
  get("reportDate").textContent=cnDate(d.as_of);
  get("print").onclick=()=>window.print();
  filter();draw();
  document.addEventListener("change",e=>{
    const {id,value}=e.target;
    if(["period","origin","model","scenario"].includes(id)){
      if(id==="period"){period=value;origin=undefined;}if(id==="origin")origin=value;
      if(id==="model")model=value;if(id==="scenario")scenario=value;
      filter();bridgeSelection="";
    }else if(id==="scope")scope=value;
    else if(id==="bridge")bridgeSelection=value;
    else if(id==="split")split=value;
    else if(id==="scoreMetric")scoreMetric=value;
    else if(id==="industry")industryKey=value;
    else return;
    draw();
    if(get(id))get(id).focus({preventScroll:true});
  });
  document.addEventListener("click",e=>{
    const btn=e.target.closest("button");if(!btn)return;
    if(btn.dataset.tab!==undefined)activate(+btn.dataset.tab);
    if(btn.dataset.jump){
      const picked=d.runs.find(x=>x.run_id===btn.dataset.jump);
      if(picked){period=picked.period;model=picked.model;origin=picked.origin;scenario=picked.scenario;filter();bridgeSelection="forecast_error|"+period;activate(1);get("tab-1").focus();}
    }
    if(btn.dataset.driver){driverKey=btn.dataset.driver;draw();document.querySelector('[data-driver="'+CSS.escape(driverKey)+'"]')?.focus({preventScroll:true});}
  });
  document.querySelector(".tabs").addEventListener("keydown",e=>{
    const offsets={ArrowLeft:-1,ArrowRight:1,Home:-tab,End:2-tab};
    if(!(e.key in offsets))return;
    e.preventDefault();activate((tab+offsets[e.key]+3)%3);get("tab-"+tab).focus();
  });
}
start().catch(error=>{get("panel").textContent="看板未能载入。"+error.message;});
