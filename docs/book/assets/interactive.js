/* Local telemetry replay; static figures and CSV remain available without JS. */
"use strict";
document.querySelectorAll(".mission-explorer").forEach(async element => {
  try {
    const response = await fetch(element.dataset.src);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json(), rows = data.rows.filter(r => r.time_s >= 0);
    const rover = data.summary?.vehicle === "rover";
    const valueKey = rover ? "ground_speed_mps" : "relative_altitude_m";
    const unit = rover ? "m/s" : "m";
    const valueLabel = rover ? "ground speed" : "altitude";
    const canvas = document.createElement("canvas");
    canvas.width = 900; canvas.height = 630;
    canvas.setAttribute("aria-label", `Mission track and ${valueLabel} at the selected time`);
    const controls = document.createElement("div"); controls.className = "plot-controls";
    const play = document.createElement("button"); play.textContent = "Replay";
    const slider = document.createElement("input");
    slider.type = "range"; slider.min = "0"; slider.max = String(rows.length - 1);
    slider.value = slider.max; slider.setAttribute("aria-label", "Mission time");
    const readout = document.createElement("output"), label = document.createElement("label");
    const setpoint = document.createElement("input"); setpoint.type = "checkbox"; setpoint.checked = true;
    label.append(setpoint, " Altitude setpoint"); controls.append(play, slider, label);
    label.hidden = rover || !rows.some(row => Number.isFinite(row.altitude_setpoint_m));
    element.replaceChildren(controls, readout, canvas);
    const ctx = canvas.getContext("2d");
    const east = rows.map(r=>r.east_m).concat(data.waypoints.map(w=>w.east));
    const north = rows.map(r=>r.north_m).concat(data.waypoints.map(w=>w.north));
    const span = Math.max(Math.max(...east)-Math.min(...east), Math.max(...north)-Math.min(...north), 1)*1.2;
    const cx = (Math.max(...east)+Math.min(...east))/2, cy = (Math.max(...north)+Math.min(...north))/2;
    const mapX = x=>450+(x-cx)*290/span, mapY = y=>185-(y-cy)*290/span;
    const maxTime = rows.at(-1).time_s;
    const maxAlt = Math.max(...rows.map(r=>Math.max(r[valueKey] || 0,rover ? 0 : r.altitude_setpoint_m || 0)),1)*1.15;
    const timeX = x=>75+x/maxTime*770, altY = y=>565-y/maxAlt*180;
    function line(points,x,y,color) {
      ctx.strokeStyle=color; ctx.lineWidth=2; ctx.beginPath(); let started=false;
      for (const p of points) {
        if (!Number.isFinite(p[0]) || !Number.isFinite(p[1])) {started=false;continue;}
        if (started) ctx.lineTo(x(p[0]),y(p[1])); else ctx.moveTo(x(p[0]),y(p[1])); started=true;
      }
      ctx.stroke();
    }
    function draw() {
      const index=Number(slider.value), row=rows[index], visible=rows.slice(0,index+1);
      ctx.fillStyle="#fff";ctx.fillRect(0,0,900,630);ctx.fillStyle="#172b4d";ctx.font="18px sans-serif";
      ctx.fillText("Top-down trajectory · north up",290,25);
      ctx.fillText(rover ? "Ground speed" : "Altitude and controller setpoint",290,370);
      ctx.font="13px sans-serif";ctx.strokeStyle="#ccd5df";ctx.lineWidth=1;
      for(let i=0;i<=4;i++) {
        const value=i*maxAlt/4,y=altY(value);
        ctx.beginPath();ctx.moveTo(75,y);ctx.lineTo(845,y);ctx.stroke();ctx.fillText(value.toFixed(1)+" "+unit,12,y+4);
        const t=i*maxTime/4;ctx.fillText(t.toFixed(0)+" s",timeX(t)-12,590);
      }
      line(rows.map(r=>[r.east_m,r.north_m]),mapX,mapY,"#d1d9e0");
      line(visible.map(r=>[r.east_m,r.north_m]),mapX,mapY,"#2374ab");
      data.waypoints.forEach(w=>{const x=mapX(w.east),y=mapY(w.north);ctx.fillStyle="#b86900";ctx.fillRect(x-3,y-3,6,6);ctx.fillText(String(w.seq),x+6,y-5);});
      ctx.fillStyle="#ce3048";ctx.beginPath();ctx.arc(mapX(row.east_m),mapY(row.north_m),5,0,Math.PI*2);ctx.fill();
      ctx.fillStyle="#172b4d";ctx.fillText(`Map width: ${span.toFixed(0)} m · numbered mission waypoints`,245,345);
      line(visible.map(r=>[r.time_s,r[valueKey]]),timeX,altY,"#2374ab");
      if(!rover && setpoint.checked) line(visible.map(r=>[r.time_s,r.altitude_setpoint_m]),timeX,altY,"#d17a00");
      ctx.strokeStyle="#ce3048";ctx.beginPath();ctx.moveTo(timeX(row.time_s),385);ctx.lineTo(timeX(row.time_s),565);ctx.stroke();
      ctx.fillStyle="#2374ab";ctx.fillText(`Measured ${valueLabel}`,75,618);
      if(!rover && !label.hidden){ctx.fillStyle="#d17a00";ctx.fillText("Controller setpoint",300,618);}
      readout.textContent=`Time ${row.time_s.toFixed(1)} s · ${valueLabel} ${Number.isFinite(row[valueKey]) ? row[valueKey].toFixed(2) : "unavailable"} ${unit} · mission item ${row.mission_item}`;
    }
    let timer=null;
    function stop(){clearInterval(timer);timer=null;play.textContent="Replay";}
    play.addEventListener("click",()=>{
      if(timer){stop();return;}
      if(Number(slider.value)>=rows.length-1) slider.value="0";
      play.textContent="Pause";
      timer=setInterval(()=>{slider.value=String(Math.min(Number(slider.value)+1,rows.length-1));draw();if(Number(slider.value)===rows.length-1)stop();},70);
    });
    slider.addEventListener("input",()=>{stop();draw();});setpoint.addEventListener("change",draw);
    canvas.addEventListener("pointerdown",event=>{
      const rect=canvas.getBoundingClientRect(),x=(event.clientX-rect.left)*900/rect.width,y=(event.clientY-rect.top)*630/rect.height;
      if(y<385 || y>590)return;
      const t=Math.max(0,Math.min(maxTime,(x-75)/770*maxTime));
      slider.value=String(rows.reduce((best,r,i)=>Math.abs(r.time_s-t)<Math.abs(rows[best].time_s-t)?i:best,0));stop();draw();
    });
    draw();
  } catch(error) {element.textContent=`Interactive view unavailable (${error.message}). Use the static plot and CSV below.`;}
});
