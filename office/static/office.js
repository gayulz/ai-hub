'use strict';
const $=id=>document.getElementById(id),map=$('map'),ctx=map.getContext('2d'),world=$('world'),labels=$('labels'),panel=$('panel');
// 파스텔 팔레트 (0: 외곽선은 검정 대신 부드러운 자두색)
const P=['#5b4b6e','#7d6b91','#a596bd','#cfe6da','#e3daf0','#f4effb','#fffaff','#fff8ec','#f8e1c8','#efc9a6','#d9a98a','#a77c6c','#8fd4a2','#bdeac0','#e2f7d8','#5cb488','#ffc98f','#ffe9a8','#ffa585','#ffcfae','#ff92b5','#ffcfe0','#b096e0','#d9c9f7','#80aef0','#bcdcff','#ff7f93','#ffe8d6','#ffb2b9','#5a9fb3','#94dbd6','#cdf5ea','#f1e4d0','#fbf5ea','#d8c3a6','#ffffff','#a8dcbf','#fff3c4','#e6f0ff','#fde9f1','#fbe9d6','#d8e7ff','#ece3fb','#fff8dc','#e3f1fb'];
const W=704,H=384,T=16,obstacles=[],props=[],keys=new Set(),reduced=matchMedia('(prefers-reduced-motion: reduce)');
let g=ctx,S=2,vw=720,vh=422,camera={x:0,y:0},last=0,paused=reduced.matches,near=null,pending=null,destination=null,toastTimer;
const bg=document.createElement('canvas');bg.width=W;bg.height=H;const bgctx=bg.getContext('2d');
const rect=(x,y,w,h,c)=>{g.fillStyle=P[c];g.fillRect(Math.round(x),Math.round(y),w,h)},line=(x,y,w,c)=>rect(x,y,w,1,c);
function sprite(rows,x,y,colors){rows.forEach((row,j)=>[...row].forEach((v,i)=>{if(colors[v]!==undefined)rect(x+i,y+j,1,1,colors[v])}))}
const HEART=['xx.xx','xxxxx','.xxx.','..x..'],STAR=['..x..','.xxx.','xxxxx','.xxx.','.x.x.'];
function heart(x,y,c){sprite(HEART,x,y,{x:c})}
function pixelText(text,x,y,c=0){const glyph={A:['010','101','111','101','101'],B:['110','101','110','101','110'],C:['011','100','100','100','011'],D:['110','101','101','101','110'],E:['111','100','110','100','111'],F:['111','100','110','100','100'],G:['011','100','101','101','011'],H:['101','101','111','101','101'],I:['111','010','010','010','111'],K:['101','101','110','101','101'],L:['100','100','100','100','111'],M:['10001','11011','10101','10101','10101'],N:['101','111','111','111','101'],O:['010','101','101','101','010'],P:['110','101','110','100','100'],R:['110','101','110','101','101'],S:['011','100','010','001','110'],T:['111','010','010','010','010'],U:['101','101','101','101','111'],V:['101','101','101','101','010'],W:['10101','10101','10101','11011','10001'],Y:['101','101','010','010','010'],'0':['111','101','101','101','111'],'1':['010','110','010','010','111'],'2':['110','001','010','100','111'],'3':['110','001','010','001','110'],'4':['101','101','111','001','001'],'6':['011','100','111','101','111'],'7':['111','001','010','010','010'],'/':['001','001','010','100','100'],'.':['0','0','0','0','1'],'-':['000','000','111','000','000'],'♥':['11011','11111','01110','00100','00000']};for(const ch of text){const rows=glyph[ch];if(rows){sprite(rows,x,y,{'1':c});x+=rows[0].length+1}else x+=3}}
function tiles(x,y,w,h,a,b,s){for(let j=y;j<y+h;j+=T)for(let i=x;i<x+w;i+=T){const tw=Math.min(T,x+w-i),th=Math.min(T,y+h-j),k=(Math.round((i-x)/T)+Math.round((j-y)/T))%2;rect(i,j,tw,th,k?b:a);if(s!==undefined&&tw>9&&th>9&&(i*7+j*3)%5===0){rect(i+7,j+6,1,3,s);rect(i+6,j+7,3,1,s)}}}
function planks(x,y,w,h,a=8,b=40,c=9){for(let j=y,r=0;j<y+h;j+=8,r++){const th=Math.min(8,y+h-j);rect(x,j,w,th,r%2?b:a);line(x,j,w,c);for(let i=x+(r%2?12:28);i<x+w;i+=32)rect(i,j,1,th,c)}}
function wall(x,y,w,h=8){rect(x+1,y+3,w,h,34);rect(x,y,w,h,32);rect(x,y,w,Math.min(h,4),33);line(x,y,w,35);line(x,y+h-1,w,34);obstacles.push({x,y,w,h})}
function block(x,y,w,h){obstacles.push({x,y,w,h})}
function plant(x,y,tall=false){props.push({x,y,kind:'plant',tall});block(x-7,y-10,14,12)}
function drawPlant(x,y,tall){rect(x-5,y-1,12,3,4);if(tall){sprite(['...oo...','..ollo..','.ollglo.','olglllgo','.ollllo.','olgllglo','.ollllo.','..oggo..','...oo...'],x-4,y-22,{o:15,g:12,l:13});heart(x-2,y-18,17)}else sprite(['..o..o..','.olo.lo.','.ollolo.','..olllo.','...oo...'],x-4,y-13,{o:15,l:13,g:12});rect(x-1,y-9-(tall?4:0),2,4+(tall?4:0),15);rect(x-4,y-6,9,7,0);rect(x-3,y-6,7,6,tall?24:18);line(x-3,y-6,7,tall?25:19);rect(x-4,y-7,9,2,0);rect(x-3,y-7,7,1,tall?25:19);rect(x-2,y-3,1,1,35)}
function monitor(x,y,w=18){rect(x+1,y+2,w,13,4);rect(x,y,w,12,0);rect(x+1,y+1,w-2,10,35);rect(x+2,y+2,w-4,7,30);line(x+3,y+3,w-9,31);line(x+3,y+5,w-12,35);heart(x+w-8,y+4,17);rect(x+w/2-1,y+12,3,2,2);rect(x+w/2-4,y+14,9,1,2)}
function desk(x,y,accent=13){rect(x+1,y+6,57,25,4);rect(x,y,56,24,10);rect(x+1,y,54,19,8);line(x+2,y+1,52,7);line(x+1,y+19,54,9);rect(x+3,y+24,4,5,10);rect(x+49,y+24,4,5,10);monitor(x+4,y-10,20);monitor(x+27,y-11,21);rect(x+16,y+11,22,6,2);rect(x+17,y+11,20,5,35);line(x+18,y+12,18,5);line(x+18,y+14,18,5);rect(x+40,y+12,4,5,2);rect(x+40,y+12,3,4,35);rect(x+46,y+3,7,8,0);rect(x+47,y+3,5,7,accent);line(x+47,y+3,5,35);rect(x+52,y+5,2,3,0);rect(x+49,y+6,1,1,35);rect(x+5,y+3,6,6,0);rect(x+6,y+4,4,4,17);block(x,y-7,56,34);chair(x+28,y+35,accent)}
function chair(x,y,c=22){rect(x-8,y-5,16,12,0);rect(x-7,y-4,14,10,c);rect(x-5,y-3,3,1,35);rect(x-8,y+6,16,7,0);rect(x-7,y+6,14,6,c);line(x-6,y+7,4,35);rect(x-1,y+13,2,3,2);line(x-6,y+16,12,2);rect(x-7,y+16,3,2,0);rect(x+4,y+16,3,2,0)}
function shelf(x,y,w=26){rect(x+1,y+3,w,30,4);rect(x,y,w,30,0);rect(x+1,y+1,w-2,28,10);rect(x+2,y+2,w-4,25,40);const bk=[20,24,17,12,22,30,26];for(let j=0;j<2;j++){for(let i=0;i<w-7;i+=4){const h=6+((i+j)%3);rect(x+3+i,y+3+j*12+(8-h),3,h,bk[(i/4+j*2)%7]);line(x+3+i,y+4+j*12+(8-h),1,35)}line(x+2,y+14+j*12,w-4,9)}rect(x+2,y+30,3,2,0);rect(x+w-5,y+30,3,2,0);sprite(['.oo.','oggo','.oo.'],x+w-8,y-3,{o:15,g:13});rect(x+w-8,y,4,3,18);block(x,y+8,w,24)}
function windowArt(x,y,w=40){rect(x-2,y-1,w+4,17,34);rect(x,y,w,14,35);rect(x+2,y+1,w-4,11,25);rect(x+4,y+4,6,2,35);rect(x+5,y+3,3,1,35);rect(x+w-12,y+7,7,2,35);rect(x+w-10,y+6,3,1,35);rect(x+w/2,y,2,14,35);rect(x-3,y+14,w+6,3,33);line(x-3,y+16,w+6,34)}
function rack(x,y,index){rect(x+2,y+4,30,45,4);rect(x,y,29,43,0);rect(x+1,y+1,27,41,23);line(x+2,y+2,25,35);for(let i=0;i<4;i++){rect(x+4,y+6+i*8,21,6,0);rect(x+5,y+7+i*8,19,4,42);line(x+6,y+8+i*8,9,2);rect(x+19,y+8+i*8,3,2,[12,30,17,12][i])}rect(x+3,y+43,4,3,0);rect(x+22,y+43,4,3,0);block(x,y+5,29,42)}
function sofa(x,y,w=78){rect(x+2,y+5,w,29,4);rect(x,y,w,27,0);rect(x+1,y+1,w-2,9,30);line(x+3,y+2,w-6,31);rect(x+5,y+10,w-10,15,31);line(x+6,y+11,w-12,35);rect(x+5+w/3,y+10,1,15,30);rect(x+5+w*2/3,y+10,1,15,30);rect(x+1,y+7,6,19,30);rect(x+w-7,y+7,6,19,30);line(x+2,y+8,4,31);line(x+w-6,y+8,4,31);rect(x+3,y+27,4,3,0);rect(x+w-7,y+27,4,3,0);sprite(['.k...k......','kyk.kyk.....','kyyyyyykkkk.','kykyykyyyyyk','kwwyyyyyyyyk','.kkkkkkkkkk.'],x+7,y+5,{k:0,y:16,w:7});rect(x+w-20,y+12,6,5,17);line(x+w-20,y+12,6,35);block(x,y,w,18)}
function flowers(){for(let y=6;y<H;y+=14)for(let x=6;x<W;x+=14){const r=(x*13+y*7)%17;if(r===0||r===9){const c=r?25:17;rect(x,y-1,1,1,c);rect(x-1,y,3,1,c);rect(x,y+1,1,1,c);rect(x,y,1,1,r?17:35)}else if(r===4||r===12){rect(x,y,1,2,36);rect(x+2,y-1,1,3,36)}}}
function bunting(x,w){line(x,60,w,2);const c=[18,17,30,23,24];for(let i=x+3,k=0;i<x+w-5;i+=10,k++){rect(i,61,5,1,c[k%5]);rect(i+1,62,3,1,c[k%5]);rect(i+2,63,1,1,c[k%5])}}
function buildOffice(){g=bgctx;rect(0,0,W,H,3);flowers();rect(22,34,660,340,0);rect(24,36,656,336,34);
	tiles(36,58,632,302,6,5);tiles(40,62,200,114,14,31,35);tiles(248,62,112,114,38,41,35);planks(368,62,112,114);tiles(488,62,176,114,5,42,35);tiles(40,216,180,108,37,43,35);tiles(228,216,166,108,6,42);planks(402,216,262,108);planks(36,332,632,28,40,8,9);
	wall(28,40,648,18);wall(28,58,8,310);wall(668,58,8,310);wall(28,360,302);wall(374,360,302);[240,360,480].forEach(x=>wall(x,58,8,118));wall(36,176,174);wall(238,176,74);wall(340,176,108);wall(476,176,24);wall(528,176,140);wall(36,208,150);wall(214,208,88);wall(330,208,90);wall(452,208,216);wall(220,216,8,108);wall(394,216,8,108);wall(36,324,140);wall(204,324,98);wall(330,324,100);wall(458,324,210);
	bunting(38,200);bunting(250,108);bunting(370,108);bunting(490,176);bunting(42,176);bunting(230,162);bunting(404,258);
	windowArt(58,42,40);windowArt(192,42,30);windowArt(272,42,32);windowArt(444,42,25);windowArt(518,42,40);windowArt(610,42,34);
	desk(80,105,13);desk(164,105,21);desk(276,105,25);desk(396,105,19);shelf(43,104,24);plant(59,167);plant(228,88,true);plant(347,89);plant(380,166);shelf(448,82,24);
	[506,557,608].forEach((x,i)=>rack(x,108,i));plant(650,86);rect(514,157,127,11,0);rect(515,158,125,9,22);pixelText('COLLECT ♥ SYNC ♥ BACKUP',518,160,35);
	desk(68,253,17);shelf(189,232,22);plant(53,307);rect(158,262,30,26,0);rect(159,263,28,24,10);rect(161,265,24,20,17);pixelText('DND',165,270,20);heart(170,278,18);block(158,273,30,15);
	rect(243,234,134,81,24);rect(245,236,130,77,25);rect(247,238,126,73,44);for(let y=242;y<310;y+=14)for(let x=251;x<370;x+=16)if((x/16+y/14|0)%2)rect(x+1,y+1,3,3,25);[[272,251],[309,251],[346,251],[272,295],[309,295],[346,295]].forEach((p,i)=>chair(...p,[18,24,30,22,17,12][i]));rect(254,250,112,42,0);rect(255,251,110,40,10);rect(256,249,108,36,8);line(258,250,104,7);rect(302,257,15,10,0);rect(303,258,13,7,30);heart(307,260,35);rect(299,269,22,2,2);[[270,263],[340,272]].forEach(p=>{rect(p[0],p[1],10,7,35);line(p[0]+2,p[1]+2,6,25);line(p[0]+2,p[1]+4,4,25)});rect(286,262,5,5,18);rect(330,260,5,5,24);block(255,251,110,40);rect(278,220,62,13,0);rect(279,221,60,11,35);rect(280,222,58,9,25);pixelText('PRIVATE',295,224,0);
	rect(482,296,150,24,0);rect(483,297,148,22,23);rect(486,300,142,16,42);for(let x=492;x<624;x+=14)heart(x,305,23);
	sofa(496,279,78);rect(584,288,34,22,0);rect(585,287,32,18,8);line(586,288,30,7);rect(594,290,8,7,0);rect(595,290,6,6,35);rect(596,291,4,2,11);rect(589,298,22,3,24);rect(608,293,3,4,17);block(584,286,34,24);rect(626,268,30,33,0);rect(627,268,28,32,10);rect(628,268,26,23,8);rect(633,253,15,20,0);rect(634,254,13,10,24);rect(636,256,9,6,25);rect(637,266,7,5,2);rect(637,271,7,4,35);heart(636,257,35);block(626,266,30,35);plant(651,319,true);plant(418,310,true);rect(640,225,14,32,0);rect(641,228,12,28,35);rect(641,217,12,14,25);line(642,218,9,35);rect(643,239,3,2,24);rect(648,239,3,2,26);block(640,230,14,27);
	rect(511,219,114,45,0);rect(512,220,112,43,30);rect(515,222,106,36,7);line(515,222,106,35);rect(519,253,97,3,4);rect(519,253,34,3,15);rect(595,254,9,2,30);rect(607,254,8,2,24);rect(511,262,114,2,10);plant(489,251);
	rect(259,331,56,23,0);rect(260,333,54,20,10);rect(261,331,52,15,8);line(262,332,50,7);rect(264,334,12,10,9);for(let i=0;i<3;i++)rect(266+i*3,333-i,5,9,[35,21,31][i]);rect(285,333,19,10,35);line(288,335,12,25);line(288,338,8,25);rect(309,330,3,14,15);heart(307,326,18);block(260,332,54,20);rect(329,338,46,20,0);rect(330,339,44,18,15);rect(332,341,40,14,12);pixelText('WELCOME',337,346,35);plant(52,351);plant(648,351);rect(342,361,20,7,8);line(338,368,28,10);
	for(let x=50;x<660;x+=24)heart(x,192,42);g=ctx;
}
const actors=[
	{id:'codex',name:'Codex',x:108,y:150,hair:11,shirt:12,light:13,acc:'phones'},
	{id:'agy',name:'AGY',x:332,y:163,hair:1,shirt:24,light:25,acc:'glasses'},
	{id:'claude',name:'Claude',x:424,y:150,hair:18,shirt:16,light:19,acc:'star'},
	{id:'gemini',name:'Gemini CLI',x:540,y:304,hair:22,shirt:30,light:31,acc:'tails'},
	{id:'gemma',name:'로컬 모델',x:96,y:297,hair:10,shirt:17,light:7,acc:'beanie'},
	{id:'blog',name:'블로그 에이전트',x:192,y:150,hair:9,shirt:20,light:21,acc:'bow'}
].map(a=>({...a,state:'확인 불가',task:'자료 조회 중',result:'조회 전',calls:null,bubble:true}));
const player={id:'me',name:'나',x:350,y:345,hair:11,shirt:31,light:35,acc:'ahoge',state:'탐색 중',face:'down',path:[]};
const rooms=[{name:'개발실',x:140,y:65,area:[36,58,204,118]},{name:'검토실',x:304,y:65,area:[248,58,112,118]},{name:'비서실',x:424,y:65,area:[368,58,112,118]},{name:'기계실',x:575,y:65,area:[488,58,180,118]},{name:'집중 업무 존',x:129,y:215,area:[36,216,184,108]},{name:'회의실',x:262,y:215,area:[228,216,166,108]},{name:'라운지',x:463,y:215,area:[402,216,266,108]}];
const zone=a=>rooms.find(r=>a.x>r.area[0]&&a.x<r.area[0]+r.area[2]&&a.y>r.area[1]&&a.y<r.area[1]+r.area[3])?.name||(a.y>325?'입구 · 결재 데스크':'공용 복도');
function tag(text,cls,x,y){const el=document.createElement('div');el.className='at '+cls;el.innerHTML=text;labels.append(el);el.wx=x;el.wy=y;return el}
rooms.forEach(r=>r.label=tag(r.name+(r.name==='집중 업무 존'?'<small>방해 금지</small>':''),'room-name',r.x,r.y));
actors.forEach(a=>{a.face='down';a.path=[];a.nameEl=document.createElement('button');a.nameEl.className='at name';a.nameEl.innerHTML='<span class="dot"></span>'+a.name;labels.append(a.nameEl);a.nameEl.addEventListener('click',()=>approach(a));a.bubbleEl=tag('','bubble',a.x,a.y);const b=document.createElement('button');b.className='person';b.innerHTML='<span class="dot"></span>'+a.name;b.addEventListener('click',()=>approach(a));$('people').append(b);a.roster=b});
player.nameEl=tag('나','name me',player.x,player.y);
const rackWarnings=[0,1,2].map(i=>{const el=tag('확인 필요','bubble warn',520+i*51,88);el.hidden=true;el.style.pointerEvents='auto';el.style.cursor='pointer';el.addEventListener('click',()=>openPanel('jobs'));return el});
function place(text,x,y,fn){const b=document.createElement('button');b.className='at place';b.innerHTML=text;labels.append(b);b.wx=x;b.wy=y;b.addEventListener('click',fn);return b}
const approval={id:'approval',name:'결재함',x:286,y:355},meeting={id:'meeting',name:'회의 소집',x:316,y:196},board={id:'board',name:'현황판',x:562,y:276};
const approvalButton=place('결재함 <b class="badge">…</b>',286,320,()=>approach(approval));place('회의 소집',316,199,()=>interact(meeting));
const boardButton=place('<b>C4 현황판</b><br>자료 조회 중',515,222,()=>interact(board));boardButton.className='board-hit';
const at=(el,x,y)=>{el.style.left=Math.round((x-camera.x)*S)+'px';el.style.top=Math.round((y-camera.y)*S)+'px'};
function resize(){const b=world.getBoundingClientRect();S=2;vw=Math.ceil(b.width/S);vh=Math.ceil(b.height/S);map.width=vw;map.height=vh;map.style.width=vw*S+'px';map.style.height=vh*S+'px';ctx.imageSmoothingEnabled=false;boardButton.style.width=106*S+'px';boardButton.style.height=36*S+'px';updateCamera()}
function updateCamera(){camera.x=vw>=W?(W-vw)/2:Math.max(0,Math.min(W-vw,player.x-vw/2));camera.y=vh>=H?(H-vh)/2:Math.max(0,Math.min(H-vh,player.y-vh/2))}
function walkable(x,y){return x>42&&x<662&&y>66&&y<355&&!obstacles.some(o=>x>o.x-4&&x<o.x+o.w+4&&y>o.y-3&&y<o.y+o.h+3)}
function pathTo(a,x,y){const cols=W/8,rows=H/8,start=Math.floor(a.y/8)*cols+Math.floor(a.x/8),valid=n=>n>=0&&n<cols*rows&&walkable(n%cols*8+4,Math.floor(n/cols)*8+4);let target=-1,best=Infinity;for(let j=-6;j<=6;j++)for(let i=-6;i<=6;i++){const cx=Math.floor(x/8)+i,cy=Math.floor(y/8)+j,n=cy*cols+cx,d=Math.hypot(cx*8+4-x,cy*8+4-y);if(cx>=0&&cx<cols&&cy>=0&&cy<rows&&valid(n)&&d<best){best=d;target=n}}if(target<0)return [];
	const parents=new Int32Array(cols*rows).fill(-1),queue=[start];parents[start]=start;for(let i=0;i<queue.length;i++){let n=queue[i];if(n===target)break;for(const nn of [n-cols,n+cols,...(n%cols>0?[n-1]:[]),...(n%cols<cols-1?[n+1]:[])])if(valid(nn)&&parents[nn]===-1){parents[nn]=n;queue.push(nn)}}if(parents[target]===-1)return [];let path=[];for(let n=target;n!==start;n=parents[n])path.push({x:n%cols*8+4,y:Math.floor(n/cols)*8+4});return path.reverse()}
function moveTo(a,x,y,onArrival){a.path=pathTo(a,x,y);a.onArrival=onArrival;if(!a.path.length&&Math.hypot(a.x-x,a.y-y)<12)onArrival?.()}
function approach(a){if(Math.hypot(a.x-player.x,a.y-player.y)<42){interact(a);return}pending=a;destination={x:a.x,y:a.y};moveTo(player,a.x,a.y,()=>{const p=pending;pending=null;destination=null;if(p){if(Math.hypot(p.x-player.x,p.y-player.y)<48)interact(p);else say('직원이 이동했어요. 가까이 다가가 다시 말을 걸어 주세요.')}});if(!player.path.length){pending=null;destination=null;say('그 자리까지 갈 수 없어요. 가까운 바닥을 클릭해 주세요.')}}
function step(a,dt,speed){if(!a.path.length)return;a.walking=true;const p=a.path[0],dx=p.x-a.x,dy=p.y-a.y,d=Math.hypot(dx,dy),s=speed*dt;a.face=Math.abs(dx)>Math.abs(dy)?dx>0?'right':'left':dy>0?'down':'up';if(d<=s){a.x=p.x;a.y=p.y;a.path.shift();if(!a.path.length){a.walking=false;const f=a.onArrival;a.onArrival=null;f?.()}}else{a.x+=dx/d*s;a.y+=dy/d*s}}
// 2등신 치비 캐릭터: 큰 머리, 반짝이는 눈, 볼터치
const BODY=['....oooooooo....','..oohhhhhhhhoo..','.ohhHHhhhhhhhho.','.ohHhhhhhhhhhho.','ohhhhhhhhhhhhhho','ohhsshhhhhhsshho','ohssssssssssssho','ohssEessssEessho','ohsseessssseesho','ohsrrssmmssrrsho','.ohssssssssssho.','..oossssssssoo..','...occcccccco...','..osccllccccso..','..osccccccccso..','...occccccco....','...occccccco....'];
function drawActor(a,t,portrait=false){const walking=a.walking&&!paused,frame=walking?Math.floor(t/130)%4:0,bob=walking&&(frame%2)?1:0,breathe=!walking&&!paused&&!portrait&&Math.floor((t+a.x*23)/650)%2?1:0,cx=Math.round(a.x),x=cx-8,y=Math.round(a.y)-22-bob+breathe,back=a.face==='up',side=a.face==='left'||a.face==='right';
	rect(cx-6,Math.round(a.y)-2,12,2,4);rect(cx-4,Math.round(a.y)-1,8,1,4);
	if(a.acc==='tails'){rect(x-2,y+5,4,7,0);rect(x-1,y+6,2,5,a.hair);rect(x+14,y+5,4,7,0);rect(x+15,y+6,2,5,a.hair)}
	const hide=back?a.hair:null;sprite(BODY,x,y,{o:0,h:a.hair,H:35,s:hide??27,e:hide??0,E:hide??35,r:hide??28,m:hide??26,c:a.shirt,l:a.light});
	if(back){line(x+3,y+12,10,a.hair)}else if(side){const off=a.face==='left'?10:4;rect(x+off,y+7,2,2,27);rect(x+(a.face==='left'?11:3),y+9,2,1,27)}
	const ly=y+17,l1=walking&&frame===1?-1:0,l2=walking&&frame===3?-1:0;rect(x+4,ly+l1,4,2,0);rect(x+5,ly+l1,2,1,2);rect(x+9,ly+l2,4,2,0);rect(x+10,ly+l2,2,1,2);
	if(a.acc==='phones'){line(x+3,y,10,24);rect(x+1,y+1,2,4,24);rect(x+13,y+1,2,4,24);rect(x,y+5,3,5,0);rect(x+1,y+6,2,3,24);rect(x+13,y+5,3,5,0);rect(x+13,y+6,2,3,24)}
	if(a.acc==='glasses'&&!back){rect(x+3,y+6,4,1,0);rect(x+9,y+6,4,1,0);rect(x+3,y+7,1,2,0);rect(x+12,y+7,1,2,0);rect(x+7,y+7,2,1,0)}
	if(a.acc==='star')sprite(STAR,x+11,y-2,{x:16});
	if(a.acc==='tails')sprite(['.x.','xxx','.x.'],x+2,y+1,{x:17});
	if(a.acc==='beanie'){rect(x+2,y-2,12,6,0);rect(x+3,y-1,10,4,31);line(x+3,y+2,10,30);rect(x+6,y-1,1,3,30);rect(x+9,y-1,1,3,30);rect(x+7,y-4,2,2,35)}
	if(a.acc==='bow')sprite(['xx.xx','xxkxx','xx.xx'],x+10,y-1,{x:20,k:26});
	if(a.acc==='ahoge'){rect(x+8,y-2,1,2,0);rect(x+9,y-3,2,1,0);rect(x+8,y-1,1,1,a.hair)}
	if(!walking&&!portrait&&['구현 중','검토 중','집중 중','준비 중'].includes(a.state)){const f=paused?0:Math.floor(t/300)%2;rect(x+1,y+13+f,3,2,27);rect(x+12,y+14-f,3,2,27)}
	if(a.id==='me'&&!portrait){const f=paused?0:Math.floor(t/400)%2;heart(cx-2,y-9-f,18)}}
function updateLabels(){rooms.forEach(r=>at(r.label,r.x,r.y));[...labels.querySelectorAll('.place'),...rackWarnings,boardButton].forEach(el=>at(el,el.wx,el.wy));for(const a of actors){at(a.nameEl,a.x,a.y+5);a.nameEl.dataset.state=a.state;a.nameEl.title=a.name+' · '+zone(a)+' · '+a.state;a.nameEl.setAttribute('aria-label',a.name+' 찾아가기: '+a.state);a.roster.dataset.state=a.state;a.roster.title=a.state+' · '+zone(a);a.roster.setAttribute('aria-label',a.name+' 찾아가기: '+a.state);a.bubbleEl.hidden=!a.bubble||near===a;a.bubbleEl.className='at bubble '+(a.id==='gemma'?'dnd':a.id==='codex'?'work':'');a.bubbleEl.textContent=a.state==='상태 미확인'?'? 상태 미확인':a.task;at(a.bubbleEl,a.x,a.y-(a.id==='codex'?61:a.id==='blog'?45:a.id==='gemma'?64:49))}at(player.nameEl,player.x,player.y+6);$('location').textContent=zone(player);world.dataset.zone=zone(player);if(near&&!panel.open){$('proximity').hidden=false;$('proximity').innerHTML='<kbd>Space</kbd> '+(near.id==='board'?'현황판 보기':near.id==='meeting'?'회의 소집':near.id==='approval'?'결재함 보기':'말 걸기');at($('proximity'),near.x,near.y-46)}else $('proximity').hidden=true}
function setEvent(title,detail){$('event-title').textContent=title;$('event-detail').textContent=detail;$('live').textContent=title+' · '+detail}
function draw(t){g=ctx;rect(0,0,vw,vh,3);ctx.save();ctx.translate(-Math.round(camera.x),-Math.round(camera.y));ctx.drawImage(bg,0,0);if(destination){const x=Math.round(destination.x),y=Math.round(destination.y);heart(x-2,y-2,15)}
	drawRackStates();
	const things=[...props,...actors,player].sort((a,b)=>a.y-b.y);things.forEach(a=>a.kind==='plant'?drawPlant(a.x,a.y,a.tall):drawActor(a,t));
	if(!paused){const blink=Math.floor(t/700)%2;if(blink)drawRackStates();const z=Math.floor(t/500)%3;for(let i=0;i<=z;i++){const zx=516+i*4,zy=278-i*5;line(zx,zy,3,2);rect(zx+1,zy+1,1,1,2);line(zx,zy+2,3,2)}const sp=Math.floor(t/400)%4;rect(301+sp*6,248,1,1,35)}
	ctx.restore();updateLabels()}
function tick(t){
	const dt=Math.min(.04,(t-last)/1000||0);last=t;
	if(!panel.open){
		let dx=(keys.has('arrowright')||keys.has('d')?1:0)-(keys.has('arrowleft')||keys.has('a')?1:0),dy=(keys.has('arrowdown')||keys.has('s')?1:0)-(keys.has('arrowup')||keys.has('w')?1:0);player.walking=false;
		if(dx||dy){player.path=[];pending=null;destination=null;const length=Math.hypot(dx,dy),mx=dx/length*70*dt,my=dy/length*70*dt;player.face=Math.abs(dx)>Math.abs(dy)?dx>0?'right':'left':dy>0?'down':'up';if(walkable(player.x+mx,player.y)){player.x+=mx;player.walking=true}if(walkable(player.x,player.y+my)){player.y+=my;player.walking=true}}
		else step(player,dt,70);
	}
	if(!paused)actors.forEach(a=>{a.walking=false;step(a,dt,38)});
	near=[...actors,approval,meeting,board].filter(a=>Math.hypot(a.x-player.x,a.y-player.y)<(a.id==='board'?52:40)).sort((a,b)=>Math.hypot(a.x-player.x,a.y-player.y)-Math.hypot(b.x-player.x,b.y-player.y))[0]||null;
	updateCamera();draw(t);requestAnimationFrame(tick);
}
function say(text){clearTimeout(toastTimer);$('toast').textContent=text;$('toast').hidden=false;toastTimer=setTimeout(()=>$('toast').hidden=true,4400)}
const h=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const localTime=value=>value?new Date(value).toLocaleString('ko-KR',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}):'확인 불가';
const notice=group=>group?.status==='확인 불가'?'<p class="notice">확인 불가 · '+h(group.reason)+'</p>':'';
function emptyState(reason){
	const group=()=>({status:'확인 불가',reason,items:[]});
	return {agents:actors.map(a=>({id:a.id,status:'확인 불가',task:reason,result:'확인 불가',reason,calls:null})),jobs:[],activity:[],approvals:{c4:group(),runs:group(),decisions:group()},board:{progress:group(),usage:{c4:group(),limit:group(),plans:group()},schedule:{decisions:group(),jobs:[]}}};
}
let stateSnapshot=emptyState('자료 조회 중'),panelView=null,refreshing=false;
const seats={codex:[108,150],agy:[148,150],claude:[116,163],gemini:[76,150],gemma:[216,150],blog:[192,150]};
const lounge=[[466,300],[500,308],[520,308],[540,304],[565,308],[625,313]];
function targetFor(a){
	const i=actors.indexOf(a);
	if(a.state==='검토 중')return [[304,150],[332,163],[276,160],[316,160],[260,150],[344,150]][i];
	if(a.state==='집중 중')return [96+i*7,297];
	if(['상태 미확인','확인 불가'].includes(a.state))return [404+i*8,150];
	if(['쉬는 중','꺼짐'].includes(a.state))return lounge[i];
	const [x,y]=seats[a.id];return [x,y+(a.state==='대기 중'?8:0)];
}
function drawRackStates(){
	for(let i=0;i<3;i++){
		const job=stateSnapshot.jobs[i],x=506+i*51,c=job?.status==='확인 필요'?26:(!job||job.status==='확인 불가')?2:15;
		rect(x,108,29,2,c);rect(x,110,2,41,c);rect(x+27,110,2,41,c);
		for(let j=0;j<4;j++)rect(x+19,116+j*8,3,2,c);
	}
}
function applyState(data){
	stateSnapshot=data;
	for(const value of data.agents){
		const a=actors.find(a=>a.id===value.id);if(!a)continue;
		const before=a.state;a.state=value.status;a.task=value.task||value.reason||value.status;a.result=value.result||'확인 불가';a.calls=value.calls;a.info=value;a.bubble=true;
		if(before!==a.state){const [x,y]=targetFor(a);if(paused){a.x=x;a.y=y;a.path=[];a.walking=false}else moveTo(a,x,y)}
	}
	document.querySelector('.online-count').textContent=data.agents.filter(a=>['구현 중','검토 중','일하는 중','집중 중'].includes(a.status)).length;
	const groups=Object.values(data.approvals),count=groups.reduce((n,group)=>n+group.items.length,0);
	approvalButton.innerHTML='결재함 <b class="badge">'+count+(groups.some(group=>group.status==='확인 불가')?'?':'')+'</b>';
	const progress=data.board.progress;
	boardButton.innerHTML='<span class="sr-only">라운지 현황판 확대</span><b>C4 현황판</b><br>'+h(progress.items.slice(0,3).map(row=>row.project+' '+row.applied+'/'+row.total).join(' · ')||(progress.status==='확인 불가'?'확인 불가':'대상 없음'))+'<br><small>클릭해서 확대</small>';
	rackWarnings.forEach((el,i)=>{const job=data.jobs[i];el.hidden=job?.status!=='확인 필요';el.textContent=(job?.name||'')+' 확인 필요'});
	const latest=data.activity[0];setEvent(latest?latest.text:'최근 활동 없음',latest?localTime(latest.at):'읽을 수 있는 사건·로그 없음');
	$('sync-state').textContent=data.updated?'갱신 '+localTime(data.updated):'자료 갱신 실패';
	if(panel.open)renderCurrent();
}
async function loadState(){
	if(refreshing)return;refreshing=true;
	const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),12000);
	try{
		const response=await fetch('/api/state',{cache:'no-store',signal:controller.signal});
		if(!response.ok)throw new Error('상태 조회 실패');
		applyState(await response.json());
	}catch{applyState(emptyState('서버 자료를 갱신할 수 없습니다'))}
	finally{clearTimeout(timer);refreshing=false}
}
function showPanel(title,html){
	const opened=panel.open,scroll=panel.scrollTop,reply=document.activeElement?.dataset?.reply;
	keys.clear();player.path=[];pending=null;destination=null;$('panel-title').textContent=title;$('panel-body').innerHTML=html;
	if(!opened){panel.showModal();$('close').focus()}else{panel.scrollTop=scroll;if(reply)[...panel.querySelectorAll('[data-reply]')].find(b=>b.dataset.reply===reply)?.focus({preventScroll:true})}
}
function interact(a){
	if(a.id==='meeting'){say('회의 소집은 3단계에서 열립니다.');return}
	if(['approval','board'].includes(a.id)){openPanel(a.id);return}
	panelView={type:'agent',id:a.id};renderCurrent();
}
function renderAgent(a){
	const info=a.info||{},link=info.dashboard?.url;
	showPanel(a.name+' · '+zone(a),'<div class="employee"><canvas id="portrait" class="portrait" width="32" height="32" aria-hidden="true"></canvas><div><h3>'+h(a.name)+' <small>'+h(a.state)+'</small></h3><dl><dt>지금 하는 일</dt><dd>'+h(a.task)+'</dd><dt>최근 결과</dt><dd>'+h(a.result)+'</dd><dt>오늘 호출 수</dt><dd>'+(a.calls===null?'확인 불가':h(a.calls)+'회')+'</dd>'+(info.last_seen?'<dt>최근 활동</dt><dd>'+h(localTime(info.last_seen))+'</dd>':'')+(info.reason?'<dt>확인 근거</dt><dd>'+h(info.reason)+'</dd>':'')+'</dl>'+(link?'<p><a href="'+h(link)+'" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">블로그 대시보드 열기</a></p>':'')+notice(info.dashboard)+'</div></div><div class="panel-foot"><span>상태 자료만 읽습니다.</span><div><button class="assign" disabled aria-describedby="stage-two">일 맡기기</button><span class="stage-label" id="stage-two">2단계에서 열림</span></div></div>');
	const pc=$('portrait').getContext('2d');g=pc;pc.imageSmoothingEnabled=false;tiles(0,0,32,32,31,14);for(let i=0;i<32;i+=8)heart(i+1,1,35);drawActor({...a,x:16,y:30,face:'down',walking:false},0,true);g=ctx;
}
function replyCard(title,detail,reply){
	return '<div class="reply"><b>'+h(title)+'</b><p>'+h(detail)+'</p><code>'+h(reply)+'</code><button class="mini-button" data-reply="'+h(reply)+'">회신 문구 복사</button></div>';
}
function approvalPanel(){
	const {c4,runs,decisions}=stateSnapshot.approvals;
	const count=group=>group.items.length+'건'+(group.status==='확인 불가'?' · 일부 확인 불가':'');
	showPanel('입구 · 결재함','<div class="approval"><div class="approval-summary"><div>C4 승인 대기<strong>'+count(c4)+'</strong></div><div>실행 반영 대기<strong>'+count(runs)+'</strong></div><div>결정 대기<strong>'+count(decisions)+'</strong></div></div><h3>C4 승인 대기 묶음</h3>'+notice(c4)+c4.items.map(item=>replyCard(item.project+' · '+item.kind+' · '+item.id,'준비 '+item.prepared+'건 · 보류 '+item.held+'건',item.reply)).join('')+'<h3>실행 반영 대기</h3>'+notice(runs)+runs.items.map(item=>replyCard(item.id,item.status,item.reply)).join('')+'<h3>답변을 기다리는 결정</h3>'+notice(decisions)+decisions.items.map(item=>replyCard(item.title,item.due?'기한 '+item.due:'기한 없음',item.reply)).join('')+'<div class="panel-foot"><span>회신 문구를 복사해 사용하세요.</span><div><button class="assign" disabled>결재하기</button><span class="stage-label">2단계에서 열림</span></div></div></div>');
}
function boardPanel(){
	const {progress,usage,schedule}=stateSnapshot.board;
	const rows=progress.items.map(row=>{
		const ratio=row.total?Math.max(0,Math.min(100,row.applied/row.total*100)):0;
		return '<div class="progress-row"><div><span><b>'+h(row.project)+'</b> <small>'+h(row.kind)+'</small></span><b>'+h(row.applied)+' / '+h(row.total)+'</b></div><div class="track" role="progressbar" aria-label="'+h(row.project+' '+row.kind)+'" aria-valuemin="0" aria-valuemax="'+h(row.total)+'" aria-valuenow="'+h(row.applied)+'"><i style="width:'+ratio+'%"></i></div></div>';
	}).join('');
	const dates=schedule.decisions.items.map(item=>'<li><time>'+h(item.due)+'</time>'+h(item.title)+'</li>').join('');
	const jobs=schedule.jobs.map(item=>'<li><time>'+h(item.next_run?localTime(item.next_run):'확인 불가')+'</time>'+h(item.title)+notice(item)+'</li>').join('');
	showPanel('라운지 현황판','<div class="columns"><section><h3>C4 프로젝트별 진행률</h3>'+notice(progress)+(rows||'<p>집계된 대상 없음</p>')+'</section><section><h3>다가오는 일정</h3>'+notice(schedule.decisions)+'<ul class="schedule">'+dates+'</ul><h3 style="margin-top:14px">오늘 남은 예약 작업</h3><ul class="schedule">'+(jobs||'<li>남은 예약 없음</li>')+'</ul><div class="usage">오늘 C4 구독 <b>'+(usage.c4.subscription??'?')+' / '+(usage.limit.daily_limit_calls??'?')+'회</b><br>로컬 <b>'+(usage.c4.local??'?')+'회</b>'+notice(usage.c4)+notice(usage.limit)+'<p>요금제별 호출 수</p>'+notice(usage.plans)+usage.plans.items.map(item=>'<p>'+h(item.plan)+' · '+h(item.count)+'회</p>').join('')+'</div></section></div><div class="panel-foot">'+h(localTime(stateSnapshot.updated))+' 기준 · 10초마다 갱신</div>');
}
function jobsPanel(){
	showPanel('기계실 · 예약 작업',stateSnapshot.jobs.map(item=>'<section class="job"><h3>'+h(item.name)+' · '+h(item.status)+'</h3>'+notice(item)+'<p>launchd 상태: '+h(item.state||'확인 불가')+' · 마지막 종료 코드: '+h(item.last_exit_code??'기록 없음')+'</p><p>다음 실행: '+h(localTime(item.schedule.next_run))+'</p>'+notice(item.schedule)+'<p>마지막 로그: '+h(item.log.last_line)+' · '+h(localTime(item.log.at))+(item.log.time_source==='mtime'?' (파일 수정 시각)':'')+'</p>'+notice(item.log)+'</section>').join('')||'<p class="notice">예약 작업 자료 확인 불가</p>');
}
function openPanel(type){panelView={type};renderCurrent()}
function renderCurrent(){
	if(panelView?.type==='agent'){renderAgent(actors.find(a=>a.id===panelView.id));return}
	if(panelView?.type==='board')boardPanel();
	else if(panelView?.type==='approval')approvalPanel();
	else if(panelView?.type==='jobs')jobsPanel();
	else if(panelView?.type==='activity')showPanel('최근 활동 기록','<ol class="activity-list">'+stateSnapshot.activity.map(item=>'<li><time>'+h(localTime(item.at))+'</time><p>'+h(item.text)+'</p></li>').join('')+'</ol>'+(stateSnapshot.activity.length?'':'<p>읽을 수 있는 활동 기록 없음</p>'));
}
$('panel-body').addEventListener('click',async e=>{
	const button=e.target.closest('[data-reply]');if(!button)return;
	const reply=button.dataset.reply;
	try{await navigator.clipboard.writeText(reply);say('회신 문구를 복사했습니다.')}
	catch{
		const field=document.createElement('textarea');field.value=reply;field.setAttribute('aria-label','복사할 회신 문구');button.after(field);field.focus();field.select();
		try{if(document.execCommand('copy')){field.remove();button.focus();say('회신 문구를 복사했습니다.')}else say('문구를 선택했습니다. 복사 단축키를 사용하세요.')}catch{say('문구를 선택했습니다. 복사 단축키를 사용하세요.')}
	}
});
map.addEventListener('pointerdown',e=>{if(panel.open)return;const b=map.getBoundingClientRect(),x=(e.clientX-b.left)/S+camera.x,y=(e.clientY-b.top)/S+camera.y;map.focus({preventScroll:true});const a=actors.find(a=>Math.abs(a.x-x)<12&&y>a.y-27&&y<a.y+6);if(a){approach(a);return}if(x>512&&x<626&&y>219&&y<264){interact(board);return}if(x>258&&x<316&&y>328&&y<355){approach(approval);return}pending=null;destination={x,y};moveTo(player,x,y,()=>destination=null);if(!player.path.length){destination=null;say('벽이나 가구를 피해 가까운 바닥을 눌러 주세요.')}});
addEventListener('keydown',e=>{const key=e.key.toLowerCase();if(panel.open)return;if(e.target.closest('button,summary,input,textarea,select'))return;if(['arrowup','arrowdown','arrowleft','arrowright','w','a','s','d'].includes(key)){e.preventDefault();keys.add(key)}if(e.code==='Space'){e.preventDefault();if(!e.repeat){if(near)interact(near);else say('직원이나 결재 데스크 가까이 가서 Space를 눌러 주세요.')}}});addEventListener('keyup',e=>keys.delete(e.key.toLowerCase()));addEventListener('blur',()=>keys.clear());document.addEventListener('visibilitychange',()=>{keys.clear();last=performance.now()});
$('close').addEventListener('click',()=>panel.close());panel.addEventListener('close',()=>{keys.clear();map.focus({preventScroll:true})});panel.addEventListener('click',e=>{if(e.target===panel){const r=panel.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)panel.close()}});
reduced.addEventListener('change',e=>{paused=e.matches;if(paused)actors.forEach(a=>{const target=targetFor(a);a.x=target[0];a.y=target[1];a.path=[];a.walking=false})});
$('activity').addEventListener('click',()=>openPanel('activity'));
$('machine').addEventListener('click',()=>openPanel('jobs'));
function clock(){const d=new Date();$('clock').dateTime=d.toISOString();$('clock').textContent=d.toLocaleTimeString('ko-KR',{hour:'2-digit',minute:'2-digit',hour12:false});$('date').textContent=d.toLocaleDateString('ko-KR',{month:'2-digit',day:'2-digit',weekday:'long'})}clock();setInterval(clock,1000);
buildOffice();const lg=$('logo').getContext('2d');g=lg;rect(0,0,16,16,31);sprite(['.oo.oo.','oxxoxxo','oxxxxxo','.oxxxo.','..oxo..','...o...'],4,4,{o:0,x:18});rect(6,5,1,1,35);g=ctx;
console.assert(actors.length===6&&actors.every(a=>walkable(a.x,a.y)),'직원 시작 위치는 이동 가능');console.assert(pathTo(player,304,150).length>0&&pathTo(player,96,297).length>0,'입구에서 업무 공간까지 경로가 연결됨');
addEventListener('resize',resize);resize();requestAnimationFrame(tick);loadState();setInterval(loadState,10000);
