'use strict';
(()=>{
	const statuses={analyzing:'분석 중',participants_pending:'참석자 확인 대기',questions_pending:'질문 답변 대기',answers_pending:'답변 확인 대기',running:'진행 중',completed:'완료',failed:'실패',budget_exhausted:'예산 소진',questions_exhausted:'질문 한도 소진',record_pending:'기록 대기'};
	const ended=new Set(['completed','failed','budget_exhausted','questions_exhausted','record_pending']);
	const cut=(value,size)=>Array.from(value||'').slice(0,size).join('');
	function devText(record){
		return cut('안건: '+cut(record.agenda,120)+'\n결론: '+(record.summary?.conclusion||[]).join('\n')+'\n할 일: '+(record.summary?.todo||[]).map(t=>'- '+t).join('\n'),2000);
	}
	function init(){
		const bridge=window.officeBridge,$=id=>document.getElementById(id),esc=bridge.esc,dock=$('meeting-dock');
		const records=new Map(),running=new Set(),pending=new Map(),counts={'1':0,'2':0};
		let current=null,opened=false,expanded=true,timer=null,focus=null,summaryKey='';
		const rows=()=>bridge.state()?.meetings?.items||[];
		const record=()=>records.get(current)||rows().find(r=>r.id===current);
		const otherDialog=()=>!!document.querySelector('dialog[open]');
		function visibility(){
			const hidden=!opened||otherDialog()||!$('meeting-popover').hidden;
			if(dock.hidden!==hidden)dock.hidden=hidden;
			$('meeting-to-dev').disabled=!bridge.token()||otherDialog();
		}
		function render(){
			$('meeting-toggle').textContent=expanded?'접기':'펼치기';
			$('meeting-toggle').setAttribute('aria-expanded',String(expanded));
			$('meeting-dock-content').hidden=!expanded;dock.dataset.expanded=String(expanded);
			const row=record();if(!row)return;
			$('meeting-dock-title').textContent=cut(row.agenda||'회의',30);
			$('meeting-dock-state').textContent=statuses[row.status]||'확인 불가';
			const max=row.calls_max||14,calls=row.calls||0;
			$('meeting-dock-calls').textContent='호출 '+calls+'/'+max;
			$('meeting-progress').setAttribute('aria-valuemax',String(max));
			$('meeting-progress').setAttribute('aria-valuenow',String(Math.min(calls,max)));
			$('meeting-progress-bar').style.width=Math.min(100,calls/max*100)+'%';
			const round=row.phase==='round2'?'2':'1',spoken=new Set((row.rounds?.[round]||[]).map(p=>p.role)),spokeFirst=new Set((row.rounds?.['1']||[]).map(p=>p.role));
			$('meeting-dock-participants').innerHTML=(row.participants||[]).map(p=>{
				if(typeof p==='string')p={role:p,name:bridge.state()?.meeting_roles?.find(r=>r.key===p)?.name||p};
				const absent=(row.absent||[]).find(a=>(typeof a==='string'?a:a.role)===p.role);
				const status=absent?'불참('+(absent.reason||'사유 확인 대기')+')':spoken.has(p.role)?'발언 완료':round==='2'&&spokeFirst.has(p.role)?'2라운드 대기':'대기';
				return '<li>'+esc(p.name)+' · '+esc(status)+'</li>';
			}).join('');
			let added=false;
			for(const number of ['1','2']){
				const values=row.rounds?.[number]||[],list=$('meeting-log-'+number);
				for(;counts[number]<values.length;counts[number]++){
					const speech=values[counts[number]],item=document.createElement('li');
					item.textContent=speech.name+': '+speech.text;list.append(item);added=true;
				}
			}
			const summary=ended.has(row.status)?row.summary:null,key=JSON.stringify(summary);
			if(key!==summaryKey){
				summaryKey=key;
				$('meeting-result').innerHTML=summary?['conclusion','todo','open_questions','decisions'].map((k,i)=>'<section><h3>'+['결론','할 일','열린 질문','결정 후보'][i]+'</h3>'+((summary[k]||[]).length?'<ul>'+summary[k].map(t=>'<li>'+esc(t)+'</li>').join('')+'</ul>':'<p class="muted">없음</p>')+'</section>').join(''):'';
			}
			$('meeting-dock-failure').textContent=row.failure||'';
			$('meeting-to-dev').hidden=row.status!=='completed'||!row.summary;
			if(added)$('meeting-logs').scrollTop=$('meeting-logs').scrollHeight;
			visibility();
		}
		function syncTimer(){
			if(opened||running.size){if(timer===null)timer=setInterval(poll,3000)}
			else if(timer!==null){clearInterval(timer);timer=null}
		}
		async function read(id){
			if(pending.has(id))return pending.get(id);
			const task=(async()=>{
				const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),5000);
				try{
					const response=await fetch('/api/meeting?id='+encodeURIComponent(id),{method:'GET',cache:'no-store',signal:controller.signal}),value=await response.json();
					if(!response.ok||value.id!==id||!statuses[value.status])throw new Error();
					records.set(id,value);
					if(value.status==='running')running.add(id);else running.delete(id);
					document.dispatchEvent(new CustomEvent('office-meeting-detail',{detail:value}));
					if(id===current){$('meeting-dock-notice').textContent='';render()}
				}catch{if(id===current)$('meeting-dock-notice').textContent='회의 기록을 읽지 못했습니다. 잠시 뒤 다시 확인해 주세요.'}
				finally{clearTimeout(timeout)}
			})();
			pending.set(id,task);
			try{await task}finally{pending.delete(id);syncTimer()}
		}
		function poll(){
			const ids=new Set(running);if(opened&&current)ids.add(current);
			return Promise.all([...ids].map(read));
		}
		async function open(id){
			if(!/^m-[a-f0-9]{32}$/.test(id)||otherDialog())return false;
			focus=document.activeElement;
			if(current!==id){
				for(const number of ['1','2']){counts[number]=0;$('meeting-log-'+number).replaceChildren()}
				$('meeting-result').replaceChildren();summaryKey='';
				$('meeting-dock-participants').replaceChildren();$('meeting-dock-failure').textContent='';$('meeting-to-dev').hidden=true;
				$('meeting-dock-calls').textContent='호출 0/14';$('meeting-progress-bar').style.width='0%';
			}
			current=id;opened=true;expanded=true;$('meeting-popover').hidden=true;
			document.dispatchEvent(new CustomEvent('office-dock-open',{detail:'meeting'}));
			$('meeting-dock-notice').textContent='회의 기록 조회 중';
			$('meeting-dock-title').textContent='회의';$('meeting-dock-state').textContent='조회 중';
			render();visibility();syncTimer();$('meeting-toggle').focus({preventScroll:true});
			await read(id);return true;
		}
		function close(){
			opened=false;visibility();syncTimer();
			bridge.toast('닫아도 회의는 서버에서 계속 진행됩니다. 회의실에서 다시 열 수 있습니다.');
			if(focus?.isConnected)focus.focus({preventScroll:true});
		}
		function stateChanged(){
			let added=false;
			for(const row of rows()){
				if(row.status==='running'&&!ended.has(records.get(row.id)?.status)){
					if(!running.has(row.id)){running.add(row.id);added=true}
				}else if(ended.has(row.status))running.delete(row.id);
			}
			if(opened)render();syncTimer();if(added)poll();
		}
		$('meeting-toggle').onclick=()=>{expanded=!expanded;render()};
		$('meeting-dock-close').onclick=close;
		$('meeting-to-dev').onclick=()=>{
			const row=record();
			if(!row||row.status!=='completed'||!row.summary||$('meeting-to-dev').disabled)return;
			if(bridge.openDevWith(devText(row),row.target))visibility();
		};
		dock.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();close()}});
		document.addEventListener('office-state',stateChanged);
		document.addEventListener('office-dock-open',e=>{if(e.detail!=='meeting'){opened=false;visibility();syncTimer()}});
		if(typeof MutationObserver!=='undefined')new MutationObserver(visibility).observe(document.body,{subtree:true,attributes:true,attributeFilter:['open','hidden']});
		window.officeMeetingPanel={open,close,devText};stateChanged();
	}
	if(window.officeBridge)init();else document.addEventListener('office-ready',init,{once:true});
})();
