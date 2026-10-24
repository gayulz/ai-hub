function createPixelArt(scene) {
	'use strict';
	const width=scene.width||1200,height=scene.height||760,background=document.createElement('canvas');
	background.width=width;background.height=height;
	const ctx=background.getContext('2d'),p=scene.palette,night=scene.ground==='night';
	ctx.imageSmoothingEnabled=false;
	const mix=(a,b,t)=>{const u=parseInt(a.slice(1),16),v=parseInt(b.slice(1),16);return '#'+[16,8,0].map(s=>Math.round(((u>>s)&255)*(1-t)+((v>>s)&255)*t).toString(16).padStart(2,'0')).join('');};
	const light=(c,t=.2)=>mix(c,'#fff7dc',t),dark=(c,t=.2)=>mix(c,'#172a2b',t);
	const R=(x,y,w,h,c)=>{ctx.fillStyle=c;ctx.fillRect(Math.round(x),Math.round(y),Math.round(w),Math.round(h));};
	const border=(x,y,w,h,c,edge=dark(c,.35),n=2)=>{R(x,y,w,h,edge);R(x+n,y+n,w-n*2,h-n*2,c);};
	const shadow=(x,y,w,h)=>{R(x+4,y+3,w-8,h,'#172d2920');R(x,y+6,w,h-6,'#172d2916');};
	let seed=57;const rand=()=>{seed=(seed*1664525+1013904223)>>>0;return seed/4294967296;};
	function grain(x,y,w,h,base,kind='wood') {
		R(x,y,w,h,base);ctx.save();ctx.beginPath();ctx.rect(x,y,w,h);ctx.clip();
		if(kind==='wood'||kind==='deck')for(let yy=y;yy<y+h;yy+=20){R(x,yy,w,1,dark(base,.16));for(let xx=x-50+((yy-y)/20%2)*40;xx<x+w;xx+=80){R(xx,yy,1,20,dark(base,.16));R(xx+12,yy+7,24,1,light(base,.09));R(xx+42,yy+14,17,1,dark(base,.07));if(kind==='deck'){R(xx+4,yy+4,2,2,dark(base,.24));R(xx+71,yy+15,2,2,dark(base,.24));}}}
		else if(kind==='tile'||kind==='stone')for(let yy=y;yy<y+h;yy+=32)for(let xx=x;xx<x+w;xx+=32){R(xx,yy,31,31,((xx+yy)/32)%3<1?light(base,.05):base);R(xx,yy,32,1,dark(base,.12));R(xx,yy,1,32,dark(base,.12));R(xx+3,yy+3,25,1,light(base,.11));}
		else for(let i=0;i<w*h/60;i++){const xx=x+Math.floor(rand()*w/2)*2,yy=y+Math.floor(rand()*h/2)*2;R(xx,yy,2,2,i%2?light(base,.12):dark(base,.10));}
		ctx.restore();
	}
	if(scene.id==='observatory'){R(0,0,width,height,p.grass);for(const [x,y] of [[-40,20],[1330,0],[-35,800],[1300,790]]){R(x+30,y+26,230,62,'#ecf6f9');R(x+60,y+5,150,103,'#f9fdff');R(x,y+48,290,33,'#f9fdff');R(x+28,y+83,230,14,'#e7f2f7');}}else grain(0,0,width,height,p.grass,scene.ground==='tile'?'tile':'grass');
	if(scene.ground!=='tile')for(let i=0;i<640;i++){const x=Math.floor(rand()*width/4)*4,y=Math.floor(rand()*height/4)*4,c=i%3?p.grassAlt:dark(p.grass,.10);R(x,y,2,4,c);R(x+3,y-2,2,6,c);R(x+6,y+1,2,3,c);}
	(scene.water||[]).forEach(w=>{
		const {x,y,w:ww,h}=w;R(x+12,y-4,ww-24,h+8,dark(p.water,.19));R(x-4,y+12,ww+8,h-24,dark(p.water,.19));R(x+8,y,ww-16,h,p.water);R(x,y+8,ww,h-16,p.water);
		for(let yy=y+12;yy<y+h-6;yy+=16)for(let xx=x+8;xx<x+ww-8;xx+=28){const k=rand();R(xx+(yy%32?8:0),yy,8+k*9,2,k>.65?p.waterLight:light(p.water,.10));if(k>.8)R(xx+4,yy+3,6,2,light(p.water,.19));}
		R(x+12,y+4,ww-24,2,light(p.water,.32));R(x+4,y+14,2,h-28,light(p.water,.23));R(x+12,y+h-4,ww-24,2,dark(p.water,.13));
		for(let i=0;i<Math.min(6,ww/55);i++){const xx=x+15+i*48,yy=y+h-11-(i%2)*7;R(xx,yy,14,5,mix(p.treeLight,p.water,.2));R(xx+3,yy-2,8,9,mix(p.treeLight,p.water,.2));R(xx+9,yy-2,3,4,p.water);}
	});
	(scene.paths||[]).forEach(q=>{shadow(q.x,q.y,q.w,q.h);grain(q.x,q.y,q.w,q.h,q.type==='wood'?p.wood:p.path,q.type==='wood'?'deck':q.type||'stone');if(q.type==='wood'){R(q.x,q.y,q.w,3,light(p.wood,.25));R(q.x,q.y+q.h-3,q.w,3,dark(p.wood,.25));}});
	function windowPane(x,y,w=48,h=16){border(x,y,w,h,dark(p.wallDark,.1));R(x+3,y+2,w-6,h-5,night?'#dbbf7d':mix(p.waterLight,'#edf5dc',.45));R(x+4,y+3,w/3,3,night?'#f6deb0':'#f4faf0');R(x+w/2-1,y,2,h,p.wall);R(x-3,y+h,w+6,3,light(p.wall,.12));}
	(scene.rooms||[]).forEach(q=>{
		const {x,y,w,h}=q,c=q.tint||p.wood;shadow(x+2,y+4,w,h);border(x,y,w,h,dark(c,.15));grain(x+4,y+4,w-8,h-8,q.floor==='grass'?dark(p.grass,.03):q.tint||light(p.wood,.1),q.floor);
		if(q.style==='open'){R(x,y,w,3,light(c,.18));R(x,y+h-3,w,3,dark(c,.2));return;}
		if(q.style==='tent'){
			R(x,y,w,8,dark(c,.29));R(x+8,y+8,w-16,20,c);R(x+16,y+8,w-32,5,light(c,.26));
			for(let i=0;i<w-24;i+=26){R(x+12+i,y+13,12,4,light(c,.16));R(x+10+i,y+25,16,4,dark(c,.12));}
			R(x,y+8,12,h-8,dark(c,.15));R(x+w-12,y+8,12,h-8,dark(c,.27));R(x+3,y+12,4,h-20,light(c,.22));R(x+w-9,y+12,4,h-20,c);
			for(const xx of [x+16,x+w-20]){R(xx,y+30,4,20,dark(c,.30));R(xx-3,y+39,10,4,light(c,.30));}return;
		}
		const wall=q.style==='glass'?mix(p.wall,p.waterLight,.40):p.wall;
		R(x,y,w,24,dark(wall,.24));R(x+3,y+2,w-6,6,light(wall,.17));R(x+3,y+8,w-6,13,wall);R(x,y+24,10,h-24,dark(wall,.13));R(x+w-10,y+24,10,h-24,dark(wall,.28));R(x+2,y+24,3,h-26,light(wall,.18));
		if(q.style==='brick')for(let yy=y+10;yy<y+23;yy+=7){R(x+4,yy,w-8,1,dark(wall,.15));for(let xx=x+14+(yy%2)*12;xx<x+w-5;xx+=26)R(xx,yy-6,1,6,dark(wall,.18));}
		else for(let xx=x+30;xx<x+w-55;xx+=80)windowPane(xx,y+8,Math.min(50,w-60),13);
		if(q.style==='glass')for(let yy=y+34;yy<y+h-18;yy+=35){R(x+2,yy,6,26,mix(p.waterLight,wall,.2));R(x+w-8,yy,6,26,mix(p.waterLight,wall,.2));R(x+3,yy+3,2,10,'#edf5e2');}
		R(x,y+h-10,w/2-32,10,dark(wall,.2));R(x+w/2+32,y+h-10,w/2-32,10,dark(wall,.2));R(x+3,y+h-10,w/2-35,3,light(wall,.1));R(x+w/2+32,y+h-10,w/2-35,3,light(wall,.1));R(x+w/2-32,y+h-4,64,4,light(p.wood,.22));
	});
	function glow(x,y,size=58,color='#f2c57d'){for(let i=4;i>0;i--){ctx.globalAlpha=.035;R(x-size*i/4,y-size*i/6,size*i/2,size*i/3,color);}ctx.globalAlpha=1;}
	function chair(x,y,c){border(x,y,24,20,dark(c,.2));R(x+2,y+2,20,12,c);R(x+4,y+3,16,3,light(c,.25));R(x+3,y+17,18,8,light(c,.08));R(x+10,y+25,4,5,dark(c,.4));R(x+3,y+29,18,2,dark(c,.4));}
	function monitor(x,y,c){border(x,y,34,23,'#354a49','#263836');R(x+3,y+3,28,16,c);R(x+5,y+5,21,2,light(c,.55));R(x+5,y+10,12,2,light(c,.35));R(x+5,y+14,18,1,light(c,.32));R(x+15,y+23,4,5,'#405956');R(x+10,y+27,14,2,'#405956');}
	function mug(x,y,c='#faf1d5'){R(x,y,7,8,c);R(x+2,y,4,2,'#695a48');R(x+7,y+2,3,4,c);R(x+8,y+3,1,2,p.wood);}
	function keyboard(x,y){border(x,y,30,7,p.paper,p.wallDark,1);for(let i=0;i<6;i++)R(x+3+i*4,y+2,2,2,p.muted);}
	function pot(x,y,c=p.treeLight){R(x+7,y+24,16,3,dark(p.wood,.28));R(x+8,y+27,14,11,'#b38364');R(x+10,y+28,4,8,'#d6ae86');R(x+5,y+22,20,5,'#d2a179');R(x+13,y+8,3,16,p.tree);R(x+5,y+9,10,8,c);R(x+2,y+5,8,7,light(c,.17));R(x+14,y+4,10,9,light(c,.1));R(x+16,y,7,7,c);R(x+12,y+14,13,6,p.tree);R(x+5,y+16,9,5,light(c,.08));}
	function tree(x,y,pine=false){
		const c=p.tree,l=p.treeLight;shadow(x+5,y+66,52,13);R(x+28,y+46,10,pine?42:34,dark(p.wood,.4));R(x+30,y+49,4,pine?37:28,p.wood);R(x+20,y+74,24,5,dark(p.wood,.4));
		if(pine){for(let i=0;i<4;i++){const yy=y+i*15,ww=20+i*12;R(x+28-ww/2,yy+8,ww,17,c);R(x+24-ww/2,yy+18,ww+8,8,dark(c,.1));R(x+29-ww/2,yy+8,ww/2,7,l);R(x+32,yy+15,ww/3,7,mix(c,l,.35));}R(x+25,y,6,9,l);return;}
		R(x+8,y+9,48,45,dark(c,.08));R(x+2,y+20,60,28,c);R(x+14,y+2,36,60,c);R(x+21,y-2,22,12,c);R(x+8,y+14,24,25,l);R(x+17,y+4,22,20,light(l,.10));R(x+29,y+14,22,24,mix(c,l,.72));R(x+3,y+28,22,16,mix(c,l,.48));R(x+18,y+29,23,20,mix(c,l,.8));R(x+37,y+37,21,15,mix(c,l,.2));R(x+11,y+47,26,12,dark(c,.06));R(x+12,y+18,5,3,light(l,.26));R(x+26,y+8,6,3,light(l,.23));R(x+39,y+24,6,3,light(l,.13));
	}
	function rug(o){const {x,y}=o,w=o.w||90,h=o.h||60,c=o.color||p.treeLight;border(x,y,w,h,'#dfd5b5',dark(c,.2),2);R(x+4,y+4,w-8,h-8,c);R(x+8,y+8,w-16,h-16,light(c,.15));for(let i=12;i<w-12;i+=12){R(x+i,y+6,4,3,dark(c,.15));R(x+i,y+h-9,4,3,dark(c,.15));}for(let i=8;i<h-3;i+=8){R(x-3,y+i,3,2,'#e6dabd');R(x+w,y+i,3,2,'#e6dabd');}}
	const drawers=(x,y,w,h,c)=>{border(x,y,w,h,c);R(x+3,y+5,w-6,1,light(c,.27));R(x+w/2-4,y+h/2,8,2,dark(c,.4));};
	function prop(o){
		const x=o.x,y=o.y,c=o.color||p.treeLight,w=o.w||80,h=o.h||48;
		switch(o.type){
			case 'rug':rug(o);break;
			case 'bamboo':for(const [dx,dy] of [[9,7],[21,0],[31,15]]){R(x+dx,y+dy,5,96-dy,'#648652');R(x+dx+1,y+dy,2,92-dy,'#a0b978');for(let yy=dy+15;yy<90;yy+=19){R(x+dx-1,y+yy,7,2,'#435e42');R(x+dx+1,y+yy-2,2,1,'#c2cf96');}for(let yy=dy+13;yy<70;yy+=24){R(x+dx-7,y+yy,9,3,p.tree);R(x+dx-9,y+yy-3,6,3,p.treeLight);R(x+dx+3,y+yy+4,7,3,p.tree);R(x+dx+6,y+yy+1,4,3,p.treeLight);}}break;
			case 'tree':tree(x,y);break;case 'pine':tree(x,y,true);break;
			case 'plant':shadow(x+3,y+29,24,8);pot(x,y,c);break;
			case 'chair':shadow(x,y+22,24,8);chair(x,y,c);break;
			case 'deskseat':
			case 'desk':
				shadow(x,y+29,64,13);R(x+3,y+29,5,12,dark(p.wood,.4));R(x+56,y+29,5,12,dark(p.wood,.4));border(x,y,64,33,p.wood);R(x+2,y+2,60,4,light(p.wood,.27));R(x+3,y+31,58,5,dark(p.wood,.25));monitor(x+8,y-16,mix(c,p.water,.35));border(x+11,y+18,30,7,'#e8e4cf','#858c7c',1);for(let i=0;i<6;i++)R(x+14+i*4,y+20,2,2,'#a1ab98');R(x+44,y+19,5,7,'#f4eed8');mug(x+51,y+6);chair(x+20,y+37,c);break;
			case 'cubicle': {
				const up=o.direction!=='down',cx=x+w/2,ty=y+(up?20:35),cy=up?y+h-34:y+10,py=up?y+3:y+h-18;
				const panel=()=>{border(x,py,w,14,p.wallDark);R(x+3,py+3,w-6,7,light(c,.12));};
				shadow(x+3,y+h-15,w-6,12);chair(cx-12,cy,c);
				// 뒷모습 자리는 칸막이를 먼저 그리고 모니터를 칸막이 앞 높이에 둔다(머리 위 이름표가 모니터를 가리지 않게)
				if(up)panel();
				border(x+3,ty,w-6,43,p.wood);R(x+5,ty+2,w-10,4,light(p.wood,.27));
				monitor(cx-17,up?y-1:ty+11,mix(c,p.water,.35));keyboard(cx-15,ty+(up?27:2));mug(x+w-21,ty+14);
				if(!up)panel();
				R(x,up?py:ty,4,up?ty+43-py:py+14-ty,p.wallDark);R(x+w-4,up?py:ty,4,up?ty+43-py:py+14-ty,dark(p.wallDark,.15));break;
			}
			case 'longtable':
				(scene.props||[]).filter(q=>q.type==='chair'&&(o.seats||[]).includes(q.seat)).forEach(prop);
				shadow(x,y+h-6,w,14);R(x+7,y+h-3,6,10,dark(p.wood,.4));R(x+w-13,y+h-3,6,10,dark(p.wood,.4));
				border(x,y,w,h,p.wood);grain(x+3,y+3,w-6,h-8,light(p.wood,.12));R(x+3,y+h-5,w-6,3,dark(p.wood,.2));
				border(x+w/2-12,y+h/2-9,24,18,p.paper,p.wallDark,1);R(x+w/2-8,y+h/2-3,16,2,p.muted);mug(x+20,y+15);mug(x+w-25,y+h-24);break;
			case 'workbench': {
				const count=o.monitors||3;
				shadow(x,y+h-10,w,12);border(x,y,w,9,p.wallDark);border(x,y+24,w,h-28,p.wood);R(x+3,y+26,w-6,4,light(p.wood,.27));
				for(let i=0;i<count;i++){const cx=x+(i+.5)*w/count;monitor(cx-17,y+8,mix(c,p.water,.35));keyboard(cx-15,y+47);}
				R(x+6,y+h-4,6,9,dark(p.wood,.4));R(x+w-12,y+h-4,6,9,dark(p.wood,.4));mug(x+w-18,y+33);break;
			}
			case 'bossdesk':
				chair(x+w/2-12,y-25,c);shadow(x,y+h-7,w,14);border(x,y,w,h,p.wood);R(x+3,y+3,w-6,5,light(p.wood,.27));
				monitor(x+14,y+14,p.water);monitor(x+54,y+14,p.waterLight);keyboard(x+36,y+4);
				border(x+w-39,y+11,27,22,p.paper,p.wallDark,1);for(let i=0;i<3;i++)R(x+w-35,y+16+i*5,18,1,p.muted);
				R(x+8,y+h-4,7,10,dark(p.wood,.4));R(x+w-15,y+h-4,7,10,dark(p.wood,.4));mug(x+w-23,y+h-16);break;
			case 'table':
				shadow(x,y+h-7,w,14);R(x+6,y+h-4,5,10,dark(p.wood,.4));R(x+w-11,y+h-4,5,10,dark(p.wood,.4));border(x,y,w,h,p.wood);R(x+3,y+3,w-6,h-9,light(p.wood,.22));R(x+3,y+h-6,w-6,3,dark(p.wood,.11));border(x+w*.35,y+12,19,15,'#eee8d2','#b6b394',1);R(x+w*.35+3,y+16,12,1,'#aab59b');R(x+w*.35+3,y+20,8,1,'#aab59b');mug(x+w-19,y+9);R(x+11,y+h-16,12,7,c);break;
			case 'sofa':
				shadow(x,y+26,76,14);R(x+5,y+34,5,5,dark(p.wood,.4));R(x+65,y+34,5,5,dark(p.wood,.4));border(x,y,76,33,dark(c,.13));R(x+4,y+3,68,15,light(c,.16));R(x+5,y+5,66,3,light(c,.27));R(x+8,y+19,29,14,c);R(x+39,y+19,29,14,c);R(x,y+14,8,22,dark(c,.18));R(x+68,y+14,8,22,dark(c,.18));border(x+11,y+9,13,13,'#d6ba85',dark(c,.2),1);border(x+52,y+10,13,12,light(c,.43),dark(c,.2),1);break;
			case 'bookshelf':
				shadow(x,y+30,64,11);border(x,y,64,38,dark(p.wood,.06));R(x+4,y+3,56,31,dark(p.wood,.3));for(let row=0;row<2;row++){for(let j=0;j<8;j++){const hh=8+(j%3)*2,cc=['#7e9e7b','#c59b68','#749b9c','#ba8072','#b6ae85'][j%5];R(x+6+j*7,y+15+row*17-hh,5,hh,cc);R(x+7+j*7,y+18+row*17-hh,3,1,light(cc,.4));}R(x+2,y+16+row*17,60,3,p.wood);}R(x+2,y,60,3,light(p.wood,.28));break;
			case 'counter':
				shadow(x,y+34,o.w||96,12);border(x,y,o.w||96,o.h||44,dark(p.wood,.10));for(let i=4;i<(o.w||96)-20;i+=30)drawers(x+i,y+15,27,24,p.wood);R(x-2,y-3,(o.w||96)+4,17,light(p.wood,.36));R(x,y+12,o.w||96,3,dark(p.wood,.16));mug(x+14,y+1);R(x+38,y,18,8,'#e4dec6');R(x+42,y+2,10,2,'#9aaf97');break;
			case 'flower':
				for(const [dx,dy] of [[5,11],[16,15],[11,4]]){R(x+dx,y+dy,2,10,p.tree);R(x+dx-3,y+dy+5,4,3,p.treeLight);R(x+dx-3,y+dy-2,8,3,c);R(x+dx,y+dy-5,3,9,c);R(x+dx,y+dy-1,2,2,'#f2dc83');}break;
			case 'server':
				shadow(x,y+40,30,9);border(x,y,30,48,'#4c6260','#293e3e');R(x+3,y+3,24,3,'#75857b');for(let i=0;i<4;i++){border(x+4,y+9+i*9,22,7,'#2d4545','#263c3c',1);R(x+7,y+11+i*9,9,1,'#718e81');R(x+21,y+11+i*9,2,2,i===2?'#d9ba73':'#b1d59a');}break;
			case 'lamp':
				if(night)glow(x+10,y+20,66);R(x+8,y+11,3,32,dark(p.wallDark,.3));R(x+2,y+42,16,3,dark(p.wallDark,.25));R(x+3,y+5,14,13,'#dfc78e');R(x+6,y+1,8,4,'#eee0b2');R(x+1,y+14,18,4,light(p.wall,.3));R(x+5,y+5,5,8,'#fff1be');break;
			case 'meetingboard': {
				shadow(x+2,y+70,140,10);R(x+12,y+61,6,18,p.wallDark);R(x+126,y+61,6,18,p.wallDark);
				R(x+7,y+77,16,3,dark(p.wallDark,.15));R(x+121,y+77,16,3,dark(p.wallDark,.15));
				border(x,y,144,68,'#f1edd9',dark(p.wood,.35),4);R(x+4,y+4,136,14,light(p.wood,.36));R(x+6,y+17,132,1,dark(p.wood,.08));
				const glyph={M:['10001','11011','10101','10001','10001'],E:['111','100','110','100','111'],T:['111','010','010','010','010'],I:['111','010','010','010','111'],N:['1001','1101','1011','1001','1001'],G:['111','100','101','101','111']};
				let gx=x+10;for(const letter of 'MEETING'){glyph[letter].forEach((row,j)=>[...row].forEach((v,i)=>{if(v==='1')R(gx+i,y+8+j,1,1,dark(p.tree,.15));}));gx+=glyph[letter][0].length+2;}
				R(x+128,y+8,4,4,'#ad8260');R(x+130,y+8,1,1,'#eadbbb');
				const notes=[[10,23,27,21,'#e5cc8c'],[42,25,25,19,'#adc9ad'],[75,23,25,21,'#a3c6cc']];
				notes.forEach(([dx,dy,nw,nh,nc],i)=>{R(x+dx+1,y+dy+1,nw,nh,'#777e6c26');R(x+dx,y+dy,nw,nh,nc);R(x+dx+nw-5,y+dy+nh-4,5,4,light(nc,.3));R(x+dx+10,y+dy-2,7,3,'#ddd6b8');R(x+dx+5,y+dy+6,nw-10,2,dark(nc,.32));R(x+dx+5,y+dy+11,nw-13,1,dark(nc,.27));R(x+dx+5,y+dy+14,nw-9,1,dark(nc,.22));});
				border(x+106,y+24,25,17,'#faf7e7','#8ba695',1);R(x+111,y+40,5,4,'#8ba695');R(x+112,y+40,3,2,'#faf7e7');
				for(let i=0;i<3;i++)R(x+111+i*6,y+31,2,2,'#628576');
				R(x+11,y+51,7,7,'#88a58b');R(x+13,y+53,2,3,'#f8f4db');R(x+15,y+51,2,3,'#f8f4db');
				R(x+24,y+51,56,2,'#9da990');R(x+24,y+56,44,1,'#b4bea6');R(x+91,y+51,37,1,'#b4bea6');R(x+91,y+55,28,1,'#b4bea6');
				R(x+3,y+64,138,5,p.wood);R(x+91,y+63,11,3,'#668873');R(x+107,y+63,8,3,'#c59272');R(x+123,y+62,10,4,'#b9baa5');break;
			}
			case 'board':
				R(x+6,y+32,4,12,p.wallDark);R(x+40,y+32,4,12,p.wallDark);border(x,y,50,35,'#e9e6d2',p.wood,3);for(let i=0;i<3;i++){R(x+7+i*13,y+9,9,9,['#d5b887','#a7bf9c','#91b6b4'][i]);R(x+8+i*13,y+12,6,1,'#6e8276');}R(x+7,y+24,30,2,'#b3bfaa');R(x+7,y+28,20,2,'#b3bfaa');break;
			case 'bench':
				shadow(x,y+21,64,9);R(x+5,y+19,4,10,p.wallDark);R(x+55,y+19,4,10,p.wallDark);border(x,y,64,13,p.wood);R(x+2,y+2,60,3,light(p.wood,.2));R(x+3,y+16,58,8,light(p.wood,.13));R(x+3,y+21,58,3,dark(p.wood,.3));R(x,y+10,3,13,p.wallDark);R(x+61,y+10,3,13,p.wallDark);break;
			case 'coffee':
				shadow(x,y+40,44,8);border(x,y+17,44,28,p.wood);R(x-2,y+14,48,6,light(p.wood,.35));border(x+6,y,25,22,'#e4ddc6','#405957',2);R(x+9,y+4,19,10,'#586e65');R(x+11,y+6,8,3,'#b2d3b0');R(x+20,y+15,5,5,'#344947');mug(x+21,y+18);mug(x+34,y+9);R(x+6,y+30,30,2,dark(p.wood,.3));R(x+19,y+35,7,2,p.wallDark);break;
			case 'campfire':
				glow(x+28,y+24,92,'#f7a452');shadow(x+3,y+32,50,13);for(let i=0;i<7;i++){const xx=x+5+i*7,yy=y+34+(i%2)*5;border(xx,yy,9,7,'#7c8273','#545d52',1);}R(x+10,y+33,36,7,'#765242');R(x+15,y+29,30,6,'#a57648');R(x+19,y+16,20,21,'#e47d43');R(x+24,y+4,9,28,'#eaa24a');R(x+15,y+21,9,14,'#f0ad4d');R(x+22,y+19,13,17,'#f8d679');R(x+26,y+12,5,20,'#ffedac');R(x+13,y+6,3,3,'#e9ab5b');R(x+38,y+13,3,3,'#e9ab5b');break;
			case 'crate':
				shadow(x,y+23,32,9);border(x,y,32,30,p.wood);R(x+4,y+4,24,22,dark(p.wood,.15));R(x+3,y+3,26,3,light(p.wood,.2));R(x+3,y+24,26,3,light(p.wood,.2));for(let i=0;i<6;i++)R(x+5+i*3,y+5+i*3,4,4,light(p.wood,.16));R(x+4,y+6,2,2,p.wallDark);R(x+26,y+24,2,2,p.wallDark);break;
			case 'umbrella':
				shadow(x+7,y+60,65,12);R(x+38,y+24,4,45,p.wallDark);R(x+31,y+67,17,3,p.wallDark);for(let j=0;j<5;j++){const ww=32+j*12;R(x+40-ww/2,y+j*5,ww,7,j%2?light(c,.25):c);}R(x+2,y+24,76,5,dark(c,.1));for(let j=0;j<5;j++)R(x+5+j*15,y+28,9,4,j%2?light(c,.25):c);R(x+38,y+1,4,27,light(c,.4));break;
			case 'fountain':
				shadow(x,y+48,64,12);border(x+5,y+18,54,37,dark(p.wall,.1),p.wallDark,3);R(x+9,y+22,46,26,p.water);R(x+13,y+26,38,2,p.waterLight);border(x+23,y+3,18,40,p.wall,p.wallDark,2);R(x+17,y+14,30,7,light(p.wall,.1));R(x+27,y,10,8,p.waterLight);R(x+20,y+19,3,16,p.waterLight);R(x+42,y+19,3,16,p.waterLight);R(x+25,y+40,18,3,p.waterLight);break;
			case 'bike':
				for(const xx of [x+1,x+32]){border(xx,y+11,17,17,'#a6b69a','#3c5753',3);R(xx+7,y+13,2,13,'#d4d7bb');R(xx+2,y+18,13,2,'#d4d7bb');}R(x+11,y+10,22,3,c);R(x+19,y+12,4,12,c);R(x+10,y+20,24,3,c);R(x+29,y+7,4,15,c);R(x+29,y+4,11,3,'#3b5147');R(x+14,y+5,12,4,'#3b5147');R(x+22,y+22,10,2,'#3b5147');break;
			default:throw new Error('지원하지 않는 오브젝트: '+o.type);
		}
	}
	const props=scene.props||[],tableSeats=new Set(props.filter(o=>o.type==='longtable').flatMap(o=>o.seats||[]));props.filter(o=>o.type==='rug').forEach(prop);
	props.filter(o=>o.type!=='rug'&&!(o.type==='chair'&&tableSeats.has(o.seat))).sort((a,b)=>(a.y+(a.type==='tree'?70:30))-(b.y+(b.type==='tree'?70:30))).forEach(prop);
	if(night){for(let i=0;i<65;i++){const x=Math.floor(rand()*width),y=Math.floor(rand()*height);if((scene.rooms||[]).some(q=>x>q.x&&x<q.x+q.w&&y>q.y&&y<q.y+q.h))continue;R(x,y,2,2,'#dcda9b');if(i%4===0){R(x-2,y,6,2,'#d9d6a844');R(x,y-2,2,6,'#d9d6a844');}}}
	function drawActor(target,a,time=0){
		const key=String(a.id||a.name||'guest'),hash=[...key].reduce((n,ch)=>((n*31+ch.charCodeAt(0))>>>0),7);
		const ap=a.appearance||{},hairStyle=ap.hairStyle||ap.hairstyle||['short','bob','long','curly','bun'][hash%5];
		const outfit=ap.outfit||['jacket','hoodie','shirt','overall'][Math.floor(hash/5)%4];
		const accessory=ap.accessory||['none','glasses','none','headset','cap'][Math.floor(hash/20)%5],headwear=ap.headwear||(accessory==='cap'?'cap':'none');
		const shirt=a.shirt||'#8fa481',hair=a.hair||'#574638',skin=ap.skin||['#efc8a4','#dab08a','#bf8d6d','#e7b998'][hash%4];
		const trousers=ap.trousers||['#586957','#5b6673','#756253','#4c6864'][Math.floor(hash/3)%4],shoes=ap.shoes||'#47534b';
		const ink='#293630',back=a.facing?a.facing==='back':a.face==='up',side=!a.facing&&(a.face==='left'||a.face==='right'),cap=ap.capColor||shirt;
		const phase=a.walking?Math.floor(time/130)%4:0,stride=a.walking?[0,1,0,-1][phase]:0,bob=a.walking?phase%2:0;
		target.save();target.translate(Math.round(a.x-17.6),Math.round(a.y-52.8));target.scale(1.1,1.1);target.imageSmoothingEnabled=false;
		const px=(x,y,w,h,c)=>{target.fillStyle=c;target.fillRect(x,y,w,h);};
		const box=(x,y,w,h,c,edge=ink)=>{px(x,y,w,h,edge);px(x+1,y+1,w-2,h-2,c);};
		const oval=(x,y,w,h,c)=>{for(let row=0;row<h;row++){const ry=(row+.5-h/2)/(h/2),half=Math.sqrt(Math.max(0,1-ry*ry))*w/2,start=Math.round(w/2-half),end=Math.round(w/2+half);px(x+start,y+row,Math.max(1,end-start),1,c);}};
		const outlineOval=(x,y,w,h,c,edge=ink)=>{oval(x,y,w,h,edge);oval(x+1,y+1,w-2,h-2,c);};
		oval(5,46,23,3,'#17372b2b');oval(8,47,17,2,'#17372b18');
		if(a.face==='left'){target.translate(32,0);target.scale(-1,1);}
		target.translate(0,-bob);
		// 32×48 원본 격자에 머리 곡선과 한 픽셀 머릿결을 직접 그린다.
		if(hairStyle==='ponytail'){outlineOval(23,7,8,22,hair);px(27,11,1,12,light(hair,.19));px(25,25,3,2,light(hair,.09));box(23,9,5,3,'#b9a574');}
		if(hairStyle==='afro'){outlineOval(1,1,30,28,hair);for(const [hx,hy] of [[4,4],[9,2],[15,2],[22,4],[2,11],[26,11],[3,19],[25,19]]){oval(hx,hy,5,5,light(hair,.13));px(hx+1,hy+1,1,1,light(hair,.23));}}
		if(hairStyle==='long'){
			outlineOval(3,7,26,29,hair);px(3,16,4,17,dark(hair,.08));px(25,16,4,17,dark(hair,.19));
			px(4,17,1,13,light(hair,.18));px(6,21,1,12,light(hair,.12));px(26,19,1,13,light(hair,.13));px(24,26,1,8,dark(hair,.2));
		}
		if(hairStyle==='bob'){outlineOval(3,7,26,22,hair);px(4,17,3,10,hair);px(25,17,3,10,dark(hair,.1));px(5,16,1,8,light(hair,.21));px(26,18,1,6,light(hair,.14));}
		if(hairStyle==='bun'){outlineOval(12,0,9,8,hair);px(14,2,4,1,light(hair,.25));px(13,3,1,3,light(hair,.13));px(13,7,7,1,'#ad9970');}
		// 팔과 다리에 각각 명암을 넣고 신발 밑창을 발 좌표에 맞춘다.
		const lx=side?12:9,rx=side?17:17,leftLift=Math.max(0,stride),rightLift=Math.max(0,-stride);
		box(lx,37,6,9,trousers);box(rx,37,6,9,dark(trousers,.10));px(lx+1,39,2,5,light(trousers,.13));px(rx+1,40,2,4,light(trousers,.1));
		px(lx+3,41,1,3,dark(trousers,.16));px(rx+3,40,1,3,dark(trousers,.18));px(lx+1,44-leftLift,4,1,light(trousers,.17));
		outlineOval(lx-1,44-leftLift,8,4,shoes);outlineOval(rx,44-rightLift,8,4,shoes);
		px(lx,47-leftLift,6,1,light(shoes,.3));px(rx+1,47-rightLift,6,1,light(shoes,.26));px(lx+1,45-leftLift,3,1,'#d2d1bc');px(rx+2,45-rightLift,3,1,'#d2d1bc');
		const tx=side?10:7,tw=side?14:18;
		px(tx+3,26,tw-6,1,ink);px(tx+1,27,tw-2,2,ink);px(tx,29,tw,8,ink);px(tx+2,37,tw-4,2,ink);
		px(tx+2,28,tw-4,9,shirt);px(tx+1,30,tw-2,5,shirt);px(tx+3,27,tw-6,2,light(shirt,.12));px(tx+3,37,tw-6,1,dark(shirt,.19));
		const arm=(x,shift,cc)=>{outlineOval(x,28+shift,5,11,cc);px(x+1,30+shift,1,4,light(cc,.18));px(x+1,35+shift,3,2,dark(cc,.15));outlineOval(x,37+shift,5,4,skin);px(x+2,38+shift,1,2,light(skin,.1));};
		if(side)arm(19,stride,shirt);else {arm(4,stride,shirt);arm(23,-stride,dark(shirt,.08));}
		if(outfit==='coat'){
			px(tx+1,35,tw-2,7,ink);px(tx+2,35,tw-4,6,shirt);px(tx+3,41,tw-6,1,dark(shirt,.24));px(15,37,2,5,dark(shirt,.31));
			px(tx+3,36,3,1,light(shirt,.18));px(tx+tw-6,38,3,1,dark(shirt,.2));
		}
		if(outfit==='dress'){
			for(let row=0;row<14;row++){const widen=Math.floor(row/4);px(8-widen,29+row,16+widen*2,1,ink);px(9-widen,29+row,14+widen*2,1,shirt);}
			px(6,42,20,1,dark(shirt,.24));px(9,41,2,1,light(shirt,.22));px(14,35,1,7,light(shirt,.14));px(21,37,1,5,dark(shirt,.13));
		}
		px(13,24,6,4,dark(skin,.1));px(14,25,4,3,skin);px(14,27,4,1,light(skin,.08));
		if(!back){
			if(outfit==='jacket'||outfit==='coat'){
				px(13,28,6,10,'#e7e0c9');px(11,28,2,9,dark(shirt,.06));px(19,28,2,9,dark(shirt,.14));
				px(11,27,3,2,light(shirt,.24));px(12,29,2,2,light(shirt,.17));px(13,31,1,2,light(shirt,.14));
				px(18,27,3,2,light(shirt,.2));px(18,29,2,2,light(shirt,.13));px(18,31,1,2,light(shirt,.1));
				px(16,30,1,7,'#b1b5a2');px(16,32,1,2,'#e5e7d4');px(9,34,3,1,dark(shirt,.3));px(20,34,3,1,dark(shirt,.3));px(10,35,2,1,light(shirt,.1));
				if(outfit==='coat'){px(12,35,1,1,'#d5c28f');px(19,35,1,1,'#d5c28f');px(12,39,1,1,'#d5c28f');px(19,39,1,1,'#d5c28f');px(9,38,3,1,dark(shirt,.3));px(21,38,2,1,dark(shirt,.3));}
			}else if(outfit==='vest'){
				px(8,28,16,4,'#e5dfc7');px(9,27,5,11,shirt);px(18,27,5,11,dark(shirt,.09));px(13,28,1,4,dark(shirt,.20));px(18,28,1,4,dark(shirt,.23));
				px(14,31,4,7,'#dad5be');box(10,32,3,3,dark(shirt,.12),dark(shirt,.3));box(19,32,3,3,dark(shirt,.12),dark(shirt,.3));px(15,31,1,6,'#7d896f');
			}else if(outfit==='dress'){
				px(11,27,3,2,'#eee1bf');px(18,27,3,2,'#eee1bf');px(8,33,16,2,dark(shirt,.27));px(14,33,4,2,'#d9c18b');px(15,32,2,1,light(shirt,.3));
				px(11,37,1,1,light(shirt,.36));px(20,39,1,1,light(shirt,.36));px(16,40,1,1,light(shirt,.36));
			}else if(outfit==='hoodie'){
				outlineOval(10,25,12,6,dark(shirt,.08));px(12,27,8,2,light(shirt,.2));px(13,29,1,5,'#ded8c2');px(19,29,1,5,'#ded8c2');px(12,33,2,1,dark(shirt,.22));px(19,33,2,1,dark(shirt,.22));
				px(12,35,8,1,dark(shirt,.22));px(11,34,1,2,dark(shirt,.16));px(20,34,1,2,dark(shirt,.16));px(13,36,6,1,light(shirt,.1));
			}else if(outfit==='overall'){
				px(9,28,14,4,light(shirt,.27));px(10,27,3,10,trousers);px(19,27,3,10,trousers);px(10,32,12,7,trousers);
				px(11,31,1,1,'#e5c76e');px(20,31,1,1,'#e5c76e');px(13,33,6,4,dark(trousers,.12));px(14,34,4,2,light(trousers,.12));px(13,37,6,1,light(trousers,.2));
			}else{
				px(11,27,3,2,'#ede5d1');px(12,29,3,2,'#ede5d1');px(18,27,3,2,'#ede5d1');px(17,29,3,2,'#ede5d1');
				px(16,29,1,9,dark(shirt,.16));for(let yy=31;yy<38;yy+=3)px(16,yy,1,1,'#e5dcc2');px(10,32,4,3,dark(shirt,.09));px(10,32,4,1,light(shirt,.23));
			}
			px(tx+3,35,2,1,dark(shirt,.14));px(tx+tw-5,32,2,1,light(shirt,.12));
		}else{
			px(tx+3,29,tw-6,1,light(shirt,.18));px(tx+3,35,3,1,dark(shirt,.12));px(tx+tw-6,34,2,1,dark(shirt,.13));
			if(outfit==='hoodie'){outlineOval(10,26,12,7,dark(shirt,.1));px(12,28,8,2,light(shirt,.14));}
			if(outfit==='overall'){px(10,28,3,9,trousers);px(19,28,3,9,trousers);px(10,35,12,4,trousers);px(12,36,8,1,light(trousers,.16));}
		}
		// 둥근 얼굴과 턱선을 얇은 외곽선, 홍채와 피부 명암으로 분리한다.
		outlineOval(4,3,24,24,hair);outlineOval(4,15,4,7,dark(skin,.03));outlineOval(24,15,4,7,dark(skin,.07));
		oval(6,9,20,17,skin);oval(7,10,17,14,light(skin,.07));px(7,18,1,4,dark(skin,.08));px(24,18,1,4,dark(skin,.1));
		px(9,23,2,1,dark(skin,.06));px(11,25,10,1,dark(skin,.1));px(6,17,1,3,light(skin,.07));px(25,17,1,3,dark(skin,.12));
		oval(5,4,22,10,hair);px(7,11,18,2,hair);px(8,6,5,1,light(hair,.20));px(13,5,7,1,light(hair,.16));px(20,7,3,1,light(hair,.1));
		if(hairStyle==='short'){
			for(const [hx,hy,hh] of [[6,10,6],[8,11,3],[10,11,4],[13,11,2],[16,11,3],[20,10,3],[24,10,6]])px(hx,hy,2,hh,hair);
			px(6,8,1,4,light(hair,.12));px(10,7,3,1,light(hair,.25));px(14,6,4,1,light(hair,.15));px(25,12,1,5,dark(hair,.15));
		}else if(hairStyle==='bob'){
			px(4,10,3,14,hair);px(25,10,3,14,hair);px(8,12,15,2,hair);px(10,13,3,2,hair);px(20,13,2,2,hair);
			px(5,11,1,11,light(hair,.22));px(7,12,1,5,light(hair,.10));px(26,12,1,10,light(hair,.14));px(5,24,3,1,dark(hair,.17));
		}else if(hairStyle==='long'){
			px(4,10,4,15,hair);px(24,10,4,15,hair);px(8,11,4,3,hair);px(12,11,3,2,hair);px(21,11,3,4,hair);
			px(5,11,1,12,light(hair,.20));px(7,13,1,7,light(hair,.1));px(25,12,1,11,light(hair,.13));px(27,16,1,8,dark(hair,.2));px(9,8,3,1,light(hair,.25));
		}else if(hairStyle==='curly'){
			for(const [hx,hy] of [[6,4],[10,3],[15,3],[20,5],[24,8],[4,8],[4,12],[24,13],[7,11],[12,10],[17,11],[21,10]]){outlineOval(hx,hy,4,4,hair,dark(hair,.25));px(hx+1,hy+1,1,1,light(hair,.22));}
		}else if(hairStyle==='afro'){
			for(const [hx,hy] of [[3,9],[5,6],[9,5],[13,4],[17,5],[21,7],[25,11],[3,16],[25,17]]){outlineOval(hx,hy,4,5,hair,dark(hair,.19));px(hx+1,hy+1,1,1,light(hair,.22));}
			px(5,19,2,4,hair);px(25,19,2,4,hair);
		}else if(hairStyle==='ponytail'){
			px(5,10,3,7,hair);px(24,10,3,7,hair);px(8,11,5,2,hair);px(21,11,3,3,hair);px(7,7,1,5,light(hair,.2));px(22,6,1,5,light(hair,.14));
		}else{
			px(5,11,3,7,hair);px(24,11,3,7,hair);px(8,11,6,2,hair);px(21,11,3,3,hair);
			px(7,7,1,5,light(hair,.16));px(9,6,1,3,light(hair,.2));px(23,9,1,5,light(hair,.13));
		}
		if(back){
			oval(6,8,20,18,hair);px(9,11,1,10,light(hair,.15));px(11,10,1,7,light(hair,.1));px(22,14,1,8,dark(hair,.17));px(12,23,8,1,dark(hair,.12));
			if(hairStyle==='long'){oval(6,17,20,18,hair);px(8,19,1,12,light(hair,.16));px(11,20,1,13,light(hair,.1));px(22,21,1,11,dark(hair,.18));px(18,27,1,6,light(hair,.08));}
		}else if(side){
			oval(5,9,12,15,hair);px(6,13,1,8,light(hair,.17));outlineOval(14,16,4,6,skin,dark(skin,.12));px(15,18,1,2,dark(skin,.16));
			px(25,18,2,2,skin);px(25,20,1,1,dark(skin,.10));px(20,14,4,1,dark(hair,.12));px(20,16,4,5,'#f0ece0');
			px(22,16,2,4,'#566653');px(23,17,1,3,'#24352d');px(22,16,1,1,'#fff8e8');px(20,15,4,1,'#374236');px(20,20,4,1,dark(skin,.13));
			px(21,22,3,1,'#d49282');px(24,23,1,1,'#9e6d5d');
		}else{
			const eye=(x)=>{px(x,14,4,1,dark(hair,.12));px(x-1,16,1,3,dark(skin,.13));px(x,16,4,4,'#f6efe0');px(x+1,16,2,4,'#65735b');px(x+2,17,1,3,'#26382e');px(x+1,16,1,1,'#fffaf0');px(x,15,4,1,'#3c4437');px(x,20,4,1,dark(skin,.12));};
			eye(9);eye(19);px(15,20,1,2,dark(skin,.11));px(16,21,1,1,light(skin,.15));px(8,21,3,1,'#dba08b');px(22,21,3,1,'#dba08b');
			px(14,23,4,1,'#a97665');px(15,24,2,1,light(skin,.13));
		}
		if(accessory==='glasses'&&!back){
			const frame='#35453d',lens=(x,y)=>{['01111110','10000001','10000001','10000001','10000001','10000001','01111110'].forEach((row,j)=>[...row].forEach((v,i)=>{if(v==='1')px(x+i,y+j,1,1,frame);}));px(x+2,y+1,2,1,'#e8efdf');};
			if(side){lens(18,14);px(14,16,4,1,frame);px(25,15,2,1,frame);}else{lens(6,14);lens(18,14);px(14,16,4,1,frame);px(4,16,2,1,frame);px(26,16,2,1,frame);}
		}
		if(accessory==='headset'){
			px(5,7,1,9,'#547365');px(26,7,1,9,'#547365');px(8,4,16,2,'#688d79');outlineOval(3,15,4,8,'#799580');outlineOval(26,15,4,8,'#799580');px(4,17,1,4,'#a1b49a');
			if(!back){px(28,22,1,3,'#3f5a4d');px(24,25,5,1,'#3f5a4d');px(23,24,3,2,'#3f5a4d');}
		}
		if(headwear==='cap'){
			outlineOval(5,3,23,11,cap);px(7,6,1,5,light(cap,.20));px(9,5,7,1,light(cap,.23));px(16,5,1,5,dark(cap,.13));px(24,7,1,4,dark(cap,.17));
			px(7,12,20,2,dark(cap,.20));px(side?17:6,14,side?13:20,1,ink);px(side?18:7,13,side?11:18,1,light(cap,.10));
			px(15,7,3,4,'#dfddb3');px(15,7,2,1,'#f6edce');px(16,8,1,2,cap);px(15,10,3,1,'#c6c698');
			if(back){px(12,12,8,2,dark(cap,.24));px(14,13,4,1,'#b5b99d');}
		}
		// 직무 소품은 몸의 한쪽을 덮을 만큼 크게 그려 축소된 지도에서도 구분한다.
		const item=ap.roleItem;if(item){
			const ix=side?18:1,iy=29;
			if(item==='clipboard'){
				box(ix,iy,11,14,'#b99467');px(ix+2,iy+2,7,10,'#eee6ca');px(ix+4,iy-1,4,3,'#718370');
				for(let j=0;j<3;j++){px(ix+3,iy+4+j*3,1,1,'#72916b');px(ix+5,iy+4+j*3,3,1,'#a4af95');}px(ix+8,iy+3,1,8,'#b66b4c');
			}else if(item==='book'){
				box(ix,iy+1,13,12,'#638a87');px(ix+2,iy+2,4,9,'#eee6cb');px(ix+7,iy+2,4,9,'#f4edd6');px(ix+6,iy+2,1,10,'#a8a58d');
				for(let j=0;j<3;j++){px(ix+3,iy+4+j*2,2,1,'#b5b59a');px(ix+8,iy+4+j*2,2,1,'#b5b59a');}px(ix+9,iy+1,1,4,'#bf7965');
			}else if(item==='tablet'){
				box(ix,iy,10,14,'#4d6561');px(ix+2,iy+2,6,9,'#8eb4ad');px(ix+3,iy+3,4,1,'#e4efdb');px(ix+3,iy+6,3,1,'#5b8c85');px(ix+3,iy+8,4,1,'#5b8c85');px(ix+4,iy+12,2,1,'#a3b9a5');
			}else if(item==='camera'){
				px(ix+3,iy-1,5,3,'#506460');box(ix,iy+2,12,9,'#61756f');outlineOval(ix+3,iy+3,7,7,'#9fb7b0');oval(ix+4,iy+4,5,5,'#344d4b');px(ix+5,iy+4,2,1,'#d4e8d5');px(ix+1,iy+3,2,2,'#e5d9b0');px(ix+10,iy+3,1,1,'#b9d291');
			}else if(item==='palette'){
				outlineOval(ix,iy,12,14,'#c9a477');oval(ix+7,iy+2,3,3,ink);
				for(const [dx,dy,col] of [[3,2,'#d9896d'],[2,6,'#e2c467'],[4,10,'#87a776'],[8,10,'#769da9']])oval(ix+dx,iy+dy,3,3,col);
				px(ix+10,iy-2,1,12,'#715b40');px(ix+9,iy-3,3,3,'#8f7450');
			}else if(item==='briefcase'){
				box(ix+4,iy+2,5,4,'#b9956c');box(ix,iy+5,13,9,'#a8825a');px(ix+1,iy+7,11,1,light('#a8825a',.23));px(ix+6,iy+8,2,3,'#d2bd83');px(ix+2,iy+12,9,1,dark('#a8825a',.15));
			}else if(item==='coffee'){
				box(ix+2,iy+3,8,11,'#f0e5c9');box(ix+9,iy+5,4,6,'#ede1c5');px(ix+10,iy+6,2,4,p.grass);px(ix+3,iy+4,6,2,'#6f523e');px(ix+3,iy+8,6,3,'#8eaa79');px(ix+5,iy,1,2,'#efe8cb');px(ix+7,iy-2,1,2,'#efe8cb');
			}else if(item==='headphones'){
				px(9,27,1,7,'#445f55');px(22,27,1,7,'#445f55');px(10,33,12,2,'#445f55');outlineOval(7,29,5,7,'#84a291');outlineOval(20,29,5,7,'#84a291');px(8,31,1,3,'#b9c9ab');px(21,31,1,3,'#b9c9ab');
			}else if(item==='music'){
				box(ix,iy,12,14,'#f2ebd3','#a6af99');for(let j=0;j<5;j++)px(ix+2,iy+4+j,8,1,'#c7c8ae');
				px(ix+5,iy+3,1,6,'#5c7565');px(ix+8,iy+2,1,6,'#5c7565');px(ix+5,iy+2,4,2,'#5c7565');oval(ix+3,iy+8,3,2,'#5c7565');oval(ix+6,iy+7,3,2,'#5c7565');px(ix+3,iy+11,6,1,'#a6b29b');
			}
		}
		target.restore();
	}
	return {background,drawActor,width,height};
}
