/* Explore all recorded trajectories; selection highlights a run without hiding the others. */
"use strict";
document.querySelectorAll(".ensemble-explorer").forEach(async element => {
  try {
    const response = await fetch(element.dataset.src);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const colors = {reference:"#172b4d",passed:"#2374ab",flight_failure:"#c83249",mission_incomplete:"#b56c00",run_error:"#8e5baf"};
    const allRows = data.runs.flatMap(run=>run.rows);
    if (!allRows.length) throw new Error("No position telemetry has been recorded yet");
    const controls = document.createElement("label"); controls.textContent = "Highlight a run: ";
    const selector = document.createElement("select");
    selector.setAttribute("aria-label", "Highlight a payload study run");
    selector.add(new Option("All trajectories", ""));
    data.runs.forEach(run=>selector.add(new Option(`${run.id} · ${(1000*run.payload_mass_kg).toFixed(1)} g · ${run.status.replaceAll("_"," ")}`,run.id)));
    controls.append(selector);
    const status = document.createElement("p"); status.setAttribute("role","status");
    const plots = document.createElement("div"); plots.className="ensemble-plots";
    const legend = document.createElement("div"); legend.className="ensemble-legend";
    for(const [key,color] of Object.entries(colors)) if(data.runs.some(run=>run.status===key)) {
      const label=document.createElement("span");label.textContent="━ "+key.replaceAll("_"," ");label.style.color=color;legend.append(label);
    }
    element.replaceChildren(controls,status,plots,legend);
    function svgElement(name,attributes={}) {
      const node=document.createElementNS("http://www.w3.org/2000/svg",name);
      for(const [key,value] of Object.entries(attributes))node.setAttribute(key,String(value));
      return node;
    }
    function label(svg,x,y,text,anchor="middle") {
      const node=svgElement("text",{x,y,"text-anchor":anchor,fill:"#172b4d","font-size":12});
      node.textContent=text;svg.append(node);
    }
    const east=allRows.map(r=>r.east_m).concat(data.waypoints.map(w=>w.east));
    const north=allRows.map(r=>r.north_m).concat(data.waypoints.map(w=>w.north));
    const bounds=values=>values.reduce((b,v)=>[Math.min(b[0],v),Math.max(b[1],v)],[Infinity,-Infinity]);
    const [eastMin,eastMax]=bounds(east),[northMin,northMax]=bounds(north);
    const span=Math.max(eastMax-eastMin,northMax-northMin,1)*1.15;
    const centerX=(eastMin+eastMax)/2,centerY=(northMin+northMax)/2;
    const maxTime=Math.max(1,...allRows.map(r=>r.time_s));
    const [altMin,altMax]=bounds(allRows.map(r=>r.relative_altitude_m));
    const bottom=Math.min(0,altMin),top=Math.max(1,altMax)*1.08;
    const mapX=e=>60+((e-centerX)/span+.5)*320,mapY=n=>350-((n-centerY)/span+.5)*320;
    const timeX=t=>60+t/maxTime*320,altY=a=>350-(a-bottom)/(top-bottom)*320;
    const groups=[];
    function makePlot(kind) {
      const svg=svgElement("svg",{viewBox:"0 0 410 400",role:"img","aria-label":kind==="track"?"All Monte Carlo trajectories":"All Monte Carlo altitude traces"});
      svg.append(svgElement("rect",{width:410,height:400,fill:"white"}));
      label(svg,215,18,kind==="track"?"All trajectories · north up":"All altitude traces");
      for(let i=0;i<=4;i++) {
        const x=60+i*80,y=350-i*80;
        svg.append(svgElement("line",{x1:x,y1:30,x2:x,y2:350,stroke:"#e0e5eb"}),svgElement("line",{x1:60,y1:y,x2:380,y2:y,stroke:"#e0e5eb"}));
        label(svg,x,370,(kind==="track"?centerX+(i/4-.5)*span:i/4*maxTime).toFixed(0));
        label(svg,52,y+4,(kind==="track"?centerY+(i/4-.5)*span:bottom+i/4*(top-bottom)).toFixed(0),"end");
      }
      label(svg,220,393,kind==="track"?"East of mission home (m)":"Time since arming (s)");
      const ylabel=svgElement("text",{transform:"translate(14 200) rotate(-90)","text-anchor":"middle",fill:"#172b4d","font-size":12});
      ylabel.textContent=kind==="track"?"North of mission home (m)":"Altitude above home (m)";svg.append(ylabel);
      if(kind==="track") {
        svg.append(svgElement("polyline",{points:data.waypoints.map(w=>`${mapX(w.east)},${mapY(w.north)}`).join(" "),fill:"none",stroke:"#bf7900","stroke-dasharray":"5 4"}));
        data.waypoints.forEach(w=>{svg.append(svgElement("circle",{cx:mapX(w.east),cy:mapY(w.north),r:3,fill:"#bf7900"}));label(svg,mapX(w.east)+7,mapY(w.north)-6,String(w.seq),"start");});
      }
      for(const run of [...data.runs].sort((a,b)=>(a.status==="reference")-(b.status==="reference"))) {
        const group=svgElement("g",{"data-run-id":run.id});
        const xy=run.rows.map(row=>kind==="track"?[mapX(row.east_m),mapY(row.north_m)]:[timeX(row.time_s),altY(row.relative_altitude_m)]);
        const path=svgElement("polyline",{points:xy.map(pair=>pair.join(",")).join(" "),fill:"none",stroke:colors[run.status],"stroke-width":run.status==="reference"?2.5:1.5});
        const title=svgElement("title");title.textContent=`${run.id}: ${(1000*run.payload_mass_kg).toFixed(1)} g payload; ${run.status.replaceAll("_"," ")}`;path.append(title);group.append(path);
        if(xy.length && !["passed","reference"].includes(run.status)) {
          const [x,y]=xy.at(-1);group.append(svgElement("path",{d:`M${x-4},${y-4}l8,8m-8,0l8,-8`,stroke:colors[run.status],"stroke-width":2}));
        }
        svg.append(group);groups.push({group,run});
      }
      plots.append(svg);
    }
    makePlot("track");makePlot("altitude");
    function update() {
      const selected=data.runs.find(run=>run.id===selector.value);
      groups.forEach(({group,run})=>{group.style.opacity=selected?(run.id===selected.id?"1":".16"):(run.status==="passed"?".55":"1");});
      const missing=data.runs.filter(run=>!run.rows.length).length;
      status.textContent=selected?`${selected.id} · ${(1000*selected.payload_mass_kg).toFixed(1)} g payload · ${selected.reason || selected.status}. ${selected.rows.length} position samples.`:
        `${data.runs.length} completed runs · ${data.runs.length-missing} trajectories · ${missing} without position telemetry. Crosses mark where failed recordings end.`;
    }
    selector.addEventListener("change",update);update();
  } catch(error) {element.textContent=`Trajectory explorer unavailable (${error.message}). See the static plot and run table below.`;}
});
