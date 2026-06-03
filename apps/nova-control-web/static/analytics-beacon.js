(function(){
  "use strict";
  if(navigator.doNotTrack==="1"||window.__na)return;
  window.__na=1;

  var endpoint="https://analytics.digitalnoise.net";
  var site=location.hostname;
  var path=location.pathname;
  var screen=window.innerWidth<768?"mobile":window.innerWidth<1200?"tablet":"desktop";
  var referrer=document.referrer?"":(function(){try{return new URL(document.referrer).hostname}catch(e){return""}})();
  try{referrer=document.referrer?new URL(document.referrer).hostname:""}catch(e){referrer=""}

  function send(url,data){
    var body=JSON.stringify(data);
    if(navigator.sendBeacon){
      navigator.sendBeacon(url,new Blob([body],{type:"application/json"}));
    }else{
      fetch(url,{method:"POST",body:body,keepalive:true,headers:{"Content-Type":"application/json"}}).catch(function(){});
    }
  }

  function sendEvent(type,data){
    send(endpoint+"/collect/event",{site:site,path:path,event_type:type,event_data:data||{}});
  }

  // Page view
  send(endpoint+"/collect",{site:site,path:path,referrer:referrer,screen:screen});

  // Engagement time (heartbeat every 15s while visible)
  var engagementS=0;
  var heartbeatId=null;
  var isVisible=!document.hidden;

  function startHeartbeat(){
    if(heartbeatId)return;
    heartbeatId=setInterval(function(){
      if(isVisible)engagementS+=15;
    },15000);
  }

  function stopHeartbeat(){
    if(heartbeatId){clearInterval(heartbeatId);heartbeatId=null;}
  }

  document.addEventListener("visibilitychange",function(){
    isVisible=!document.hidden;
    if(isVisible){startHeartbeat();}else{stopHeartbeat();}
  });
  startHeartbeat();

  // Send engagement on unload
  function sendEngagement(){
    if(engagementS>0){
      sendEvent("engagement",{seconds:engagementS});
    }
  }
  document.addEventListener("visibilitychange",function(){
    if(document.hidden)sendEngagement();
  });
  window.addEventListener("pagehide",sendEngagement);

  // Scroll depth (25/50/75/100%)
  var scrollMarks={25:false,50:false,75:false,100:false};
  var scrollTimeout=null;
  function checkScroll(){
    var h=document.documentElement;
    var scrollable=h.scrollHeight-h.clientHeight;
    if(scrollable<=0)return;
    var pct=Math.round((window.scrollY/scrollable)*100);
    [25,50,75,100].forEach(function(mark){
      if(!scrollMarks[mark]&&pct>=mark){
        scrollMarks[mark]=true;
        sendEvent("scroll",{depth:mark});
      }
    });
  }
  window.addEventListener("scroll",function(){
    if(scrollTimeout)return;
    scrollTimeout=setTimeout(function(){scrollTimeout=null;checkScroll();},500);
  },{passive:true});

  // Outbound link clicks
  document.addEventListener("click",function(e){
    var link=e.target.closest("a[href]");
    if(!link)return;
    var href=link.getAttribute("href")||"";

    // Outbound link
    if(href.startsWith("http")&&!href.includes(location.hostname)){
      sendEvent("outbound_click",{url:href.substring(0,200)});
    }

    // File downloads
    var ext=(href.split("?")[0].split(".").pop()||"").toLowerCase();
    if(["pdf","zip","tar","gz","dmg","pkg","exe","mp4","mp3","csv","xlsx"].indexOf(ext)!==-1){
      sendEvent("download",{file:href.substring(0,200),ext:ext});
    }
  },true);
})();
