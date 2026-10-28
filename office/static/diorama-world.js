// 확정된 블록 장면의 지형, 건물과 소품.
const C={"wall":"#e9eddf","ground":"#edf1df","scene":"#f4f6f1","paper":"#f7f8f4","surface":"#fffefa","ink":"#293d34","muted":"#66756a","green":"#42664c","wood":"#d8bb93","soil":"#b49b81","stone":"#c1c8bd","secretary":"#f6eac6","infra":"#dceef2","data":"#e7e0f2","content":"#f5dfd8","sub":"#e2edcf","company":"#dee9f4","reception":"#f3ecd5","review":"#f2e8d4","server":"#e0e9ed","lounge":"#e3eed7","working":"#397451","meeting":"#557578","resting":"#647488","question":"#956620","approval":"#a45e3c","exhausted":"#795894"};
const P={"0001":"#0001","152c3130":"#152c3130","25372e50":"#25372e50","293d34":"#293d34","293d3405":"#293d3405","293d340c":"#293d340c","31533b":"#31533b","32492b20":"#32492b20","344d46":"#344d46","35423a":"#35423a","3d514c":"#8eaaa5","3e8160":"#3e8160","42664c":"#42664c","435a42":"#435a42","485861":"#485861","486b4b":"#486b4b","4b5950":"#4b5950","4d5c50":"#4d5c50","4f5a4f":"#4f5a4f","52564a":"#52564a","525b54":"#a7b3a5","52675b":"#52675b","535d55":"#9ba99d","586456":"#586456","586762":"#586762","5c6b4500":"#5c6b4500","5c6b4532":"#5c6b4532","5f6959":"#c1cbb8","62685d":"#bdc4b6","637061":"#637061","657a5e":"#657a5e","657e4c":"#98b47b","697b69":"#b8cba9","697e75":"#697e75","6c5040":"#6c5040","6d8179":"#6d8179","6f7f6a":"#b8cba9","718e8e":"#718e8e","749c83":"#749c83","759267":"#759267","787f69":"#787f69","7a8175":"#bdc6b7","7b8277":"#7b8277","7b8c74":"#7b8c74","7b9084":"#7b9084","7d8474":"#bcc6b5","7f8174":"#b7bdaf","7f8d9f":"#7f8d9f","839c9c":"#839c9c","887b5d":"#887b5d","899c73":"#899c73","8b9687":"#8b9687","8b9d8d":"#8b9d8d","8c9b79":"#8c9b79","8da994":"#8da994","8e9c83":"#8e9c83","8ea077":"#8ea077","8f7157":"#8f7157","8fa268":"#abc58d","91a175":"#91a175","929d92":"#929d92","93ad83":"#93ad83","94745c":"#94745c","96836c":"#96836c","969c89":"#969c89","9776af":"#9776af","9a815f":"#d0b994","9a9e91":"#9a9e91","9aa18f":"#9aa18f","9d8966":"#9d8966","9d9d87":"#9d9d87","9e8870":"#9e8870","9eaf92":"#9eaf92","a1a6a0":"#a1a6a0","a46940":"#a46940","a5b48e":"#a5b48e","a68c6e":"#a68c6e","a6aaa0":"#a6aaa0","a6b59a":"#a6b59a","a88762":"#a88762","a8b39e50":"#a8b39e50","abd0a1":"#abd0a1","acc393":"#acc393","ae8d69":"#d3bd9e","af9775":"#af9775","b0b8a5":"#d9dfce","b18352":"#b18352","b3bd9f":"#b3bd9f","b5c8b0":"#b5c8b0","b6b7a8":"#b6b7a8","b88553":"#b88553","b8be93":"#b8be93","b8c1ad":"#b8c1ad","bab5c4":"#bab5c4","bac6ae":"#bac6ae","bcd5cf":"#bcd5cf","bdd1bc":"#bdd1bc","becdd5":"#becdd5","bfa482":"#bfa482","bfd5cf":"#bfd5cf","bfd8c4":"#bfd8c4","c1a27a":"#dec49e","c1ac85":"#c1ac85","c47950":"#c47950","c4bdcc":"#c4bdcc","c58b60":"#c58b60","c5b08a":"#c5b08a","c5c9af":"#c5c9af","c6aa7b":"#c6aa7b","c77950":"#c77950","c8cdb7":"#c8cdb7","caaea4":"#caaea4","cbd1c0":"#cbd1c0","cbd8b3":"#cbd8b3","ccd2be":"#ccd2be","ce9c56":"#ce9c56","cfccdf":"#cfccdf","d0cfb9":"#d0cfb9","d29b35":"#d29b35","d2d4bf":"#d2d4bf","d2d8c9":"#d2d8c9","d2d9c4":"#d2d9c4","d3d5c1":"#d3d5c1","d4a25b":"#d4a25b","d4a89c":"#d4a89c","d5b585":"#d5b585","d5d1ad":"#d5d1ad","d6dacb":"#d6dacb","d7bd67":"#d7bd67","d7c7a0":"#d7c7a0","d89056":"#d89056","d8b47f":"#d8b47f","d8bb93":"#d8bb93","d9cda9":"#d9cda9","d9d6c0":"#d9d6c0","dc986b":"#dc986b","ded6b7":"#ded6b7","dedac5":"#dedac5","dedfd4":"#dedfd4","dfd9c2":"#dfd9c2","e1e4d6":"#e1e4d6","e3e5db":"#e3e5db","e5e4d4":"#e5e4d4","e5e9dc":"#e5e9dc","e5eddd":"#e5eddd","e6d8bd":"#e6d8bd","e6e1c9":"#e6e1c9","e7c6ae":"#e7c6ae","e7dbc2":"#e7dbc2","e8d7ae":"#e8d7ae","e9e5ce":"#e9e5ce","e9e8db":"#e9e8db","eceee4":"#f4f6f1","ede6d6":"#ede6d6","edf1edee":"#edf1edee","eee0b8":"#eee0b8","eee8d4":"#eee8d4","eee9d5":"#eee9d5","eff2df":"#eff2df","f2f1df":"#f2f1df","f3e8d1":"#f3e8d1","f4e9cd":"#f4e9cd","f4f0dd":"#f4f0dd","f4f0df":"#f4f0df","f4f3ed":"#f4f3ed","f9faf3e8":"#f9faf3e8","fff":"#fff","fff3e3":"#fff3e3","fff6d7":"#fff6d7","fff9":"#fff9","fffdf2f0":"#fffdf2f0","fffdf5":"#fffdf5","fffef9":"#fffef9","fffef960":"#fffef960","fffef980":"#fffef980"};
const stateText={working:'작업 중',meeting:'회의 중',resting:'대기 중',question:'질문 있어요',approval:'승인 필요',exhausted:'한도 소진'};
// 방의 높이는 지형 단차다. 건물과 책상은 모두 단층으로 유지한다.
const WORLD_SCALE=1.5, MAP={w:96,d:91.5};
const wide=([x,y,z])=>[x*WORLD_SCALE,y*WORLD_SCALE,z];
const rooms=[
	{id:'secretary',name:'비서팀',x:3,y:3,w:13,d:10,level:1.6,color:'secretary',door:'south',at:10,old:[1,1]},
	{id:'infra',name:'인프라 운영팀',x:23,y:2,w:14,d:10,level:2.4,color:'infra',door:'south',at:32,old:[16,1]},
	{id:'data',name:'데이터관리팀',x:45,y:4,w:13,d:10,level:1.6,color:'data',door:'south',at:51,old:[32,1]},
	{id:'content',name:'콘텐츠팀',x:2,y:21,w:19,d:20,level:.8,color:'content',door:'east',at:31,old:[1,13]},
	{id:'meeting',name:'회의실',x:27,y:24,w:10,d:12,level:1.6,color:'secretary',door:'west',at:30,extraDoors:[{side:'east',at:30}],old:[18,15]},
	{id:'company',name:'회사 프로젝트팀',x:47,y:23,w:13,d:8,level:2.4,color:'company',door:'west',at:27,locked:true,old:[32,13]},
	{id:'sub',name:'서브프로젝트팀',x:47,y:39,w:13,d:8,level:.8,color:'sub',door:'west',at:43,old:[32,23]},
	{id:'reception',name:'접수',x:3,y:46,w:9,d:7,level:.8,color:'reception',door:'north',at:8,old:[1,33]},
	{id:'review',name:'검수실',x:19,y:48,w:8,d:7,level:1.6,color:'review',door:'north',at:23,old:[12,33]},
	{id:'lounge',name:'휴게 공간',x:31,y:49,w:11,d:7,level:.8,color:'lounge',door:'north',at:36,old:[22,33]},
	{id:'owner-room',name:'대표실',x:39,y:36,w:8,d:6,level:1.6,color:'secretary',door:'west',at:41,old:[39,36]},
	{id:'server',name:'서버실',x:48,y:52,w:10,d:7,level:1.6,color:'server',door:'north',at:53,old:[35,33]}
].map(r=>({...r,originalSize:[r.w,r.d],x:r.x*WORLD_SCALE,y:r.y*WORLD_SCALE,w:r.w*WORLD_SCALE,d:r.d*WORLD_SCALE,at:r.at*WORLD_SCALE,extraDoors:r.extraDoors?.map(d=>({...d,at:d.at*WORLD_SCALE}))}));
const roomById=id=>rooms.find(r=>r.id===id), roomFor=p=>rooms.find(r=>r.name===p.dept);
const teams={'접수':'접수','비서팀':'비서팀','인프라 운영팀':'인프라팀','데이터관리팀':'데이터팀','콘텐츠팀':'콘텐츠팀','서브프로젝트팀':'서브팀','회사 프로젝트팀':'회사팀','검수실':'검수실','회의실':'회의실'};
const shortRoles={'비서실장(PM)':'비서실장','이력서·자소서 작성·관리':'이력서·자소서','검색 품질 분석':'검색 분석','편집장(PM)':'편집장','클립(영상·카드뉴스)':'클립','티스토리 잔디로그':'잔디로그','마케팅(SEO·GEO·CRO)':'마케팅','히스토리 검색 담당(로컬 전용)':'히스토리 검색','독립 검수원':'독립 검수'};
const teamRole=p=>p.id==='owner'?'[ 숲속 오피스 대표 ]':'[ '+teams[p.dept]+'/'+(shortRoles[p.role]||p.role)+' ]';
const shifted=(point,r)=>[r.x+(point[0]-r.old[0])*WORLD_SCALE,r.y+(point[1]-r.old[1])*WORLD_SCALE];
// 책상 좌표는 소속 자리다. 회의나 휴식 중에도 자기 책상은 남겨 둔다.
const staff=[
	{id:'dog',name:'보리',species:'강아지',animal:'dog',dept:'접수',role:'접수 담당',home:[3,35],color:'#d5b585'},
	{id:'rabbit',name:'모찌',species:'토끼',animal:'rabbit',dept:'비서팀',role:'비서실장(PM)',home:[2,5],position:[20.4,23.8],color:'#ede6d6'},
	{id:'alpaca',name:'파카',species:'알파카',animal:'alpaca',dept:'비서팀',role:'학습 코치',home:[6,5],color:'#e6d8bd'},
	{id:'hedgehog',name:'또박이',species:'고슴도치',animal:'hedgehog',dept:'비서팀',role:'이력서·자소서 작성·관리',home:[10,5],color:'#bfa482'},
	{id:'penguin',name:'펭돌',species:'펭귄',animal:'penguin',dept:'인프라 운영팀',role:'PM',home:[17,2.3],color:'#485861'},
	{id:'deer',name:'봄비',species:'사슴',animal:'deer',dept:'인프라 운영팀',role:'기획',home:[23,2.3],color:'#c1a27a'},
	{id:'beaver',name:'나무',species:'비버',animal:'beaver',dept:'인프라 운영팀',role:'백엔드',home:[17,6.8],position:[24.4,35.8],color:'#9e8870'},
	{id:'raccoon',name:'구름',species:'너구리',animal:'raccoon',dept:'인프라 운영팀',role:'프론트',home:[23,6.8],color:'#969c89'},
	{id:'badger',name:'소리',species:'오소리',animal:'badger',dept:'데이터관리팀',role:'PM',home:[33,5.4],color:'#899083'},
	{id:'owl',name:'달빛',species:'부엉이',animal:'owl',dept:'데이터관리팀',role:'검색 품질 분석',home:[37,5.4],color:'#a88762'},
	{id:'mole',name:'땅콩',species:'두더지',animal:'mole',dept:'데이터관리팀',role:'볼트 정리',home:[41,5.4],color:'#9e8870'},
	{id:'fox',name:'여우비',species:'여우',animal:'fox',dept:'콘텐츠팀',role:'편집장(PM)',home:[2,14.5],position:[24.4,23.8],color:'#d89056'},
	{id:'meerkat',name:'살피',species:'미어캣',animal:'meerkat',dept:'콘텐츠팀',role:'자료조사',home:[6,14.5],color:'#bfa482'},
	{id:'sparrow',name:'재잘',species:'참새',animal:'sparrow',dept:'콘텐츠팀',role:'네이버 블로그',home:[10,14.5],color:'#a68c6e'},
	{id:'squirrel',name:'도토리',species:'다람쥐',animal:'squirrel',dept:'콘텐츠팀',role:'클립(영상·카드뉴스)',home:[2,19.2],color:'#b88553'},
	{id:'woodpecker',name:'톡톡',species:'딱따구리',animal:'woodpecker',dept:'콘텐츠팀',role:'티스토리 개발',home:[6,19.2],color:'#586762'},
	{id:'duck',name:'잔디',species:'오리',animal:'duck',dept:'콘텐츠팀',role:'티스토리 잔디로그',home:[10,19.2],color:'#d9cda9'},
	{id:'parrot',name:'수다',species:'앵무새',animal:'parrot',dept:'콘텐츠팀',role:'스레드',home:[2,24],color:'#8fa268'},
	{id:'seal',name:'방울',species:'물범',animal:'seal',dept:'콘텐츠팀',role:'유튜브',home:[6,24],position:[29,35.8],color:'#a1a6a0'},
	{id:'wolf',name:'바람',species:'늑대',animal:'wolf',dept:'콘텐츠팀',role:'마케팅(SEO·GEO·CRO)',home:[10,24],color:'#7f8d9f'},
	{id:'koala',name:'코코',species:'코알라',animal:'koala',dept:'서브프로젝트팀',role:'PM',home:[34,25],color:'#a6aaa0'},
	{id:'cat',name:'고등어',species:'고양이',animal:'cat',dept:'서브프로젝트팀',role:'개발',home:[39,25],color:'#a1a6a0'},
	{id:'turtle',name:'세월',species:'거북이',animal:'turtle',dept:'회사 프로젝트팀',role:'히스토리 검색 담당(로컬 전용)',home:[35,16],color:'#91a175'},
	{id:'bear',name:'꼼꼼',species:'곰',animal:'bear',dept:'검수실',role:'독립 검수원',home:[14,35],color:'#94745c'},
	{id:'otter',name:'모아',species:'수달',animal:'otter',dept:'회의실',role:'회의 진행',home:null,position:[22.4,16.5],color:'#9e8870'}
].map(p=>({...p,x:p.position?.[0]??p.home[0]+.55,y:p.position?.[1]??p.home[1]+1.7,seated:true,facing:p.id==='otter'?0:2,status:'unknown'}));
// 직원은 각자의 자리에서 시작한다.
for(const p of staff){
	const r=roomFor(p);if(p.home)p.home=shifted(p.home,r);
	p.workSeat=p.home?[p.home[0]+.55,p.home[1]+1.7,r.level+.36]:[48.4,51.8,1.96];
	[p.x,p.y,p.level]=p.workSeat;
}
// 팀마다 마주 보는 자리와 독립 자리를 섞고, 책상 간격을 확보한다.
function arrangeTeam(dept,columns){
	const group=staff.filter(p=>p.dept===dept),r=rooms.find(room=>room.name===dept);
	group.forEach((p,i)=>{const row=Math.floor(i/columns),col=i%columns;
		const x=columns===1?(row===2?r.x+r.w-5:r.x+2.5):r.x+2+col*(r.w-7)/(columns-1);
		const y=r.y+(row===0?3.2:row===1?4.55:Math.max(9.5,r.d-10));
		p.home=[x,y];p.deskFlip=row===0;p.facing=p.deskFlip?0:2;
		p.workSeat=[x+.55,y+(p.deskFlip?-1.25:1.7),r.level+.36];
		[p.x,p.y,p.level]=p.workSeat;
	});
}
arrangeTeam('비서팀',1);arrangeTeam('인프라 운영팀',2);arrangeTeam('데이터관리팀',1);
arrangeTeam('콘텐츠팀',3);arrangeTeam('서브프로젝트팀',1);
const ownerSeat=[61.55,57.9,1.96], mailboxAt={x:67.5,y:57.1,z:1.96};
staff.unshift({id:'owner',name:'나',species:'노란 고양이',animal:'cat',dept:'숲속 오피스 대표',role:'숲속 오피스 대표',color:'#f0c34d',status:'unknown',x:ownerSeat[0],y:ownerSeat[1],level:ownerSeat[2],workSeat:ownerSeat,seated:true,facing:0});
const engines=[[36,34.7],[39,34.7],[42,34.7],[36,38],[39,38],[42,38]].map(at=>({name:'서버',at}));
// 일반 소품도 배열로 관리한다. 직원 책상과 서버 기계는 위 배열에서 파생한다.
const props=[
	{kind:'board',at:[2,1.8]},{kind:'shelf',at:[9.5,1.5],w:3},{kind:'board',at:[26,1.5]},
	{kind:'shelf',at:[33,1.5],w:4},{kind:'shelf',at:[40,1.5],w:3},
	{kind:'board',at:[7.5,28]},{kind:'camera',at:[2.5,28]},{kind:'lamp',at:[5,28]},
	{kind:'board',at:[18.8,15.5]},{kind:'shelf',at:[40,13.5],w:3},
	{kind:'board',at:[33,23.5]},{kind:'shelf',at:[41,29.6],w:3},
	{kind:'shelf',at:[17,33.5],w:2},{kind:'sofa',at:[23.5,35.2]},{kind:'coffee',at:[30,38]},
	...[[1.7,9.3],[28,9],[43.3,9.2],[12.5,28.3],[26,25],[43,19],[43,29],[1.8,38.5],[22.8,38.5]].map(at=>({kind:'plant',at}))
];
for(const e of engines)e.at=shifted(e.at,roomById('server'));
for(const p of props){const r=rooms.find(r=>p.at[0]>=r.old[0]&&p.at[0]<r.old[0]+r.originalSize[0]&&p.at[1]>=r.old[1]&&p.at[1]<r.old[1]+r.originalSize[1]);if(r){p.at=shifted(p.at,r);if(r.id==='sub'&&p.kind==='shelf')p.at[0]-=3;p.level=r.level}}
let cubes=[],labels=[];
const actorBlocks=new Map(),actorMeshes=new Map();
function raised(height,draw){const start=cubes.length;draw();for(let i=start;i<cubes.length;i++)cubes[i].z+=height}
function shade(hex,n){const v=parseInt(hex.slice(1),16);return '#'+[v>>16,(v>>8)&255,v&255].map(a=>Math.round(Math.max(0,Math.min(255,a*n))).toString(16).padStart(2,'0')).join('')}
// 깊이 계산은 GPU가 맡아 긴 판을 작은 블록으로 나누지 않는다.
function box(x,y,z,w,d,h,color,opts={}){cubes.push({x,y,z,w,d,h,color,...opts})}
function plant(x,y,z=0,size=1){box(x,y,z,.65*size,.65*size,.65*size,P['ae8d69']);box(x-.06*size,y-.06*size,z+.65*size,.78*size,.78*size,.6*size,P['657e4c']);box(x+.12*size,y+.08*size,z+1.17*size,.48*size,.48*size,.43*size,P['8fa268'])}
function chair(x,y,z=0,c=P['697b69'],dir=0){box(x+.13,y+.13,z,.12,.12,.65,P['525b54']);box(x+.73,y+.6,z,.12,.12,.65,P['525b54']);box(x,y,z+.6,.95,.9,.24,c);if(dir===0)box(x,y+.65,z+.85,.95,.22,.8,c);else box(x+.7,y,z+.85,.22,.9,.8,c)}
function desk(x,y,w=2.9,d=1.35,opts={}){const start=cubes.length,z=opts.z||.15;box(x+.13,y+.1,z,.17,d-.2,1.25,P['7f8174']);box(x+w-.3,y+.1,z,.17,d-.2,1.25,P['7f8174']);box(x,y,z+1.25,w,d,.22,opts.color||C.wood);box(x+.6,y+.27,z+1.47,.1,.13,.33,P['535d55']);box(x+.3,y+.16,z+1.75,1.23,.14,.92,P['3d514c']);box(x+.4,y+.305,z+1.86,1.02,.025,.67,P['bfd8c4']);box(x+.43,y+.33,z+1.97,.65,.025,.05,P['749c83']);box(x+.43,y+.33,z+2.11,.8,.025,.04,P['8da994']);box(x+.48,y+.7,z+1.48,1.05,.34,.05,P['e5e4d4']);box(x+1.75,y+.5,z+1.48,.28,.3,.3,P['eee9d5']);box(x+1.7,y+.22,z+1.48,.38,.2,.05,P['f4f0df']);if(!opts.noChair)chair(x+.5,y+1.6,z,P['6f7f6a']);if(opts.flip)for(let i=start;i<cubes.length;i++){const b=cubes[i];b.x=2*x+w-b.x-b.w;b.y=2*y+d-b.y-b.d}}
function shelf(x,y,z=0,w=2.6){box(x,y,z,w,.7,2.9,P['9a815f']);for(let row=0;row<3;row++){box(x+.12,y+.64,z+.18+row*.87,w-.24,.05,.67,P['5f6959']);for(let b=0;b<6;b++)box(x+.19+b*(w-.36)/6,y+.65,z+.23+row*.87,(w-.5)/6-.03,.22,.52,[P['d5d1ad'],P['a6b59a'],P['bab5c4'],P['d8bb93'],P['8b9d8d'],P['dedac5']][b])}}
function server(x,y,z=0,height=1){const start=cubes.length;box(x,y,z,1.18,.9,3.35,P['586762']);box(x+.1,y+.89,z+.12,.98,.05,3.1,P['344d46']);for(let k=0;k<5;k++){box(x+.16,y+.95,z+.32+k*.51,.85,.04,.31,P['6d8179']);box(x+.25,y+1,z+.42+k*.51,.12,.04,.09,k===2?P['d7bd67']:P['abd0a1'])}if(height!==1){for(let i=start;i<cubes.length;i++){cubes[i].z=z+(cubes[i].z-z)*height;cubes[i].h*=height}}}
function board(x,y,z=0){box(x,y,z,.1,.13,1.2,P['7d8474']);box(x+2.7,y,z,.1,.13,1.2,P['7d8474']);box(x-.15,y,z+1.1,3.1,.18,1.75,P['b0b8a5']);box(x-.04,y+.19,z+1.22,2.85,.03,1.5,P['f2f1df']);for(let k=0;k<3;k++){box(x+.2,y+.23,z+2.22-k*.34,1.2+(k%2)*.7,.025,.06,P['9eaf92']);box(x+1.9,y+.24,z+1.48+k*.27,.35,.02,.23,[P['d8b47f'],P['b5c8b0'],P['c4bdcc']][k])}}
function lamp(x,y,z=0){box(x,y,z,.8,.7,.12,P['62685d']);box(x+.35,y+.3,z+.12,.1,.1,3.5,P['7a8175']);box(x-.05,y+.1,z+3.15,.9,.35,.7,P['e7dbc2']);box(x+.05,y+.46,z+3.23,.7,.025,.5,P['fff6d7'])}
function animal(p){const start=cubes.length,x=p.x,y=p.y,z=p.seated ? 1.05 : .4,c=p.color;box(x+.1,y+.1,z,.68,.65,.75,c);box(x+.04,y+.12,z-.22,.3,.52,.25,shade(c,.7));box(x+.48,y+.12,z-.22,.3,.52,.25,shade(c,.7));box(x-.03,y-.04,z+.7,.94,.85,.83,c);if(p.animal==='rabbit'){box(x+.02,y+.15,z+1.5,.24,.26,.78,c);box(x+.6,y+.15,z+1.5,.24,.26,.78,c);box(x+.08,y+.34,z+1.58,.1,.025,.48,P['caaea4']);box(x+.66,y+.34,z+1.58,.1,.025,.48,P['caaea4'])}else if(p.animal==='cat'||p.animal==='fox'){box(x-.03,y+.04,z+1.43,.28,.42,.38,c);box(x+.63,y+.04,z+1.43,.28,.42,.38,c);box(x+.04,y+.38,z+1.48,.12,.08,.17,P['d4a89c']);box(x+.7,y+.38,z+1.48,.12,.08,.17,P['d4a89c'])}else if(p.animal!=='penguin'&&p.animal!=='owl'){box(x-.13,y+.14,z+1.33,.3,.4,.33,c);box(x+.7,y+.14,z+1.33,.3,.4,.33,c)}if(p.animal==='dog'){box(x-.15,y+.3,z+.9,.25,.45,.7,shade(c,.7));box(x+.8,y+.3,z+.9,.25,.45,.7,shade(c,.7))}if(p.animal==='squirrel'){box(x+.65,y-.38,z+.1,.45,.6,1.1,shade(c,.85));box(x+.65,y-.5,z+.65,.4,.6,.6,c)}if(p.animal==='penguin'){box(x+.15,y+.76,z+.08,.58,.08,.7,P['e9e5ce']);box(x+.21,y+.82,z+.99,.45,.2,.17,P['d4a25b'])}else{box(x+.17,y+.77,z+.77,.52,.18,.35,P['e6d8bd']);box(x+.37,y+.96,z+.97,.16,.06,.11,P['52564a'])}box(x+.11,y+.82,z+1.17,.12,.035,.14,P['35423a']);box(x+.65,y+.82,z+1.17,.12,.035,.14,P['35423a']);if(p.animal==='owl'){box(x+.02,y+.8,z+1.04,.32,.055,.42,P['d9cda9']);box(x+.5,y+.8,z+1.04,.32,.055,.42,P['d9cda9']);box(x+.13,y+.87,z+1.17,.11,.02,.15,P['35423a']);box(x+.61,y+.87,z+1.17,.11,.02,.15,P['35423a']);box(x+.37,y+.85,z+.95,.15,.15,.2,P['ce9c56'])}box(x-.13,y+.28,z+.05,.23,.65,.25,c);box(x+.83,y+.28,z+.05,.23,.65,.25,c);if(['deer','wolf'].includes(p.animal)){box(x-.05,y+.08,z+1.5,.24,.3,.47,c);box(x+.65,y+.08,z+1.5,.24,.3,.47,c)}
	if(p.id==='owner'){box(x+.06,y-.15,z+.15,.8,.18,.72,'#82ab85');box(x+.35,y+.8,z+.51,.2,.12,.34,'#719e79')}
	if(p.animal==='deer'){for(const dx of [.04,.65]){box(x+dx,y+.1,z+1.83,.09,.12,.57,P['887b5d']);box(x+dx-.13,y+.1,z+2.1,.3,.12,.09,P['887b5d'])}}
	if(p.animal==='alpaca'){box(x+.16,y+.1,z+.65,.56,.55,.95,c);box(x+.1,y+.1,z+1.5,.2,.22,.55,c);box(x+.61,y+.1,z+1.5,.2,.22,.55,c)}
	if(p.animal==='hedgehog'){for(let k=0;k<4;k++)box(x-.08+k*.27,y-.16,z+.2,.17,.2,1.06,P['6c5040'])}
	if(p.animal==='raccoon'||p.animal==='badger'){box(x+.03,y+.86,z+1.12,.3,.04,.23,P['485861']);box(x+.54,y+.86,z+1.12,.3,.04,.23,P['485861']);box(x+.13,y+.91,z+1.18,.09,.03,.1,P['eee9d5']);box(x+.64,y+.91,z+1.18,.09,.03,.1,P['eee9d5'])}
	if(p.animal==='beaver'){box(x+.62,y-.5,z-.02,.42,.85,.2,P['6c5040']);box(x+.29,y+.98,z+.77,.28,.035,.21,P['eee9d5'])}
	if(p.animal==='mole')box(x+.26,y+.86,z+.95,.36,.3,.22,P['d4a89c']);
	if(['sparrow','woodpecker','duck','parrot'].includes(p.animal)){box(x+.22,y+.89,z+.96,.46,.3,.2,P['d4a25b']);box(x-.16,y+.1,z+.1,.22,.53,.63,shade(c,.8));box(x+.82,y+.1,z+.1,.22,.53,.63,shade(c,.8))}
	if(p.animal==='woodpecker')box(x+.25,y+.08,z+1.54,.34,.36,.29,P['c77950']);
	if(p.animal==='parrot')box(x+.15,y+.1,z+1.46,.55,.48,.22,P['c77950']);
	if(p.animal==='koala'){box(x-.24,y+.07,z+1.11,.4,.5,.5,c);box(x+.76,y+.07,z+1.11,.4,.5,.5,c);box(x+.3,y+.92,z+.99,.27,.1,.36,P['485861'])}
	if(p.animal==='turtle'){box(x-.03,y-.2,z-.02,.96,.47,.91,P['657e4c']);box(x+.17,y-.25,z+.14,.56,.1,.54,P['8fa268'])}
	if(p.animal==='seal'){box(x-.32,y+.28,z-.11,.48,.58,.16,c);box(x+.78,y+.28,z-.11,.48,.58,.16,c)}
	for(let i=start;i<cubes.length;i++){const b=cubes[i],base=z-.22;b.x=x+.45+(b.x-x-.45)*1.45;b.y=y+.45+(b.y-y-.45)*1.45;b.z=base+(b.z-base)*1.32;b.w*=1.45;b.d*=1.45;b.h*=1.32;if(p.id==='owner'){b.x=x+(b.x-x)*1.14;b.y=y+(b.y-y)*1.14;b.z=base+(b.z-base)*1.14;b.w*=1.14;b.d*=1.14;b.h*=1.14}}
	for(let i=start;i<cubes.length;i++)cubes[i].z+=p.level||0;
	if(p.facing===2){for(let i=start;i<cubes.length;i++){const b=cubes[i];b.x=x+.9-(b.x-x)-b.w;b.y=y+.9-(b.y-y)-b.d}}}
function sofa(x,y){box(x,y,.32,6.8,1.8,.45,'#b7cba3');box(x,y,.77,6.8,.35,1.2,'#c4d6ae');for(let i=0;i<5;i++)box(x+.35+i*1.22,y+.4,.78,1.13,1.15,.25,'#d7e5c4');for(const dx of [0,6.4])box(x+dx,y,.75,.4,1.8,.85,'#c4d6ae');box(x+2,y+2.4,.32,2.6,1,.65,'#dfcba7');box(x+2.6,y+2.6,.97,.3,.3,.25,'#f4e9cd')}
function coffee(x,y){box(x,y,.32,2,.9,1.3,'#e1ccaa');box(x,y,1.62,2,.9,.16,'#ded6b7');box(x+.2,y+.1,1.78,.95,.65,1.03,'#aebfb5');box(x+.31,y+.76,2.1,.65,.035,.45,'#b9cbb3');box(x+.51,y+.8,1.92,.23,.2,.25,'#f3e8d1')}
function cameraProp(x,y){box(x,y,.32,.8,.7,.12,'#a5b7ac');box(x+.33,y+.3,.44,.12,.1,1.75,'#aab9ac');box(x,y,2.16,.85,.6,.6,'#9eb3aa');box(x+.26,y+.6,2.28,.4,.3,.32,'#7b9084')}
function doorPoint(r){return r.door==='north'?[r.at,r.y]:r.door==='south'?[r.at,r.y+r.d]:r.door==='west'?[r.x,r.at]:[r.x+r.w,r.at]}
function room(r){
	const start=cubes.length,glass=r.locked,wallHeight=glass?2.5:1.18,cell=1.5;
	const nx=Math.round(r.w/cell),ny=Math.round(r.d/cell);
	const inside=(i,j)=>i>=0&&j>=0&&i<nx&&j<ny&&!((i===0||i===nx-1)&&(j===0||j===ny-1))&&!(['content','sub'].includes(r.id)&&i>=nx-3&&j>=ny-3);
	for(let j=0;j<ny;j++){
		const cells=Array.from({length:nx},(_,i)=>i).filter(i=>inside(i,j));if(!cells.length)continue;
		const lo=cells[0],hi=cells.at(-1)+1;
		box(r.x+lo*cell,r.y+j*cell,.2,(hi-lo)*cell,cell,.15,shade(C[r.color],j%3===0?1.01:.99),r.locked?{blocked:true}:{walkable:'room',roomId:r.id});
		for(const i of cells)for(const [di,dj,side] of [[0,-1,'north'],[0,1,'south'],[-1,0,'west'],[1,0,'east']]){
			if(inside(i+di,j+dj))continue;
			const horizontal=dj!==0,x=r.x+i*cell+(di===1?cell:0),y=r.y+j*cell+(dj===1?cell:0),mid=(horizontal?x:y)+cell/2;
			const opening=side===r.door?r.at:r.extraDoors?.find(d=>d.side===side)?.at;
			if(opening!==undefined&&Math.abs(mid-opening)<1.9)continue;
			const w=horizontal?cell:.16,d=horizontal?.16:cell;
			box(x,y,.35,w,d,wallHeight,glass?'#d9e9e6':C.wall,glass?{alpha:.23}:{});
			box(x-.02,y-.02,.35+wallHeight,w+.04,d+.04,.1,glass?'#839c9c':'#fffdf1');
		}
	}
	const [dx,dy]=doorPoint(r),horizontal=['north','south'].includes(r.door);
	for(const shift of [-1.4,1.25])box(dx+(horizontal?shift:0),dy+(horizontal?0:shift),.35,.25,.25,3.2,glass?'#839c9c':'#e1ccaa');
	box(dx-(horizontal?1.5:0),dy-(horizontal?0:1.5),3.53,horizontal?3.05:.3,horizontal?.3:3.05,.55,C[r.color]);
	box(dx-(horizontal?1.22:0),dy-(horizontal?0:1.22),.35,horizontal?2.48:.22,horizontal?.22:2.48,.05,'#d8bb93');
	if(glass){box(dx,dy-1.18,.39,.12,2.35,2.95,'#bcd5cf',{alpha:.3});box(dx+.1,dy-.25,1.75,.16,.48,.4,'#b49b81');box(dx+.09,dy-.18,2.13,.15,.34,.25,'#c1c8bd')}
	for(let i=start;i<cubes.length;i++)cubes[i].z+=r.level;
	labels.push({text:r.name,x:dx,y:dy,z:4.22+r.level,locked:glass,id:r.id});
}
// 중심 좌표와 발 높이로 저장한 동선. 가구 사이 통로와 건물 밖 다리를 같은 좌표로 사용한다.
const routes={
	secretary:[[10,13,1.96],[10,17,1.96],[23,17,1.96],[23,30,1.96],[27,30,1.96]],
	infra:[[32,12,2.76],[32,17,2.76],[23,17,1.96],[23,30,1.96],[27,30,1.96]],
	data:[[51,14,1.96],[51,18,1.96],[40,18,1.96],[40,30,1.96],[37,30,1.96]],
	content:[[21,31,1.16],[22,31,1.16],[23,31,1.96],[23,30,1.96],[27,30,1.96]],
	company:[[47,27,2.76],[43,27,2.76],[40,27,1.96],[40,30,1.96],[37,30,1.96]],
	sub:[[47,43,1.16],[43,43,1.16],[37.5,43,1.96],[37.5,30,1.96],[37,30,1.96]],
	reception:[[8,46,1.16],[8,42,1.16],[23,42,1.96],[23,30,1.96],[27,30,1.96]],
	review:[[23,48,1.96],[23,42,1.96],[23,30,1.96],[27,30,1.96]],
	lounge:[[36,49,1.16],[36,42,1.16],[23,42,1.96],[23,30,1.96],[27,30,1.96]],
	server:[[53,52,1.96],[53,50,1.96],[44,50,1.96],[37.5,50,1.96],[37.5,30,1.96],[37,30,1.96]],
	owner:[[39,41,1.96],[37.5,41,1.96],[37.5,30,1.96],[37,30,1.96]],
	plaza:[[27,30,1.96],[25.5,30,1.96],[25.5,39,1.96],[37.5,39,1.96],[37.5,30,1.96],[37,30,1.96]]
};
for(const id of Object.keys(routes))routes[id]=routes[id].map(wide);
function pathPoints(route){const out=[route[0]];for(let i=1;i<route.length;i++){const a=route[i-1],b=route[i],n=Math.ceil(Math.hypot(b[0]-a[0],b[1]-a[1])/.55);for(let k=1;k<=n;k++){const t=k/n;out.push([a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t,a[2]+Math.round((b[2]-a[2])*t/.2)*.2])}}return out}
const walks=Object.fromEntries(Object.entries(routes).map(([id,path])=>[id,pathPoints(path)]));
function distanceToPath(x,y){return Math.min(...Object.values(walks).flat().map(p=>Math.hypot(x-p[0],y-p[1])))}
const stream=[{x:40,y:0,w:3,d:18,z:1.05},{x:41,y:18,w:3,d:19,z:.45},{x:42,y:37,w:3,d:24,z:.12}].map(r=>({...r,x:r.x*WORLD_SCALE,y:r.y*WORLD_SCALE,w:r.w*WORLD_SCALE,d:r.d*WORLD_SCALE}));
function inWater(x,y,pad=0){return stream.some(r=>x>=r.x-pad&&x<=r.x+r.w+pad&&y>=r.y-pad&&y<=r.y+r.d+pad)||((x-27)**2/45+(y-13.5)**2/27<1)}
function tree(x,y,z,size,kind){
	box(x-.2,y-.2,z,.55,.55,2.6*size,'#b0936e');
	if(kind===0){for(let k=0;k<3;k++){const w=(2.7-k*.65)*size;box(x-w/2,y-w/2,z+(1.5+k*.8)*size,w,w,.95*size,['#91b77c','#a2c98a','#b1d79b'][k])}}
	else {box(x-1.35*size,y-1.05*size,z+1.8*size,2.7*size,2.1*size,1.6*size,'#9ac185');box(x-.95*size,y-1.4*size,z+2.35*size,1.9*size,2.8*size,1.2*size,'#add194');if(kind===2)box(x-.85*size,y-.8*size,z+3.25*size,1.7*size,1.6*size,.55*size,'#bddda5')}
}
function flower(x,y,z,color){box(x,y,z,.09,.09,.35,'#85a56e');box(x-.15,y-.13,z+.32,.39,.35,.12,color);box(x-.04,y-.03,z+.45,.15,.15,.1,'#f4d780')}
function isLand(x,y){const a=(x-MAP.w/2)/(MAP.w/2),b=(y-MAP.d/2)/(MAP.d/2);return (Math.abs(a)**4+Math.abs(b)**4)<1+.045*Math.sin(y*.3)+.025*Math.sin(x*.37)||rooms.some(r=>x>r.x-1&&x<r.x+r.w+1&&y>r.y-1&&y<r.y+r.d+1)}
function terrace(x,y,w,d,level){
	for(let j=0;j<d;j+=1.5){const inset=Math.min(w*.2,j<1.5||j>d-2?2.2:j<3||j>d-3.5?.7:0);box(x+inset,y+j,0,w-inset*2,Math.min(1.5,d-j),level,'#d4c2a2');box(x+inset,y+j,level-.12,w-inset*2,Math.min(1.5,d-j),.26,'#c7dfa9',{blocked:true})}
}
const meetingSeats={penguin:[42.3,38.4],deer:[47.5,38.4],beaver:[42.3,49.2],fox:[47.5,49.2],otter:[52.5,44]};
function build(){
	cubes=[];labels=[];actorBlocks.clear();
	// 섬 바깥의 작은 숲과 물이 화면 끝까지 이어진다.
	for(let x=-54;x<MAP.w+54;x+=4.8)for(let y=-54;y<MAP.d+54;y+=4.8){
		const shape=Math.sin(x*.065)+Math.cos(y*.075)+Math.sin((x-y)*.09),n=Math.abs(Math.sin(x*12.9898+y*78.233)*43758.5453)%1;
		if(shape<.25||isLand(x+2.4,y+2.4)||isLand(x,y)||isLand(x+4.8,y+4.8))continue;
		box(x,y,-1.6,4.85,4.85,.65,'#b6d7a6');
		if(n>.05)tree(x+.8+n*2.8,y+.8+(n*7%1)*2.8,-.95,.95+n*.4,Math.floor(n*3));
		if(n>.7)tree(x+3.6,y+1.1,-.95,.72+n*.2,Math.floor(n*3));
	}
	// 행별로 블록을 이어 물결치는 섬의 윤곽을 한 번만 만든다.
	for(let y=0;y<MAP.d;y+=1.5){const cells=[];for(let x=0;x<MAP.w;x+=1.5)if(isLand(x+.75,y+.75))cells.push(x);if(!cells.length)continue;const x=cells[0],w=cells.at(-1)+1.5-x;
		box(x,y,-2.5,w,1.5,1.1,'#bdc8bf');box(x,y,-1.4,w,1.5,1.4,'#c9b296');box(x,y,0,w,1.5,.18,'#dce9c8',{blocked:true});
		for(const edge of [x,x+w-1.5])box(edge,y,-1.3,1.5,1.5,.8,['#c5ae91','#d5c1a4','#bba48b'][Math.round(y/1.5)%3]);
	}
	for(const r of rooms)terrace(r.x-1.5,r.y-1.5,r.w+3,r.d+3,r.level);
	terrace(51,57,6,7.5,1.6);
	for(let x=2;x<MAP.w-2;x+=4.8)for(let y=2;y<MAP.d-2;y+=4.8){if(!isLand(x,y)||rooms.some(r=>x>r.x-2.5&&x<r.x+r.w+2.5&&y>r.y-2.5&&y<r.y+r.d+2.5)||(x>52&&x<66&&y>53&&y<64)||inWater(x,y,2)||distanceToPath(x,y)<2.4)continue;const seed=Math.round(x*13+y*7),level=[.8,1.6,2.4][seed%3];terrace(x-1.7,y-1.7,3.4,3.4,level);tree(x,y,level,.9+(seed%6)*.13,seed%3)}
	for(const r of stream)for(let y=r.y;y<r.y+r.d;y+=.75){
		const x=r.x+Math.sin(y*.13)*.55,w=r.w+Math.sin(y*.19)*.55;
		for(const edge of [x-.7,x+w]){box(edge,y,.18,.7,.75,r.z,'#cfceb0');box(edge,y,r.z+.08,.7,.75,.2,'#c6dda6')}
		box(x-.4,y,.18,w+.8,.75,.12,'#bad4c1');box(x,y,r.z,w,.75,.16,'#a6d9dd',{blocked:true});if(Math.round(y/.75)%3===0)box(x+.35,y,r.z+.17,w*.55,.13,.025,'#def4ed');
	}
	for(const [x,y,z,w,drop] of [[61.5,27,1.2,3,.7],[63,55.5,.6,3,.5],[63,90,.2,4.5,3.1]]){box(x,y,z-drop,w,.22,drop,'#a4d9de');for(let k=0;k<5;k++)box(x+k*w/5,y+.24,z-drop,.18,.045,drop,'#dcf3ed');box(x-.35,y+.3,z-drop-.05,w+.7,.7,.16,'#e3f3e9')}
	for(let x=22.5;x<33;x++)for(let y=10.5;y<18;y++)if((x-27)**2/29.25+(y-13.5)**2/13.5<1)box(x,y,.3,1,1,.15,'#abdce0',{blocked:true});
	// 공통 구간은 한 번만 만들고, 경사 구간의 발판 높이를 이동 좌표와 일치시킨다.
	const planks=new Set();for(const [id,path] of Object.entries(walks))for(let i=0;i<path.length;i++){const p=path[i],next=path[Math.min(i+1,path.length-1)],prev=path[Math.max(0,i-1)],horizontal=Math.abs(next[0]-prev[0])>Math.abs(next[1]-prev[1]),key=p.map(v=>v.toFixed(1)).join(',');if(planks.has(key))continue;planks.add(key);const stone=['reception','review'].includes(id)&&!inWater(p[0],p[1],1.5);if(stone)box(p[0]-(horizontal?.31:1.1),p[1]-(horizontal?1.1:.31),.18,horizontal?.62:2.2,horizontal?2.2:.62,p[2]-.18,'#d1c5ab');box(p[0]-(horizontal?.31:1.1),p[1]-(horizontal?1.1:.31),p[2]-.22,horizontal?.62:2.2,horizontal?2.2:.62,.22,stone?(i%4===0?'#c1cbb9':'#d4ddc9'):i%4===0?'#dfc59c':'#e7d1ad',{walkable:'path'});if(!stone&&i%4===0){for(const side of [-1,1]){const x=p[0]+(horizontal?0:side*1.02),y=p[1]+(horizontal?side*1.02:0);box(x-.06,y-.06,p[2],.12,.12,.75,'#bda17b');box(x-(horizontal?.32:.07),y-(horizontal?.07:.32),p[2]+.68,horizontal?.64:.14,horizontal?.14:.64,.12,'#d1b78d')}}}
	rooms.forEach(room);
	staff.filter(p=>p.home).forEach(p=>{const z=roomFor(p).level+.36;desk(...p.home,2.8,1.35,{z,flip:p.deskFlip});if(p.deskFlip)box(p.home[0]-.05,p.home[1]+1.28,z,2.9,.13,1.6,'#b9cbb4')});
	const m=roomById('meeting');raised(m.level,()=>{
		box(44.8,42.3,.36,.35,4.7,1.1,'#c5b896');box(49.7,42.3,.36,.35,4.7,1.1,'#c5b896');box(44.5,42,1.46,5.9,5.3,.26,'#dfc89e');
		for(const [x,y] of Object.values(meetingSeats))chair(x,y,.36,'#c6d6b3');
		for(const [x,y] of [[45,42.5],[48.6,42.5],[45,45.8],[48.6,45.8]])box(x,y,1.73,1.1,.68,.06,'#eee8d4');
		desk(48,49.9,2.8,1.35,{z:.36});
	});
	raised(1.6,()=>{desk(61,56.2,2.8,1.35,{z:.36});shelf(60,54.6,.36,3.2);plant(65.7,60.7,.36,.9);
		box(64.1,54.05,1.65,2.2,.18,1.65,'#d3b68d');box(64.27,54.25,1.81,1.86,.07,1.3,'#c7e1de');box(65.13,54.32,1.81,.12,.05,1.3,'#fff4dc');box(64.27,54.32,2.41,1.86,.05,.1,'#fff4dc');
	});
	// 둥근 지붕은 폭이 줄어드는 블록으로 쌓고, 깃발과 편지는 따로 움직인다.
	const {x:mx,y:my,z:mz}=mailboxAt;
	box(mx+.7,my+.45,mz,.28,.3,1.3,'#a98a68');box(mx+.4,my+.2,mz,1,.9,.18,'#c8b490');box(mx,my,mz+1.25,1.75,1.3,1.2,'#bf5544');
	for(let i=0;i<5;i++){const inset=i*.14;box(mx+inset,my,mz+2.45+i*.13,1.75-inset*2,1.3,.14,'#ce6650')}
	box(mx+.2,my+1.3,mz+2.04,1.25,.05,.18,'#673e35');box(mx+.2,my+1.36,mz+1.98,1.25,.13,.07,'#e4a07f');box(mx+.3,my+1.32,mz+1.5,1.1,.05,.06,'#e8b29b');
	labels.push({id:'owner-plaza',text:'대표 광장',x:53.5,y:63.5,z:2.8});
	for(const p of props){const [x,y]=p.at;raised(p.level||0,()=>{if(p.kind==='shelf')shelf(x,y,.36,p.w);else if(p.kind==='board')board(x,y,.36);else if(p.kind==='plant')plant(x,y,.36,.8);else if(p.kind==='lamp')lamp(x,y,.36);else if(p.kind==='sofa')sofa(x,y);else if(p.kind==='coffee')coffee(x,y);else if(p.kind==='camera')cameraProp(x,y)})}
	for(const e of engines){const [x,y]=e.at;raised(roomById('server').level,()=>{server(x,y,.36,.78);box(x-.05,y+1.12,1.03,1.35,.07,.16,'#d6dacb')})}
	// 작은 식물은 보행로와 건물을 피하고 빈 땅을 채운다.
	for(let x=1.5;x<MAP.w-1;x+=3.1)for(let y=1.5;y<MAP.d-1;y+=3.2){if(!isLand(x,y)||(x>52&&x<66&&y>53&&y<64)||rooms.some(r=>x>r.x-1.7&&x<r.x+r.w+1.7&&y>r.y-1.7&&y<r.y+r.d+1.7)||inWater(x,y,1)||distanceToPath(x,y)<1.8)continue;const seed=Math.round(x*17+y*31);if(seed%5===0){box(x,y,.18,.95,.8,.65,'#b0cd93');box(x+.2,y+.1,.8,.5,.5,.3,'#c5dea8')}else if(seed%4===0){box(x,y,.18,.8,.6,.38,'#c6cfbc')}else {for(let k=0;k<3;k++)flower(x+k*.34,y+(k%2)*.3,.18,['#dc8e83','#f0ce79','#e9b1c0'][seed%3]);box(x-.3,y+.6,.18,.12,.5,.35,'#b7d29b')}}
}
export { MAP, rooms, staff, engines, stream, cubes, labels, actorBlocks, actorMeshes, mailboxAt, stateText, roomById, teamRole, box, animal, build };
