'use strict';
(()=>{
	const ended=new Set(['completed','failed','budget_exhausted','questions_exhausted','record_pending']);
	const direction=(dx,dy)=>Math.abs(dx)>Math.abs(dy)?dx<0?'left':'right':dy<0?'up':'down';
	function assignSeats(participants,chairs,walkable=()=>true){
		const seats=chairs.map(p=>({x:p.x+(p.w||24)/2,y:p.y+25})).sort((a,b)=>a.y-b.y||a.x-b.x);
		const xs=seats.map(p=>p.x),ys=seats.map(p=>p.y),left=seats.length?Math.min(...xs):0,right=seats.length?Math.max(...xs):0,top=seats.length?Math.min(...ys):0,bottom=seats.length?Math.max(...ys):0;
		const center={x:(left+right)/2,y:(top+bottom)/2},spares=[];
		for(let y=top;y<=bottom;y+=32)spares.push({x:left-32,y},{x:right+32,y});
		for(let x=left;x<=right;x+=32)spares.push({x,y:top-32},{x,y:bottom+32});
		const free=spares.filter(p=>walkable(p.x,p.y));
		return [...participants].sort((a,b)=>(a.name||a.role).localeCompare(b.name||b.role,'ko')).map((p,i)=>{
			const seat=seats[i]||free[i-seats.length]||{x:right+32,y:top+32*(i-seats.length)};
			return {...p,...seat,face:direction(center.x-seat.x,center.y-seat.y)};
		});
	}
	function step(position,path,seconds){
		let {x,y,face}=position,amount=90*Math.max(0,seconds),remaining=path.slice();
		while(remaining.length&&amount>0){
			const next=remaining[0],dx=next.x-x,dy=next.y-y,d=Math.hypot(dx,dy);
			if(d)face=direction(dx,dy);
			if(d<=amount){x=next.x;y=next.y;remaining.shift();amount-=d}
			else{x+=dx/d*amount;y+=dy/d*amount;amount=0}
		}
		return {x,y,face,walking:remaining.length>0,path:remaining};
	}
	function createController(bridge,now=()=>performance.now()){
		const sessions=new Map(),finished=new Set();
		function overlay(member,session){member.actor.status='작업 중';member.actor.task='회의 중 · '+Array.from(session.agenda||'').slice(0,12).join('')}
		function restore(member){
			Object.assign(member.actor,member.home,member.signal,{walking:false});
			if(!member.hadFacing)delete member.actor.facing;
		}
		function arrive(member){
			Object.assign(member.actor,member.move.target,{walking:false});
			const home=member.move.home;member.move=null;
			if(home){restore(member);return true}
			return false;
		}
		function move(member,target,home,instant=false){
			const actor=member.actor,path=instant||bridge.reducedMotion()?[]:bridge.routeFrom(actor.x,actor.y,target.x,target.y);
			member.move={target,path,home,started:now(),limit:Math.min(20000,3000+Math.max(0,path.length-1)*16/90*1000)};actor.facing=undefined;
			if(instant||bridge.reducedMotion()||!path.length)return arrive(member);
			actor.walking=true;return false;
		}
		function sync(record,initial=false){
			if(!record?.id||finished.has(record.id))return;
			let session=sessions.get(record.id);
			if(record.status==='running'&&!session){
				session={agenda:record.agenda,members:new Map(),returnAt:null};sessions.set(record.id,session);
			}
			if(!session)return;
			session.agenda=record.agenda||session.agenda;
			if(ended.has(record.status)&&session.returnAt===null)session.returnAt=now()+4000;
			if(record.status==='running'&&session.returnAt===null){
				const absent=new Set((record.absent||[]).map(p=>typeof p==='string'?p:p.role));
				const participants=(record.participants||[]).map(p=>typeof p==='string'?{role:p,name:bridge.state()?.meeting_roles?.find(r=>r.key===p)?.name}:p).filter(p=>!absent.has(p.role));
				const occupied=[...sessions.values()].flatMap(s=>[...s.members.values()].map(m=>m.seat));
				const chairs=bridge.chairs().filter(p=>!occupied.some(s=>s.x===p.x+(p.w||24)/2&&s.y===p.y+25));
				const claimed=new Set([...sessions.values()].flatMap(s=>[...s.members.values()].map(m=>m.actor)));
				const newcomers=participants.filter(p=>!session.members.has(p.role)&&bridge.actors().some(a=>a.name===p.name&&!claimed.has(a)));
				for(const seat of assignSeats(newcomers,chairs,bridge.walkable)){
					const actor=bridge.actors().find(a=>a.name===seat.name&&!claimed.has(a));if(!actor)continue;
					const member={actor,seat,hadFacing:Object.hasOwn(actor,'facing'),home:{x:actor.x,y:actor.y,face:actor.face,facing:actor.facing},signal:{status:actor.status,task:actor.task},move:null};
					session.members.set(seat.role,member);claimed.add(actor);move(member,{x:seat.x,y:seat.y,face:seat.face},false,initial);
				}
				for(const [role,member] of session.members)if(absent.has(role)&&!member.move?.home){
					if(move(member,member.home,true))session.members.delete(role);
				}
			}
			for(const member of session.members.values())overlay(member,session);
		}
		function captureSignals(state){
			for(const session of sessions.values())for(const member of session.members.values()){
				const signal=state?.people?.find(p=>p.id===member.actor.id);
				member.signal=signal?{status:signal.status,task:signal.task}:{status:member.actor.status,task:member.actor.task};
				overlay(member,session);
			}
		}
		function tick(t,seconds){
			for(const [id,session] of sessions){
				if(session.returnAt!==null&&now()>=session.returnAt&&!session.returning){
					session.returning=true;
					for(const [role,member] of session.members)if(move(member,member.home,true))session.members.delete(role);
				}
				for(const [role,member] of session.members){
					if(member.move){
						if(bridge.reducedMotion()||now()-member.move.started>=member.move.limit){if(arrive(member))session.members.delete(role)}
						else{
							const next=step(member.actor,member.move.path,seconds);member.move.path=next.path;
							Object.assign(member.actor,{x:next.x,y:next.y,face:next.face,walking:next.walking});
							if(!next.path.length&&arrive(member))session.members.delete(role);
						}
					}
					if(session.members.has(role))overlay(member,session);
				}
				if(session.returning&&!session.members.size){sessions.delete(id);finished.add(id)}
			}
		}
		return {sync,tick,captureSignals};
	}
	window.officeMeetingScene={assignSeats,step,createController};
	function init(){
		const bridge=window.officeBridge,controller=createController(bridge);let initial=true;
		function stateChanged(state){
			controller.captureSignals(state);
			for(const row of state?.meetings?.items||[])controller.sync(row,initial);
			if(state)initial=false;
		}
		bridge.tickHook(controller.tick);
		document.addEventListener('office-state',e=>stateChanged(e.detail));
		document.addEventListener('office-meeting-detail',e=>controller.sync(e.detail,initial));
		if(bridge.state())stateChanged(bridge.state());
	}
	if(window.officeBridge)init();else document.addEventListener('office-ready',init,{once:true});
})();
