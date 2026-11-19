/* The flight field and aircraft are local geometry. No map token or tile service is required. */
"use strict";
(() => {
  const C = Cesium;
  const $ = id => document.getElementById(id);
  window.addEventListener("error",event=>{$("error").hidden=false;$("error").textContent=`Flight view error: ${event.message}`;});
  const model = {history: [], telemetry: {}, mission: {}, camera: "overview", replay: false, playing: false, cursor: 0, exporting: false, offline: false};
  const options=new URLSearchParams(location.search);
  let autoplay=options.get("replay")==="1";
  window.flightView = model;
  const viewer = new C.Viewer("scene", {
    animation: false, timeline: false, geocoder: false, homeButton: false,
    navigationHelpButton: false, sceneModePicker: false, baseLayerPicker: false,
    fullscreenButton: false, infoBox: false, selectionIndicator: false,
    baseLayer: false, imageryProvider: false, terrainProvider: new C.EllipsoidTerrainProvider(),
    skyBox: false, skyAtmosphere: false, shadows: false,
    contextOptions: {webgl: {preserveDrawingBuffer: true}},
  });
  window.flightViewer = viewer;
  viewer.imageryLayers.removeAll();
  viewer.scene.globe.baseColor = C.Color.fromCssColorString("#8a9e72");
  viewer.scene.globe.enableLighting = false;
  viewer.scene.backgroundColor = C.Color.fromCssColorString("#d3e4e9");
  if(viewer.scene.sun)viewer.scene.sun.show = false;
  if(viewer.scene.moon)viewer.scene.moon.show = false;
  viewer.scene.fog.enabled = false;
  viewer.scene.screenSpaceCameraController.minimumZoomDistance = 6;
  const green = C.Color.fromCssColorString("#eaf3db"), teal = C.Color.fromCssColorString("#49999f"), amber = C.Color.fromCssColorString("#ffbd56");
  let home = [40.414929, -86.932387, 149.1], frame, sceneKey = "", routeKey = "", quadPosition, quadOrientation;
  let trail = [];
  const routeEntities = [];

  function point(east, north, up=0) {
    return C.Matrix4.multiplyByPoint(frame, new C.Cartesian3(east, north, up), new C.Cartesian3());
  }
  function line(id, coordinates, color, width=1, dashed=false) {
    return viewer.entities.add({id, polyline: {positions: coordinates, width,
      material: dashed ? new C.PolylineDashMaterialProperty({color, dashLength: 12}) : color}});
  }
  function label(text, east, north, up=0.1, color=green) {
    return viewer.entities.add({position: point(east, north, up), label: {text, font: "bold 13px sans-serif",
      fillColor: color, outlineColor: C.Color.fromCssColorString("#45614b"), outlineWidth: 2,
      style: C.LabelStyle.FILL_AND_OUTLINE, disableDepthTestDistance: Number.POSITIVE_INFINITY}});
  }
  function groundTexture() {
    const canvas = document.createElement("canvas"); canvas.width = canvas.height = 1024;
    const ctx = canvas.getContext("2d"); ctx.fillStyle = "#90a879"; ctx.fillRect(0, 0, 1024, 1024);
    for (let x=0; x<1024; x+=64) {ctx.fillStyle = x%128 ? "#8ba373" : "#95ac7d"; ctx.fillRect(x,0,64,1024);}
    let seed = 29;
    for (let i=0; i<24000; i++) {
      seed=(seed*16807)%2147483647; const x=seed%1024;
      seed=(seed*16807)%2147483647; const y=seed%1024;
      ctx.fillStyle = i%2 ? "#ffffff0a" : "#23391f0b"; ctx.fillRect(x,y,2,2);
    }
    return canvas;
  }
  function field() {
    frame = C.Transforms.eastNorthUpToFixedFrame(C.Cartesian3.fromDegrees(home[1],home[0],home[2]));
    viewer.entities.removeAll(); routeEntities.length=0; trail=[]; routeKey="";
    const orientation = C.Transforms.headingPitchRollQuaternion(point(0,0), new C.HeadingPitchRoll());
    viewer.entities.add({id:"ground",position:point(0,0,-0.12),orientation,
      plane:{plane:new C.Plane(C.Cartesian3.UNIT_Z,0),dimensions:new C.Cartesian2(1600,1600),
        material:new C.ImageMaterialProperty({image:groundTexture()})}});
    viewer.entities.add({id:"runway",position:point(-38,20,-0.07),orientation,
      box:{dimensions:new C.Cartesian3(18,650,0.08),material:C.Color.fromCssColorString("#747e76")}});
    for (let n=-270;n<330;n+=30) {
      line(`stripe-${n}`,[point(-38,n,0),point(-38,n+15,0)],C.Color.fromCssColorString("#eef0d5"),3);
    }
    for (let k=-80;k<=120;k+=10) {
      const color=C.Color.fromCssColorString(k%50===0 ? "#e6edd680" : "#d9e6cc35");
      line(`grid-n-${k}`,[point(-60,k,0.015),point(130,k,0.015)],color);
      line(`grid-e-${k}`,[point(k,-80,0.015),point(k,120,0.015)],color);
    }
    viewer.entities.add({id:"launch-pad",position:point(0,0,0),orientation,
      box:{dimensions:new C.Cartesian3(9,9,0.08),material:C.Color.fromCssColorString("#62735d")}});
    viewer.entities.add({position:point(0,0,0.06),ellipse:{semiMajorAxis:3.3,semiMinorAxis:3.3,
      height:home[2]+0.06,material:C.Color.TRANSPARENT,outline:true,outlineColor:green,outlineWidth:2}});
    label("H",0,0,0.12);
    label("N  ↑",0,65,0.15); label("E  →",65,0,0.15);
    viewer.entities.add({id:"trail",polyline:{positions:new C.CallbackProperty(()=>trail,false),width:3,material:amber}});
    quad();
    sceneKey=home.join(",");
  }
  function quad() {
    const baseRotation=()=>quadOrientation || C.Quaternion.IDENTITY;
    const basePosition=()=>quadPosition || point(0,0,0.6);
    function rotation(offset=0) {
      return new C.CallbackProperty(()=>{
        const extra=C.Quaternion.fromAxisAngle(C.Cartesian3.UNIT_Z,offset,new C.Quaternion());
        return C.Quaternion.multiply(baseRotation(),extra,new C.Quaternion());
      },false);
    }
    function position(x=0,y=0,z=0) {
      return new C.CallbackProperty(()=>{
        const r=C.Matrix3.fromQuaternion(baseRotation());
        const offset=C.Matrix3.multiplyByVector(r,new C.Cartesian3(x,y,z),new C.Cartesian3());
        return C.Cartesian3.add(basePosition(),offset,new C.Cartesian3());
      },false);
    }
    for(const angle of [Math.PI/4,-Math.PI/4]) viewer.entities.add({position:position(),orientation:rotation(angle),
      box:{dimensions:new C.Cartesian3(3.6,0.16,0.13),material:C.Color.fromCssColorString("#273d3c")}});
    viewer.entities.add({id:"quadcopter",position:position(0,0,0.12),orientation:rotation(),
      box:{dimensions:new C.Cartesian3(0.85,0.55,0.32),material:C.Color.fromCssColorString("#f6f6df")}});
    viewer.entities.add({position:position(0.45,0,0.15),orientation:rotation(),
      box:{dimensions:new C.Cartesian3(0.15,0.5,0.2),material:amber}});
    for (const x of [-1.2,1.2]) for(const y of [-1.2,1.2]) {
      viewer.entities.add({position:position(x,y,0.15),orientation:rotation(),
        cylinder:{length:0.045,topRadius:0.67,bottomRadius:0.67,
          material:C.Color.fromCssColorString(x>0 ? "#c6e6dfb0" : "#384d48b0"),outline:true,
          outlineColor:C.Color.fromCssColorString("#304b42")}});
    }
  }
  function plan() {
    const mission=model.mission;
    const key=JSON.stringify([mission.waypoints,mission.altitude,sceneKey]);
    if(key===routeKey)return;
    routeKey=key; for(const entity of routeEntities)viewer.entities.remove(entity);routeEntities.length=0;
    const waypoints=mission.waypoints||[[30,0],[30,30],[0,30]], altitude=mission.altitude||15;
    const corners=[[0,0],...waypoints,[0,0]];
    routeEntities.push(line("plan-ground",corners.map(([n,e])=>point(e,n,0.08)),teal.withAlpha(0.7),2,true));
    routeEntities.push(line("plan-air",corners.map(([n,e])=>point(e,n,altitude)),teal,2,true));
    waypoints.forEach(([n,e],i)=>{
      routeEntities.push(line(`drop-${i}`,[point(e,n,0.1),point(e,n,altitude)],teal.withAlpha(0.5),1,true));
      routeEntities.push(label(String(i+1),e,n,altitude+2,teal));
    });
    $("route-copy").textContent=`${waypoints.length} waypoints · ${altitude} m above home`;
    setCamera(model.camera);
  }
  function setCamera(mode) {
    model.camera=mode;
    document.querySelectorAll("[data-camera]").forEach(button=>button.classList.toggle("active",button.dataset.camera===mode));
    const wps=model.mission.waypoints||[[30,0],[30,30],[0,30]];
    const ns=[0,...wps.map(p=>p[0])], es=[0,...wps.map(p=>p[1])];
    const n=(Math.min(...ns)+Math.max(...ns))/2,e=(Math.min(...es)+Math.max(...es))/2;
    const span=Math.max(30,Math.max(...ns)-Math.min(...ns),Math.max(...es)-Math.min(...es));
    const center=mode==="follow"&&quadPosition ? quadPosition : point(e-4,n,3);
    viewer.camera.lookAt(center,new C.HeadingPitchRange(C.Math.toRadians(mode==="top"?0:135),C.Math.toRadians(mode==="top"?-90:-42),mode==="follow"?40:span*2.8+25));
    if(mode!=="follow")viewer.camera.lookAtTransform(C.Matrix4.IDENTITY);
  }
  function currentPose(sample) {
    if(!sample)return;
    quadPosition=C.Cartesian3.fromDegrees(sample.lon,sample.lat,sample.alt+0.5);
    quadOrientation=C.Transforms.headingPitchRollQuaternion(quadPosition,new C.HeadingPitchRoll(sample.yaw,sample.pitch,sample.roll));
    if(model.camera==="follow") viewer.camera.lookAt(quadPosition,new C.HeadingPitchRange(C.Math.toRadians(140),C.Math.toRadians(-30),40));
    $("altitude").innerHTML=`${Math.max(0,sample.alt-home[2]).toFixed(1)} <em>m</em>`;
    $("speed").innerHTML=`${sample.speed.toFixed(1)} <em>m/s</em>`;
    $("climb").innerHTML=`${sample.climb.toFixed(1)} <em>m/s</em>`;
    if(model.replay){$("phase").textContent="Flight replay";$("arming").textContent="Recorded flight";}
  }
  function receive(data) {
    if(model.offline)return;
    if(data.snapshot){model.telemetry=data.telemetry||{};model.history=data.history||[];}
    if(data.mav_data)model.telemetry[data.mav_data.mavpackettype]=data.mav_data;
    if(data.sample)model.history.push(data.sample);
    if(data.mission){
      model.mission=data.mission;
      if(data.mission.home){
        home=data.mission.home;
        if(home.join(",")!==sceneKey){field();}
      }
      plan();
      const phases={PREFLIGHT:"Preflight",ARM:"Arming",CLIMB:"Taking off",NAV:"Flying waypoints",RTH:"Returning home",LAND:"Landing",SETTLE:"Touchdown",DISARM:"Disarming",COMPLETE:"Flight complete",FAILED:"Flight failed"};
      $("phase").textContent=phases[data.mission.phase]||"Waiting for aircraft";
      $("waypoints").innerHTML=`${data.mission.waypoint_index||0} <em>/ ${(data.mission.waypoints||[]).length}</em>`;
    }
    const hb=model.telemetry.HEARTBEAT;
    if(hb){$("mode").textContent=hb.custom_mode===1?"ANGLE":hb.custom_mode===0?"ACRO":`Mode ${hb.custom_mode}`;$("arming").textContent=hb.base_mode&128?"Armed":"Disarmed";}
    if(model.mission.phase==="COMPLETE")$("arming").textContent="Disarmed";
    if(!model.replay&&(data.sample||data.snapshot||data.mission)){
      trail=model.history.map(sample=>C.Cartesian3.fromDegrees(sample.lon,sample.lat,sample.alt+0.15));
      currentPose(model.history.at(-1));
    }
    $("replay").disabled=model.history.length<2;
    $("export-video").disabled=model.exporting||model.history.length<2||model.mission.phase!=="COMPLETE";
  }
  const mmss=seconds=>`${String(Math.floor(seconds/60)).padStart(2,"0")}:${String(Math.floor(seconds%60)).padStart(2,"0")}`;
  let previous=performance.now();
  function animate(now) {
    const dt=Math.min(0.25,(now-previous)/1000);previous=now;
    const end=model.history.at(-1)?.t||0;
    if(model.replay&&model.history.length){
      if(model.playing)model.cursor+=dt*Number($("playback-speed").value);
      if(model.cursor>=end){model.cursor=end;model.playing=false;$("replay").textContent="▶ Replay flight";}
      const index=Math.max(0,model.history.findIndex(p=>p.t>=model.cursor));
      const next=model.history[index], before=model.history[Math.max(0,index-1)];
      const fraction=next.t>before.t?Math.max(0,Math.min(1,(model.cursor-before.t)/(next.t-before.t))):1;
      const pose={...next};
      for(const key of ["lat","lon","alt","roll","pitch","speed","climb"])pose[key]=before[key]+fraction*(next[key]-before[key]);
      pose.yaw=before.yaw+fraction*Math.atan2(Math.sin(next.yaw-before.yaw),Math.cos(next.yaw-before.yaw));
      currentPose(pose);
      trail=model.history.slice(0,index+1).map(p=>C.Cartesian3.fromDegrees(p.lon,p.lat,p.alt+0.15));
      if(trail.length)trail[trail.length-1]=C.Cartesian3.fromDegrees(pose.lon,pose.lat,pose.alt+0.15);
    }else{model.cursor=end;}
    $("timeline").max=String(Math.max(end,1));$("timeline").value=String(model.cursor);
    $("elapsed").textContent=`${mmss(model.cursor)} / ${mmss(end)}`;
    requestAnimationFrame(animate);
  }
  function playback(active) {
    model.replay=active;$("live").classList.toggle("active",!active);$("replay").classList.toggle("active",active);
  }
  $("replay").onclick=()=>{playback(true);if(!model.playing&&model.cursor>= (model.history.at(-1)?.t||0))model.cursor=0;model.playing=!model.playing;$("replay").textContent=model.playing?"Ⅱ Pause":"▶ Replay flight";};
  $("live").onclick=()=>{playback(false);model.playing=false;$("replay").textContent="▶ Replay flight";receive({mission:model.mission});};
  $("timeline").oninput=event=>{playback(true);model.playing=false;model.cursor=Number(event.target.value);$("replay").textContent="▶ Replay flight";};
  function loadRecording(data) {
    const fields=["t","lat","lon","alt","roll","pitch","yaw","speed","climb"];
    if(!data||!Array.isArray(data.history)||data.history.length<2||!data.mission||
       !Array.isArray(data.mission.home)||data.mission.home.length!==3||!data.mission.home.every(Number.isFinite)||
       data.history.some((p,i)=>fields.some(key=>!Number.isFinite(p[key]))||p.t<0||(i&&p.t<=data.history[i-1].t)))
      throw new Error("Choose a FastDyn flight recording JSON with valid, increasing flight samples.");
    if(model.exporting)throw new Error("Wait for the video export to finish before opening a recording.");
    model.offline=false;receive({...data,snapshot:true});model.offline=true;
    playback(true);model.playing=false;model.cursor=0;
    $("connection").textContent="Saved recording";$("connection-dot").style.background="#ae975b";
    $("error").hidden=true;
  }
  function videoType() {
    if(!window.MediaRecorder||!HTMLCanvasElement.prototype.captureStream)return null;
    return ["video/mp4;codecs=avc1.42001E","video/mp4","video/webm;codecs=vp9","video/webm;codecs=vp8"]
      .find(type=>MediaRecorder.isTypeSupported(type))||null;
  }
  function videoFrame(canvas) {
    const ctx=canvas.getContext("2d"), source=viewer.scene.canvas;
    const scale=Math.min(canvas.width/source.width,canvas.height/source.height);
    const w=source.width*scale,h=source.height*scale;
    ctx.fillStyle="#d3e4e9";ctx.fillRect(0,0,canvas.width,canvas.height);
    ctx.drawImage(source,(canvas.width-w)/2,(canvas.height-h)/2,w,h);
    ctx.fillStyle="#f9fcf7f2";ctx.beginPath();ctx.roundRect(18,18,canvas.width-36,53,9);ctx.fill();
    ctx.fillStyle="#244f43";ctx.font="600 20px sans-serif";ctx.fillText("FastDyn / Betaflight",34,51);
    ctx.font="14px sans-serif";ctx.fillText("Recorded simulation",canvas.width-185,50);
    ctx.fillStyle="#fbfdf8ee";ctx.beginPath();ctx.roundRect(18,86,225,136,9);ctx.fill();
    ctx.fillStyle="#244f43";ctx.font="600 19px sans-serif";ctx.fillText("Flight replay",34,115);
    ctx.font="15px sans-serif";
    ctx.fillText(`Height: ${$("altitude").textContent.trim()}`,34,144);
    ctx.fillText(`Ground speed: ${$("speed").textContent.trim()}`,34,169);
    ctx.fillText(`${mmss(model.cursor)} / ${mmss(model.history.at(-1).t)}`,34,199);
    ctx.fillStyle="#244f43";ctx.font="13px sans-serif";
    ctx.fillText("Teal: planned route   Amber: recorded flight path",24,canvas.height-24);
  }
  async function exportVideo() {
    if(model.exporting)return;
    const mimeType=videoType();
    if(!mimeType)throw new Error("This browser cannot record video. Open this page in Chrome, Edge or Firefox, or use the MP4 export command.");
    if(model.history.length<2||model.mission.phase!=="COMPLETE")throw new Error("Wait for flight completion before exporting the recording.");
    const end=model.history.at(-1).t;
    const canvas=document.createElement("canvas");canvas.width=1280;canvas.height=720;
    const stream=canvas.captureStream(24), chunks=[];
    const recorder=new MediaRecorder(stream,{mimeType,videoBitsPerSecond:4000000});
    const saved={replay:model.replay,playing:model.playing,cursor:model.cursor};
    model.exporting=true;model.exportError=null;model.exportProgress=0;
    $("export-video").disabled=true;$("load-recording").disabled=true;
    const controls=[$("live"),$("replay"),$("timeline"),$("playback-speed")];
    controls.forEach(control=>control.disabled=true);
    let timer;
    try {
      await new Promise((resolve,reject)=>{
        recorder.ondataavailable=event=>{if(event.data.size)chunks.push(event.data);};
        recorder.onerror=event=>{clearInterval(timer);reject(event.error||new Error("Video encoding failed"));};
        recorder.onstop=()=>{clearInterval(timer);resolve();};
        playback(true);model.playing=false;model.cursor=0;
        const begin=performance.now();
        recorder.start(1000);
        timer=setInterval(()=>{
          model.cursor=Math.min(end,(performance.now()-begin)/1000);
          model.exportProgress=Math.min(100,Math.round(model.cursor/end*100));
          $("export-video").textContent=`Exporting ${model.exportProgress}%`;
          videoFrame(canvas);
          if(model.cursor>=end)recorder.stop();
        },1000/24);
      });
      const blob=new Blob(chunks,{type:mimeType.split(";")[0]});
      if(!blob.size)throw new Error("Video export produced an empty file.");
      const link=document.createElement("a"), url=URL.createObjectURL(blob);
      link.href=url;link.download=`betaflight-flight-${new Date().toISOString().replace(/[:.]/g,"-")}.${mimeType.startsWith("video/mp4")?"mp4":"webm"}`;
      document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);
    }catch(error){model.exportError=error.message;throw error;}
    finally {
      clearInterval(timer);if(recorder.state!=="inactive")recorder.stop();stream.getTracks().forEach(track=>track.stop());
      model.exporting=false;playback(saved.replay);model.playing=saved.playing;model.cursor=saved.cursor;
      controls.forEach(control=>control.disabled=false);$("load-recording").disabled=false;
      $("export-video").disabled=false;$("export-video").textContent="Download video";
    }
  }
  $("load-recording").onclick=()=>$("recording-file").click();
  $("recording-file").onchange=async event=>{
    try{const file=event.target.files[0];if(file)loadRecording(JSON.parse(await file.text()));}
    catch(error){$("error").hidden=false;$("error").textContent=error.message;}
    event.target.value="";
  };
  $("export-video").onclick=()=>exportVideo().catch(error=>{$("error").hidden=false;$("error").textContent=error.message;});
  window.flightReplay={load:loadRecording,exportVideo,videoType,camera:setCamera};
  document.querySelectorAll("[data-camera]").forEach(button=>button.onclick=()=>setCamera(button.dataset.camera));
  function connect() {
    const protocol=location.protocol==="https:"?"wss:":"ws:";
    const ws=new WebSocket(`${protocol}//${location.host}${window.FLIGHT_PREFIX}websocket/`);
    ws.onopen=()=>{if(model.offline)return;$("connection").textContent="Connected to simulation";$("connection-dot").style.background="#4d9670";};
    ws.onmessage=event=>{try{
      const data=JSON.parse(event.data);
      if(autoplay&&data.snapshot&&data.history?.length>=2){
        autoplay=false;loadRecording(data);
        const camera=options.get("camera");if(["overview","top","follow"].includes(camera))setCamera(camera);
        model.playing=true;$("replay").textContent="Ⅱ Pause";
      }else receive(data);
    }catch(error){$("error").hidden=false;$("error").textContent=error.stack;}};
    ws.onclose=()=>{if(model.offline)return;$("connection-dot").style.background="#ae975b";$("connection").textContent=model.history.length?"Disconnected · recording available":"Waiting for simulation";if(model.mission.phase!=="COMPLETE")setTimeout(connect,4000);};
  }
  field();plan();connect();requestAnimationFrame(animate);
})();
