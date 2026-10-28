import * as THREE from '/vendor/three-0.160.0.module.min.js';
import { MAP, rooms, staff, engines, stream, cubes, labels, actorBlocks, actorMeshes, mailboxAt, stateText, roomById, teamRole, box, animal, build } from '/diorama-world.js';

const $=selector=>document.querySelector(selector);
let activeStaff=null,mailCount=0;
const person=id=>staff.find(p=>p.id===id);
const roleIds={rabbit:0,deer:3,beaver:4,raccoon:5,sparrow:22,squirrel:10,woodpecker:11,bear:7};
const toast=message=>window.officeBridge?.toast(message);
function openMeeting(anchor){document.dispatchEvent(new CustomEvent('office-meeting-room',{detail:anchor}))}
function openMailbox(){document.dispatchEvent(new Event('office-open-approvals'))}
function closePopover(){activeStaff=null}
function openStaff(p){
	activeStaff=p.id;wake();
	if(p.id==='owner')return openMailbox();
	if(p.id==='otter')return openMeeting(canvas);
	if(Object.hasOwn(roleIds,p.id))return document.dispatchEvent(new CustomEvent('office-open-person',{detail:roleIds[p.id]}));
	const esc=window.officeBridge.esc;
	window.officeBridge.openPanel(p.name,'<p class="intro">'+esc(p.dept+' · '+p.role)+'</p><p>연결된 역할 상태가 없습니다.</p>');
}
function refreshStaff(p){
	markerLayoutDirty=true;
	const node=markerNodes.get(p.id);
	if(node){node.querySelector('.status-bubble').className='status-bubble '+p.status;node.querySelector('.bubble-copy').textContent=statusLabel(p);node.setAttribute('aria-label',`${statusLabel(p)} / ${teamRole(p)} / ${p.name}`)}
	wake();
}
const canvas=$('#diorama-canvas');
let renderer,scene,camera,meshGroup,frame=0,needsFrame=false,lastTime=0,W=1,H=1,frustum=60;
const initial={yaw:.66,elevation:.78,zoom:1,panX:0,panZ:0};
const view={...initial},goal={...initial};let velocity={x:0,y:0},zoomAnchor=null;
const pointers=new Map();let gesture=null,previousPinch=null;
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
const projected=new Map(),markerNodes=new Map(),roomNodes=[];
let hoveredStaff=null;
function statusLabel(p){return p.bubble||stateText[p.status]||'상태 미연결'}
function markerDetail(p){return view.zoom>=.86||hoveredStaff===p.id||activeStaff===p.id}
function markerAnchor(p){return project(p.x+.45,p.y+.45,p.level+(p.id==='owner'?4.4:p.animal==='rabbit'?4.25:p.animal==='deer'?4.45:3.85))}
function buildMarkers(){
	const layer=$('#world-labels');layer.replaceChildren();roomNodes.length=0;
	for(const label of labels){const node=document.createElement(label.id==='meeting'?'button':'div');if(label.id==='meeting'){node.type='button';node.onclick=()=>openMeeting(node)}node.className='diorama-room-label'+(label.locked?' locked':'');node.textContent=label.text;if(label.locked){const sub=document.createElement('small');sub.textContent='잠금 · 보기 전용';node.append(sub)}const link=document.createElement('i');link.className='diorama-room-leader';layer.append(link,node);roomNodes.push({node,label,link})}
	for(const p of staff){
		const b=document.createElement('button');b.type='button';
		const bubble=document.createElement('span');bubble.className='status-bubble '+p.status;
		const copy=document.createElement('span');copy.className='bubble-copy';copy.textContent=statusLabel(p);
		const team=document.createElement('span');team.className='team-label';team.textContent=teamRole(p);
		const name=document.createElement('span');name.className='name-label';name.textContent=p.name;
		bubble.append(copy);b.append(bubble,team,name);b.className='agent-marker'+(p.id==='owner'?' owner':'');
		b.setAttribute('aria-label',`${statusLabel(p)} / ${teamRole(p)} / ${p.name} · ${p.species}`);
		b.onclick=()=>openStaff(p);
		b.onpointerenter=()=>{hoveredStaff=p.id;wake()};b.onpointerleave=()=>{if(hoveredStaff===p.id)hoveredStaff=null;wake()};
		b.onfocus=()=>{hoveredStaff=p.id;wake()};b.onblur=()=>{if(hoveredStaff===p.id)hoveredStaff=null;wake()};
		layer.append(b);markerNodes.set(p.id,b);
	}

}
function textSprite(text,x,y,z,width=2.7){
	const c=document.createElement('canvas');c.width=512;c.height=110;const g=c.getContext('2d');g.fillStyle='#fffdf2';g.fillRect(0,0,512,110);g.strokeStyle='#9eaf92';g.lineWidth=6;g.strokeRect(3,3,506,104);g.fillStyle='#293d34';g.font='600 43px "Apple SD Gothic Neo","Malgun Gothic",sans-serif';g.textAlign='center';g.textBaseline='middle';g.fillText(text,256,56,490);
	const texture=new THREE.CanvasTexture(c);texture.colorSpace=THREE.SRGBColorSpace;const s=new THREE.Sprite(new THREE.SpriteMaterial({map:texture,depthTest:true}));s.scale.set(width,width*110/512,1);s.position.set(x-MAP.w/2,z,y-MAP.d/2);meshGroup.add(s);
}
let waterTexture=null,waterClock=0;const waterStreaks=[];
function buildWater(){
	const tile=document.createElement('canvas');tile.width=tile.height=256;const g=tile.getContext('2d');
	g.fillStyle='#aedbd5';g.fillRect(0,0,256,256);g.strokeStyle='#d6efdf';g.lineWidth=2;
	for(let y=15;y<256;y+=34)for(let x=(Math.floor(y/34)%2)*22;x<256;x+=64){g.beginPath();g.moveTo(x,y);g.bezierCurveTo(x+8,y-3,x+16,y+3,x+25,y);g.stroke()}
	waterTexture=new THREE.CanvasTexture(tile);waterTexture.wrapS=waterTexture.wrapT=THREE.RepeatWrapping;waterTexture.repeat.set(24,24);waterTexture.colorSpace=THREE.SRGBColorSpace;
	const ocean=new THREE.Mesh(new THREE.PlaneGeometry(420,420),new THREE.MeshLambertMaterial({map:waterTexture}));ocean.rotation.x=-Math.PI/2;ocean.position.y=-1.06;scene.add(ocean);
	const geometry=new THREE.BoxGeometry(.13,.025,1.9),material=new THREE.MeshBasicMaterial({color:'#e6f8ef',transparent:true,opacity:.82});
	for(const r of stream)for(let i=0;i<5;i++){const mesh=new THREE.Mesh(geometry,material),offset=(i+.5)*r.d/5;scene.add(mesh);waterStreaks.push({mesh,r,offset,speed:.7+i*.09})}
	updateWater(0);
}
function updateWater(dt){
	if(!waterTexture)return false;if(!reduced){waterClock+=dt;waterTexture.offset.y=(waterClock*.008)%1}
	for(const w of waterStreaks){if(!reduced)w.offset=(w.offset+dt*w.speed)%w.r.d;const y=w.r.y+w.offset;w.mesh.position.set(w.r.x+w.r.w*(.2+(waterStreaks.indexOf(w)%5)*.15)+Math.sin(y*.13)*.55-MAP.w/2,w.r.z+.18,y-MAP.d/2)}
	return !reduced;
}
function uploadBlocks(){
	meshGroup=new THREE.Group();scene.add(meshGroup);
	const geometry=new THREE.BoxGeometry(1,1,1),matrix=new THREE.Matrix4(),q=new THREE.Quaternion(),pos=new THREE.Vector3(),scale=new THREE.Vector3();
	for(const alpha of [1,.23,.3]){
		const blocks=cubes.filter(b=>(b.alpha??1)===alpha);if(!blocks.length)continue;
		const material=new THREE.MeshLambertMaterial({color:0xffffff,transparent:alpha<1,opacity:alpha,depthWrite:alpha===1});
		const batch=new THREE.InstancedMesh(geometry,material,blocks.length);
		blocks.forEach((b,i)=>{pos.set(b.x+b.w/2-MAP.w/2,b.z+b.h/2,b.y+b.d/2-MAP.d/2);scale.set(b.w,b.h,b.d);matrix.compose(pos,q,scale);batch.setMatrixAt(i,matrix);batch.setColorAt(i,new THREE.Color(b.color))});batch.instanceMatrix.needsUpdate=true;batch.instanceColor.needsUpdate=true;batch.computeBoundingSphere();meshGroup.add(batch);
	}
	// 캐릭터의 두 자세만 캐시하고 이동 중에는 그룹의 위치·방향만 바꾼다.
	for(const p of staff){const poses={};for(const seated of [true,false]){const start=cubes.length;animal({...p,x:0,y:0,level:0,facing:0,seated});poses[seated]=cubes.splice(start)}actorBlocks.set(p.id,poses);
		const mesh=new THREE.InstancedMesh(geometry,new THREE.MeshLambertMaterial({color:0xffffff}),poses.true.length);mesh.frustumCulled=false;mesh.userData.seated=null;meshGroup.add(mesh);actorMeshes.set(p.id,mesh);syncActor(p);
	}
	for(const e of engines)textSprite(e.name,e.at[0]+.55,e.at[1]+.5,3.8+roomById('server').level,2.65);
	const office=roomById('owner-room');textSprite('대표실',office.x,office.at,office.level+3.83,3);
	buildMailbox(geometry);
	buildMarkers();
}
let mailboxFlag=null,mailboxLetter=null,flagAngle=0;
function buildMailbox(geometry){
	const batch=draw=>{const start=cubes.length;draw();const blocks=cubes.splice(start),mesh=new THREE.InstancedMesh(geometry,new THREE.MeshLambertMaterial({color:0xffffff}),blocks.length),matrix=new THREE.Matrix4(),q=new THREE.Quaternion(),pos=new THREE.Vector3(),scale=new THREE.Vector3();blocks.forEach((b,i)=>{pos.set(b.x+b.w/2,b.z+b.h/2,b.y+b.d/2);scale.set(b.w,b.h,b.d);matrix.compose(pos,q,scale);mesh.setMatrixAt(i,matrix);mesh.setColorAt(i,new THREE.Color(b.color))});mesh.instanceMatrix.needsUpdate=true;mesh.instanceColor.needsUpdate=true;mesh.frustumCulled=false;meshGroup.add(mesh);return mesh};
	mailboxFlag=batch(()=>{box(-.06,-.05,0,.12,.12,1.2,'#e5b67b');box(.06,-.05,.78,.6,.12,.42,'#cf5a44')});mailboxFlag.position.set(mailboxAt.x+1.85-MAP.w/2,mailboxAt.z+1.95,mailboxAt.y+.7-MAP.d/2);
	mailboxLetter=batch(()=>{box(-.45,-.07,-.28,.9,.14,.56,'#fff3d4');box(-.38,.08,-.02,.76,.025,.045,'#c59168');box(-.07,.08,-.1,.14,.03,.14,'#c76850')});mailboxLetter.visible=false;
}
function updateMailboxMarker(){const b=$('#mailbox-marker'),v=project(mailboxAt.x+.9,mailboxAt.y+.65,mailboxAt.z+3.8);b.hidden=!v.visible||mailCount===0;if(!b.hidden){b.style.left=clamp(v.x,25,Math.max(25,W-25))+'px';b.style.top=clamp(v.y,35,Math.max(35,H-70))+'px'}}
function updateMailbox(dt){
	let dirty=false;const target=mailCount?0:-Math.PI/2;
	if(Math.abs(flagAngle-target)>.001){flagAngle=reduced?target:flagAngle+(target-flagAngle)*Math.min(1,dt*10);if(Math.abs(flagAngle-target)<.001)flagAngle=target;dirty=true}if(mailboxFlag)mailboxFlag.rotation.z=flagAngle;

	return dirty;
}
function syncActor(p){const mesh=actorMeshes.get(p.id);if(!mesh)return;
	if(mesh.userData.seated!==p.seated){const matrix=new THREE.Matrix4(),q=new THREE.Quaternion(),pos=new THREE.Vector3(),scale=new THREE.Vector3();actorBlocks.get(p.id)[p.seated].forEach((b,i)=>{pos.set(b.x+b.w/2-.45,b.z+b.h/2,b.y+b.d/2-.45);scale.set(b.w,b.h,b.d);matrix.compose(pos,q,scale);mesh.setMatrixAt(i,matrix);mesh.setColorAt(i,new THREE.Color(b.color))});mesh.instanceMatrix.needsUpdate=true;mesh.instanceColor.needsUpdate=true;mesh.userData.seated=p.seated}
	mesh.position.set(p.x+.45-MAP.w/2,p.level+((p.motion||p.manualMoving)&&!reduced?Math.sin(p.stride||0)*.085:0),p.y+.45-MAP.d/2);mesh.rotation.y=p.heading??(p.facing===2?Math.PI:0);mesh.rotation.z=(p.motion||p.manualMoving)&&!reduced?Math.sin(p.stride||0)*.055:0;
}
const ownerKeys=new Set();let ownerControlled=false,ownerTarget=null,ownerRoute=[],floorBlocks=[],pickBlocks=[];
const WALK_STEP=.5,walkCols=Math.ceil(MAP.w/WALK_STEP)+1,walkRows=Math.ceil(MAP.d/WALK_STEP)+1;
let floorBins=new Map(),walkGrid=new Uint8Array(walkCols*walkRows),roomGrid=new Int16Array(walkCols*walkRows);
function takeOwnerControl(){
	const p=person('owner');if(!ownerControlled){ownerControlled=true;p.motion=null;p.seated=false;p.bubble='둘러보는 중';p.task='직접 숲속 사무실을 둘러봐요.';refreshStaff(p)}
}
function buildWalkMap(){
	floorBins=new Map();walkGrid.fill(0);roomGrid.fill(0);
	for(const b of floorBlocks){
		for(let x=Math.floor(b.x/2);x<=Math.floor((b.x+b.w)/2);x++)for(let y=Math.floor(b.y/2);y<=Math.floor((b.y+b.d)/2);y++){const key=x+','+y;if(!floorBins.has(key))floorBins.set(key,[]);floorBins.get(key).push(b)}
		const minX=Math.max(0,Math.ceil(b.x/WALK_STEP)),maxX=Math.min(walkCols-1,Math.floor((b.x+b.w)/WALK_STEP));
		const minY=Math.max(0,Math.ceil(b.y/WALK_STEP)),maxY=Math.min(walkRows-1,Math.floor((b.y+b.d)/WALK_STEP));
		for(let y=minY;y<=maxY;y++)for(let x=minX;x<=maxX;x++){const id=y*walkCols+x;walkGrid[id]|=b.walkable==='path'?1:2;if(b.roomId)roomGrid[id]=rooms.findIndex(r=>r.id===b.roomId)+1}
	}
}
function walkArea(x,y){let height=-Infinity,room='',path=false;for(const b of floorBins.get(Math.floor(x/2)+','+Math.floor(y/2))||[])if(x>=b.x&&x<=b.x+b.w&&y>=b.y&&y<=b.y+b.d){height=Math.max(height,b.z+b.h);if(b.walkable==='path')path=true;else room=b.roomId}return Number.isFinite(height)?{height:height+.01,room,path}:null}
function floorHeight(x,y){return walkArea(x,y)?.height??null}
function canWalkBetween(x1,y1,x2,y2){
	const steps=Math.max(1,Math.ceil(Math.hypot(x2-x1,y2-y1)/.16));let previous=walkArea(x1,y1);if(!previous)return false;
	for(let i=1;i<=steps;i++){const q=walkArea(x1+(x2-x1)*i/steps,y1+(y2-y1)*i/steps);if(!q||(previous.room&&q.room&&previous.room!==q.room&&!previous.path&&!q.path))return false;previous=q}return true;
}
function nearestWalkCell(x,y){
	let best=null;const cx=Math.round(x/WALK_STEP),cy=Math.round(y/WALK_STEP);
	for(let dy=-2;dy<=2;dy++)for(let dx=-2;dx<=2;dx++){const a=cx+dx,b=cy+dy;if(a<0||b<0||a>=walkCols||b>=walkRows)continue;const id=b*walkCols+a,d=Math.hypot(a*WALK_STEP-x,b*WALK_STEP-y);if(walkGrid[id]&&(!best||d<best.d)&&canWalkBetween(x,y,a*WALK_STEP,b*WALK_STEP))best={id,d}}
	return best?.id??-1;
}
function findWalkRoute(startX,startY,endX,endY){
	const start=nearestWalkCell(startX,startY),end=nearestWalkCell(endX,endY);if(start<0||end<0)return null;
	const score=new Float64Array(walkGrid.length);score.fill(Infinity);score[start]=0;const previous=new Int32Array(walkGrid.length);previous.fill(-1);const done=new Uint8Array(walkGrid.length),heap=[];
	const heuristic=id=>Math.hypot(id%walkCols-end%walkCols,Math.floor(id/walkCols)-Math.floor(end/walkCols));
	function push(id,f){let i=heap.length;heap.push({id,f});while(i){const parent=(i-1)>>1;if(heap[parent].f<=f)break;heap[i]=heap[parent];i=parent}heap[i]={id,f}}
	function pop(){const result=heap[0],last=heap.pop();if(heap.length){let i=0;while(2*i+1<heap.length){let c=2*i+1;if(c+1<heap.length&&heap[c+1].f<heap[c].f)c++;if(heap[c].f>=last.f)break;heap[i]=heap[c];i=c}heap[i]=last}return result.id}
	push(start,heuristic(start));
	while(heap.length){const id=pop();if(done[id])continue;if(id===end)break;done[id]=1;const x=id%walkCols,y=Math.floor(id/walkCols);
		for(const [dx,dy] of [[1,0],[-1,0],[0,1],[0,-1],[1,1],[1,-1],[-1,1],[-1,-1]]){const nx=x+dx,ny=y+dy;if(nx<0||ny<0||nx>=walkCols||ny>=walkRows)continue;const next=ny*walkCols+nx;if(!walkGrid[next]||done[next])continue;
			if(dx&&dy&&(!walkGrid[y*walkCols+nx]||!walkGrid[ny*walkCols+x]))continue;
			if(roomGrid[id]&&roomGrid[next]&&roomGrid[id]!==roomGrid[next]&&!(walkGrid[id]&1)&&!(walkGrid[next]&1))continue;
			if(!canWalkBetween(x*WALK_STEP,y*WALK_STEP,nx*WALK_STEP,ny*WALK_STEP))continue;
			const cost=score[id]+(dx&&dy?Math.SQRT2:1);if(cost<score[next]){score[next]=cost;previous[next]=id;push(next,cost+heuristic(next))}
		}
	}
	if(start!==end&&previous[end]<0)return null;const route=[];for(let id=end;id!==-1;id=previous[id]){route.push({x:(id%walkCols)*WALK_STEP,y:Math.floor(id/walkCols)*WALK_STEP});if(id===start)break}
	route.reverse();route.push({x:endX,y:endY});return route;
}
function moveOwnerTo(x,y){
	const ray=new THREE.Raycaster();ray.setFromCamera(new THREE.Vector2(x/W*2-1,1-y/H*2),camera);const o=ray.ray.origin,d=ray.ray.direction;let best=null;
	for(const b of pickBlocks){const t=(b.z+b.h-o.y)/d.y;if(t<0||!Number.isFinite(t)||(best&&t>=best.t))continue;const px=o.x+d.x*t+MAP.w/2,py=o.z+d.z*t+MAP.d/2;if(px>=b.x&&px<=b.x+b.w&&py>=b.y&&py<=b.y+b.d)best={x:px,y:py,t,blocked:!!b.blocked}}
	if(!best||best.blocked){toast('방, 길 또는 다리를 선택해 주세요.');return}
	const p=person('owner'),route=findWalkRoute(p.x+.45,p.y+.45,best.x,best.y);if(!route){toast('이 위치까지 이어진 길이 없어요.');return}
	takeOwnerControl();ownerKeys.clear();ownerRoute=route;ownerTarget=best;wake();
}
function updateOwner(dt){
	if(!ownerControlled)return false;const p=person('owner');let dx=0,dy=0;
	const forward=(ownerKeys.has('w')?1:0)-(ownerKeys.has('s')?1:0),right=(ownerKeys.has('d')?1:0)-(ownerKeys.has('a')?1:0);
	if(forward||right){dx=right*Math.cos(view.yaw)-forward*Math.sin(view.yaw);dy=-right*Math.sin(view.yaw)-forward*Math.cos(view.yaw)}
	else if(ownerTarget){while(ownerRoute.length&&Math.hypot(ownerRoute[0].x-(p.x+.45),ownerRoute[0].y-(p.y+.45))<.01)ownerRoute.shift();if(!ownerRoute.length)ownerTarget=null;else{dx=ownerRoute[0].x-(p.x+.45);dy=ownerRoute[0].y-(p.y+.45)}}
	const distance=Math.hypot(dx,dy);
	if(distance<.01){if(p.manualMoving){p.manualMoving=false;syncActor(p);markerLayoutDirty=true;return true}return false}
	const step=Math.min(5*dt,forward||right?Infinity:distance),oldX=p.x+.45,oldY=p.y+.45,x=oldX+dx/distance*step,y=oldY+dy/distance*step,level=floorHeight(x,y);
	if(level===null||!canWalkBetween(oldX,oldY,x,y)){ownerTarget=null;ownerRoute=[];p.manualMoving=false;syncActor(p);markerLayoutDirty=true;return false}
	p.x=x-.45;p.y=y-.45;p.level=level;p.seated=false;p.manualMoving=true;p.heading=Math.atan2(dx,dy);p.stride=(p.stride||0)+dt*12;
	if(goal.zoom>1.01){zoomAnchor=null;goal.panX=x-MAP.w/2;goal.panZ=y-MAP.d/2}
	if(ownerTarget&&step>=distance){ownerRoute.shift();if(!ownerRoute.length)ownerTarget=null}syncActor(p);return true;
}
function project(x,y,z){const v=new THREE.Vector3(x-MAP.w/2,z,y-MAP.d/2).project(camera);return {x:(v.x+1)*W/2,y:(1-v.y)*H/2,visible:v.z>-1&&v.z<1&&v.x>-1&&v.x<1&&v.y>-1&&v.y<1}}
const labelOffsets=[0,-8,8,-16,16,-24,24,-32,32,-40,40].map(dy=>({dy}));
let markerLayoutDirty=true,markerViewKey="";
const markerRects=new Map();
function updateMarkers(){
	const key=[W,H,view.yaw,view.elevation,view.zoom,view.panX,view.panZ,activeStaff,hoveredStaff].join(','),full=markerLayoutDirty||key!==markerViewKey;
	markerViewKey=key;
	if(!full&&!staff.some(p=>p.motion||p.manualMoving))return;
	const occupied=full?[]:[...markerRects].filter(([id])=>!person(id).motion&&!person(id).manualMoving).map(([,r])=>r),scale=W<500&&view.zoom<2?1:clamp(1+(view.zoom-1)*.08,.94,W<500?1.08:1.22);
	if(full){markerRects.clear();updateMailboxMarker();const b=$('#mailbox-marker');if(!b.hidden){const x=parseFloat(b.style.left),y=parseFloat(b.style.top);occupied.push({x:x-b.offsetWidth/2,y:y-b.offsetHeight-25,w:b.offsetWidth,h:b.offsetHeight+25})}}
	const overlaps=(x,y,w,h)=>occupied.some(r=>x<r.x+r.w+3&&x+w+3>r.x&&y<r.y+r.h+3&&y+h+3>r.y);
	const priority=p=>p.id==='owner'||activeStaff===p.id||hoveredStaff===p.id?0:['question','approval','exhausted'].includes(p.status)?1:2;
	const minY=8,maxY=H-70;
	const place=(left,top,w,h)=>{let best=null;const columns=[0];for(const dx of columns){for(const offset of labelOffsets){const x=clamp(left+dx,4,Math.max(4,W-w-4)),y=top+offset.dy;if(y<minY||y+h>maxY||overlaps(x,y,w,h))continue;best={x,y};break}if(best)break}return best};
	for(const p of [...staff].sort((a,b)=>priority(a)-priority(b))){
		if(!full&&!p.motion&&!p.manualMoving)continue;
		const v=markerAnchor(p),node=markerNodes.get(p.id),detail=markerDetail(p);projected.set(p.id,v);node.hidden=!v.visible;
		const cls='agent-marker'+(p.id==='owner'?' owner':'')+(detail?' detail':' distant')+(activeStaff===p.id?' selected':'')+(view.zoom<1.6&&activeStaff!==p.id&&hoveredStaff!==p.id?' compact':'');if(node.className!==cls){node.className=cls;markerLayoutDirty=true}if(!v.visible)continue;
		if(markerLayoutDirty||!node.markerSize)node.markerSize={w:node.offsetWidth||150,h:node.offsetHeight||(detail?64:28)};
		const w=node.markerSize.w*scale,h=node.markerSize.h*scale,left=clamp(v.x-w/2,4,Math.max(4,W-w-4)),top=v.y-h-7;const position=place(left,top,w,h);if(!position){node.hidden=true;markerRects.delete(p.id);continue}const {x,y}=position;
		const rect={x,y,w,h};occupied.push(rect);markerRects.set(p.id,rect);node.style.transform=`translate(${x}px,${y}px) scale(${scale})`;node.style.zIndex=priority(p)===0?'6':'3';
	}
	// 방 간판은 직원 묶음에 자리를 내주며, 충분한 여백에서만 표시한다.
	if(full)for(const {node,link,label:l,width,height} of roomNodes){const v=project(l.x,l.y,l.z),w=width||100,h=height||25,x=clamp(v.x-w/2,4,W-w-4),y=v.y-h;node.hidden=link.hidden=!v.visible||y<minY||y+h>maxY||overlaps(x,y,w,h);if(node.hidden)continue;occupied.push({x,y,w,h});node.style.transform=`translate(${x}px,${y}px)`;link.style.width='0px'}
	markerLayoutDirty=false;
}
function applyCamera(){
	camera.zoom=view.zoom;const aspect=W/H;camera.left=-frustum*aspect/2;camera.right=frustum*aspect/2;camera.top=frustum/2;camera.bottom=-frustum/2;camera.updateProjectionMatrix();
	const ce=Math.cos(view.elevation),r=90;camera.position.set(view.panX+r*Math.sin(view.yaw)*ce,r*Math.sin(view.elevation),view.panZ+r*Math.cos(view.yaw)*ce);camera.lookAt(view.panX,0,view.panZ);camera.updateMatrixWorld();
}
function clampPan(){const limit=Math.max(MAP.w,MAP.d)*.55*(1-1/Math.max(1,goal.zoom))+2.8;goal.panX=clamp(goal.panX,-limit,limit);goal.panZ=clamp(goal.panZ,-limit,limit)}
const groundPlane=()=>new THREE.Plane(new THREE.Vector3(0,1,0),-.2);
function groundAt(x,y){const ray=new THREE.Raycaster();ray.setFromCamera(new THREE.Vector2(x/W*2-1,1-y/H*2),camera);const point=new THREE.Vector3();return ray.ray.intersectPlane(groundPlane(),point)}
function wake(){if(!renderer||needsFrame)return;needsFrame=true;lastTime=0;frame=requestAnimationFrame(animate)}
function animate(time){
	needsFrame=false;const dt=lastTime?Math.min((time-lastTime)/1000,.05):1/60;lastTime=time;
	if(!pointers.size){goal.yaw+=velocity.x*dt;goal.elevation=clamp(goal.elevation+velocity.y*dt,.64,1.25);const decay=Math.exp(-9*dt);velocity.x*=decay;velocity.y*=decay}
	const smoothing=reduced?1:1-Math.exp(-13*dt);
	const cameraMoving=Object.keys(initial).some(k=>Math.abs(goal[k]-view[k])>.0001)||Math.abs(velocity.x)+Math.abs(velocity.y)>.0001;
	for(const key of Object.keys(initial))view[key]+=(goal[key]-view[key])*smoothing;
	if(cameraMoving||markerLayoutDirty)applyCamera();
	if(zoomAnchor?.world){const after=groundAt(zoomAnchor.x,zoomAnchor.y);if(after){const dx=zoomAnchor.world.x-after.x,dz=zoomAnchor.world.z-after.z;view.panX+=dx;view.panZ+=dz;goal.panX+=dx;goal.panZ+=dz;clampPan();view.panX=clamp(view.panX,-(Math.max(MAP.w,MAP.d)*.55*(1-1/Math.max(1,view.zoom))+2.8),2.8+Math.max(0,view.zoom-1)*6);view.panZ=clamp(view.panZ,-(Math.max(MAP.w,MAP.d)*.55*(1-1/Math.max(1,view.zoom))+2.8),2.8+Math.max(0,view.zoom-1)*6);applyCamera()}if(Math.abs(view.zoom-goal.zoom)<.0005)zoomAnchor=null}
	const ownerMoving=updateOwner(dt),mailMoving=updateMailbox(dt),waterMoving=updateWater(dt);if(cameraMoving||markerLayoutDirty||ownerMoving||mailMoving||waterMoving)renderer.render(scene,camera);updateMarkers();$('#diorama-zoom-label').textContent=Math.round(view.zoom*100)+'%';$('#diorama-zoom-in').disabled=goal.zoom>=8;$('#diorama-zoom-out').disabled=goal.zoom<=.72;
	const unsettled=Object.keys(initial).some(k=>Math.abs(goal[k]-view[k])>.0001)||Math.abs(velocity.x)+Math.abs(velocity.y)>.0001;
	if(!needsFrame&&(unsettled||pointers.size||ownerMoving||ownerKeys.size||ownerTarget||waterMoving||Math.abs(flagAngle-(mailCount?0:-Math.PI/2))>.001)){needsFrame=true;frame=requestAnimationFrame(animate)}
}
function resize(){const rect=canvas.getBoundingClientRect();W=Math.max(rect.width,1);H=Math.max(rect.height,1);renderer.setSize(W,H,false);
	// 모든 회전각에서 섬 전체가 초기 배율 안에 들어오는 고정 프러스텀.
	for(const item of roomNodes){item.width=item.node.offsetWidth;item.height=item.node.offsetHeight}markerLayoutDirty=true;const spanX=MAP.w*Math.cos(initial.yaw)+MAP.d*Math.sin(initial.yaw),spanY=(MAP.w*Math.sin(initial.yaw)+MAP.d*Math.cos(initial.yaw))*Math.sin(initial.elevation)+10;frustum=Math.max(spanX/(W/H)*1.03,spanY*H/Math.max(1,H-235));applyCamera();wake()}
function changeZoom(factor,x=W/2,y=H/2){zoomAnchor={x,y,world:groundAt(x,y)};if(!zoomAnchor.world)zoomAnchor=null;goal.zoom=clamp(goal.zoom*factor,.72,8);clampPan();wake()}
function turn(delta){goal.yaw+=delta*Math.PI/2;velocity={x:0,y:0};zoomAnchor=null;wake()}
function resetView(){view.yaw-=Math.round((view.yaw-initial.yaw)/(Math.PI*2))*Math.PI*2;Object.assign(goal,initial);velocity={x:0,y:0};zoomAnchor=null;closePopover();wake()}
function pan(dx,dy){const span=frustum/view.zoom/H;goal.panX-=Math.cos(view.yaw)*dx*span+Math.sin(view.yaw)*dy*span/Math.sin(view.elevation);goal.panZ+=Math.sin(view.yaw)*dx*span-Math.cos(view.yaw)*dy*span/Math.sin(view.elevation);clampPan()}
function setupControls(){
	const local=e=>{const r=canvas.getBoundingClientRect();return {x:e.clientX-r.left,y:e.clientY-r.top}};
	canvas.addEventListener('contextmenu',e=>e.preventDefault());
	canvas.addEventListener('pointerdown',e=>{if(![0,1,2].includes(e.button))return;e.preventDefault();canvas.setPointerCapture(e.pointerId);const p=local(e);pointers.set(e.pointerId,p);velocity={x:0,y:0};zoomAnchor=null;gesture={start:p,last:p,moved:false,pan:e.button!==0||e.shiftKey,time:performance.now()};previousPinch=null;wake()});
	canvas.addEventListener('pointerleave',()=>{if(!pointers.size&&hoveredStaff){hoveredStaff=null;wake()}});
	canvas.addEventListener('pointermove',e=>{if(!pointers.has(e.pointerId)){if(e.pointerType!=='touch'){const p=local(e),id=nearestStaff(p.x,p.y)?.id??null;if(id!==hoveredStaff){hoveredStaff=id;wake()}}return};const p=local(e),old=pointers.get(e.pointerId);pointers.set(e.pointerId,p);
		if(pointers.size===2){gesture.moved=true;const [a,b]=[...pointers.values()],pinch={distance:Math.hypot(a.x-b.x,a.y-b.y),x:(a.x+b.x)/2,y:(a.y+b.y)/2};if(previousPinch){pan(pinch.x-previousPinch.x,pinch.y-previousPinch.y);changeZoom(pinch.distance/Math.max(1,previousPinch.distance),pinch.x,pinch.y)}previousPinch=pinch;return}
		const dx=p.x-old.x,dy=p.y-old.y;if(Math.hypot(p.x-gesture.start.x,p.y-gesture.start.y)>5)gesture.moved=true;
		const now=performance.now(),dt=Math.max(.008,(now-gesture.time)/1000);gesture.time=now;
		if(gesture.pan){pan(dx,dy);velocity={x:0,y:0}}else{const ax=-dx*.006,ay=dy*.004;goal.yaw+=ax;goal.elevation=clamp(goal.elevation+ay,.64,1.25);velocity={x:clamp(ax/dt,-3.5,3.5),y:clamp(ay/dt,-1.5,1.5)}}gesture.last=p;wake()
	});
	const end=(e,cancel=false)=>{if(!pointers.has(e.pointerId))return;const p=local(e);pointers.delete(e.pointerId);if(!cancel&&!gesture?.moved&&e.button===0&&!selectNearest(p.x,p.y))moveOwnerTo(p.x,p.y);if(cancel||performance.now()-(gesture?.time??0)>90||reduced)velocity={x:0,y:0};if(pointers.size){const remaining=[...pointers.values()][0];gesture={start:remaining,last:remaining,moved:true,pan:true,time:performance.now()}}else gesture=null;previousPinch=null;wake()};
	canvas.addEventListener('pointerup',e=>end(e));canvas.addEventListener('pointercancel',e=>end(e,true));canvas.addEventListener('lostpointercapture',e=>{if(pointers.has(e.pointerId))end(e,true)});
	canvas.addEventListener('wheel',e=>{e.preventDefault();const p=local(e);const delta=e.deltaY*(e.deltaMode===1?16:e.deltaMode===2?H:1);changeZoom(Math.exp(-clamp(delta,-200,200)*.0015),p.x,p.y)},{passive:false});
	canvas.tabIndex=0;canvas.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','+','=','-','Home'].includes(e.key))return;e.preventDefault();if(e.key==='Home')resetView();else if(e.key==='+'||e.key==='=')changeZoom(1.15);else if(e.key==='-')changeZoom(1/1.15);else if(e.shiftKey){pan(e.key==='ArrowLeft'?30:e.key==='ArrowRight'?-30:0,e.key==='ArrowUp'?30:e.key==='ArrowDown'?-30:0);wake()}else {goal.yaw+=e.key==='ArrowLeft'?-.15:e.key==='ArrowRight'?.15:0;goal.elevation=clamp(goal.elevation+(e.key==='ArrowUp'?.07:e.key==='ArrowDown'?-.07:0),.64,1.25);wake()}});
}
function nearestStaff(x,y){const near=staff.map(p=>({p,v:project(p.x+.45,p.y+.45,p.level+2.2)})).filter(({v})=>v.visible).map(({p,v})=>({p,d:Math.hypot(v.x-x,v.y-y)})).sort((a,b)=>a.d-b.d)[0];return near&&near.d<22?near.p:null}
function selectNearest(x,y){const m=project(mailboxAt.x+.9,mailboxAt.y+.65,mailboxAt.z+2);if(m.visible&&Math.hypot(m.x-x,m.y-y)<22){openMailbox();return true}const p=nearestStaff(x,y);if(p){openStaff(p);return true}closePopover();return false}

function syncState(data){
	mailCount=data?Number($('#mailbox-marker').dataset.pending)||0:0;markerLayoutDirty=true;
	$('#mailbox-marker').setAttribute('aria-label','결재함 열기 · 확인할 항목 '+mailCount+'건');
	for(const p of staff){
		const role=Object.hasOwn(roleIds,p.id)?data?.people?.find(row=>row.id===roleIds[p.id]):null;
		p.status=role?({'작업 중':'working','대기':'resting','쉬는 중':'resting','오류':'question','확인 불가':'unknown'}[role.status]||'unknown'):'unknown';
		if(p.id!=='owner')p.bubble='';
		refreshStaff(p);
	}
	wake();
}
function start(){
	try{
		renderer=new THREE.WebGLRenderer({canvas,antialias:true,alpha:true,powerPreference:'high-performance'});
		renderer.setPixelRatio(Math.min(devicePixelRatio||1,1.7));renderer.outputColorSpace=THREE.SRGBColorSpace;
		scene=new THREE.Scene();scene.background=new THREE.Color('#aedbd5');camera=new THREE.OrthographicCamera(-30,30,30,-30,.1,220);
		scene.add(new THREE.AmbientLight('#ffffff',1.8));
		const sun=new THREE.DirectionalLight('#fff9ed',1.35);sun.position.set(-25,55,35);scene.add(sun);
		const fill=new THREE.DirectionalLight('#eaf4ff',.65);fill.position.set(20,15,-15);scene.add(fill);
		build();floorBlocks=cubes.filter(b=>b.walkable);pickBlocks=cubes.filter(b=>b.walkable||b.blocked);buildWalkMap();
		buildWater();uploadBlocks();setupControls();
		document.getElementById('world').classList.add('diorama-active');
		document.getElementById('brand-name').textContent='숲속 오피스';
		document.getElementById('brand-caption').textContent='블록 사무실 · 로컬 상태 · 10초마다 갱신';
		new ResizeObserver(resize).observe(canvas);resize();
		canvas.addEventListener('webglcontextlost',e=>{e.preventDefault();cancelAnimationFrame(frame);document.getElementById('world').classList.remove('diorama-active');$('#diorama-error').hidden=false});
		canvas.addEventListener('webglcontextrestored',()=>{document.getElementById('world').classList.add('diorama-active');$('#diorama-error').hidden=true;markerLayoutDirty=true;wake()});
		syncState(window.officeBridge.state());
	}catch(error){document.getElementById('world').classList.remove('diorama-active');$('#diorama-error').hidden=false}
}
$('#diorama-rotate-left').onclick=()=>turn(-1);
$('#diorama-rotate-right').onclick=()=>turn(1);
$('#diorama-zoom-in').onclick=()=>changeZoom(1.15);
$('#diorama-zoom-out').onclick=()=>changeZoom(1/1.15);
$('#diorama-reset').onclick=resetView;
$('#mailbox-marker').onclick=openMailbox;
window.addEventListener('keydown',e=>{
	const key=e.key.toLowerCase();
	if(!document.getElementById('world').classList.contains('diorama-active')||!['w','a','s','d'].includes(key)||e.ctrlKey||e.metaKey||e.altKey||e.target?.closest?.('input,textarea,select,[contenteditable="true"]')||document.querySelector('dialog[open]')||(matchMedia('(max-width:900px)').matches&&document.body.classList.contains('panel-open')))return;
	e.preventDefault();takeOwnerControl();ownerTarget=null;ownerRoute=[];ownerKeys.add(key);wake();
});
window.addEventListener('keyup',e=>{ownerKeys.delete(e.key.toLowerCase());wake()});
window.addEventListener('blur',()=>{ownerKeys.clear();wake()});
document.addEventListener('office-state',event=>syncState(event.detail));
if(window.officeBridge)start();else document.addEventListener('office-ready',start,{once:true});
