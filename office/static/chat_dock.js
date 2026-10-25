'use strict';
(()=>{
	function init(){
		const bridge=window.officeBridge,$=id=>document.getElementById(id),esc=bridge.esc,dock=$('chat-dock'),log=$('chat-log'),input=$('chat-text');
		const labels={idle:'대기',busy:'답을 쓰는 중',closed:'닫힘'},sources={office:'사무실',telegram:'텔레그램',cli:'명령줄'};
		const records=new Map(),pending=new Map(),running=new Set(),seen=new Map(),versions=new Map();
		let current=null,opened=false,expanded=true,timer=null,focus=null,count=0,sending=false,overlaid=false;
		const rows=()=>bridge.state()?.chats?.items||[],record=()=>records.get(current);
		const otherDialog=()=>!!document.querySelector('dialog[open]');
		function bubble(){
			const actor=bridge.actors().find(a=>a.name==='PM');if(!actor)return;
			const row=record(),text=running.size?'대화 중 · 답을 쓰는 중':opened&&row?.status==='idle'?'대화 중 · '+row.target:'';
			if(text){actor.task=text;actor.status='작업 중';overlaid=true}
			else if(overlaid){
				const signal=bridge.state()?.people?.find(p=>p.id===actor.id);
				actor.task=signal?.task||'다음 작업 대기';actor.status=signal?.status||'확인 불가';overlaid=false;
			}
		}
		function visibility(){
			const hidden=!opened||otherDialog()||!$('meeting-popover').hidden;
			if(dock.hidden!==hidden)dock.hidden=hidden;
			$('chat-to-dev').disabled=!bridge.token()||otherDialog();
		}
		function blocked(){
			const row=record();
			if(!row)return '대화 기록을 먼저 읽어 주세요.';
			if(!bridge.token())return '반영 세션을 읽지 못했습니다. 화면을 다시 열어 주세요.';
			if(sending||row.status==='busy')return 'PM이 답을 쓰는 중입니다.';
			if(row.status==='closed')return '끝난 대화입니다. 새 대화를 시작해 주세요.';
			if(row.card&&row.card.status!=='confirmed')return '질문 카드에 답하고 요약을 먼저 확인해 주세요.';
			if((row.turns||[]).filter(t=>t.role==='user').length>=50)return '한 대화의 글 50번을 모두 보냈습니다. 새 대화를 시작해 주세요.';
			if(Array.from(input.value).length>2000)return '2000자 이내로 적어 주세요.';
			if(!input.value.trim())return '보낼 글을 적어 주세요.';
			return '';
		}
		function controls(){
			const row=record(),reason=blocked();
			$('chat-send').disabled=!!reason;$('chat-send-reason').textContent=reason;
			$('chat-remaining').textContent=(2000-Array.from(input.value).length)+'자 남음';
			$('chat-composer').hidden=row?.status==='closed';
			input.disabled=!row||sending||row.status!=='idle'||!bridge.token()||!!row.card&&row.card.status!=='confirmed';
			$('chat-end').hidden=row?.status==='closed';$('chat-end').disabled=!row||sending||row.status!=='idle'||!bridge.token();
			$('chat-new').disabled=!!window.officeChat.unavailable(true)||sending;
			$('chat-question').disabled=!row||sending||row.status!=='idle'||!!window.officeChat.unavailable();
			$('chat-log').setAttribute('aria-busy',String(row?.status==='busy'));
		}
		function render(){
			$('chat-toggle').textContent=expanded?'접기':'펼치기';$('chat-toggle').setAttribute('aria-expanded',String(expanded));
			$('chat-dock-content').hidden=!expanded;dock.dataset.expanded=String(expanded);
			const row=record();
			if(row){
				$('chat-dock-title').textContent='대화 · '+row.target;$('chat-dock-ai').textContent=row.plan+' · '+row.tool;
				$('chat-dock-state').textContent=labels[row.status];$('chat-dock-calls').textContent='호출 '+row.calls;
				const turns=row.turns||[],follow=count===0||log.scrollHeight-log.scrollTop-log.clientHeight<48;
				if(turns.length<count){log.replaceChildren();count=0}
				let added=false;
				for(;count<turns.length;count++){
					const turn=turns[count],user=turn.role==='user',item=document.createElement('article');
					item.className='chat-message '+(user?'chat-user':'chat-assistant');
					item.innerHTML='<strong>'+(user?'나':'PM')+'</strong><p>'+esc(turn.text)+'</p><small>'+esc(sources[turn.source]||'명령줄')+'</small>';
					log.append(item);added=true;
				}
				if(added&&follow)log.scrollTop=log.scrollHeight;
				$('chat-needs-user').hidden=!row.needs_user;$('chat-needs-user').textContent=row.needs_user?'사용자 확인 필요: '+row.needs_user:'';
				$('chat-failure').textContent=row.failure?'대화를 마치지 못했습니다. '+(typeof row.failure_reason==='string'&&row.failure_reason?'이유: '+row.failure_reason+'. ':'')+'상태를 확인하고 다시 보내 주세요.':'';
				$('chat-question').hidden=row.status==='closed'||!['waiting','answered'].includes(row.card?.status);
				$('chat-question').textContent=row.card?.status==='answered'?'답변 확인':'질문 카드에 답하기';
				$('chat-to-dev').hidden=!row.dev_draft;
			}
			controls();visibility();bubble();
		}
		function syncTimer(){
			if(running.size){if(timer===null)timer=setInterval(poll,3000)}
			else if(timer!==null){clearInterval(timer);timer=null}
		}
		function update(id,changes){
			versions.set(id,(versions.get(id)||0)+1);
			const value={...records.get(id),...changes,id};records.set(id,value);
			if(value.status==='busy')running.add(id);else running.delete(id);
			if(id===current)render();else bubble();syncTimer();
		}
		async function read(id){
			if(pending.has(id))return pending.get(id);
			const version=versions.get(id)||0;
			const task=(async()=>{
				const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),5000);
				try{
					const response=await fetch('/api/chat?id='+encodeURIComponent(id),{method:'GET',cache:'no-store',signal:controller.signal}),value=await response.json();
					if(!response.ok||value.id!==id||!labels[value.status]||!Array.isArray(value.turns))throw new Error();
					// 전송·확인 전에 시작된 조회가 새 진행 상태를 덮어쓰지 않게 한다.
					if((versions.get(id)||0)!==version)return records.get(id)||null;
					const wasRunning=running.has(id);update(id,value);
					if(id===current)$('chat-dock-notice').textContent='';
					if(wasRunning&&value.status!=='busy')bridge.refresh();
					return value;
				}catch{
					if(id===current)$('chat-dock-notice').textContent='대화 기록을 읽지 못했습니다. 잠시 뒤 다시 확인해 주세요.';
					return null;
				}finally{clearTimeout(timeout)}
			})();
			pending.set(id,task);
			try{return await task}finally{pending.delete(id);syncTimer()}
		}
		function poll(){return Promise.all([...running].map(read))}
		async function open(id){
			if(!/^c-[a-f0-9]{32}$/.test(id)||otherDialog()||sending)return false;
			focus=document.activeElement;
			if(current!==id){
				log.replaceChildren();count=0;input.value='';
				$('chat-needs-user').textContent='';$('chat-needs-user').hidden=true;$('chat-failure').textContent='';
				$('chat-question').hidden=true;$('chat-to-dev').hidden=true;
				$('chat-dock-title').textContent='대화';$('chat-dock-ai').textContent='';$('chat-dock-state').textContent='조회 중';$('chat-dock-calls').textContent='호출 0';
			}
			current=id;opened=true;expanded=true;$('meeting-popover').hidden=true;
			document.dispatchEvent(new CustomEvent('office-dock-open',{detail:'chat'}));
			$('chat-dock-notice').textContent='대화 기록 조회 중';render();$('chat-toggle').focus({preventScroll:true});
			const value=await read(id);
			if(opened&&current===id)log.scrollTop=log.scrollHeight;
			if(opened&&current===id&&value?.status==='idle'&&value.card?.status==='answered')window.officeChat.openQuestions(value);
			return true;
		}
		function close(notice=true){
			opened=false;visibility();bubble();
			if(notice){
				bridge.toast('대화 기록은 남고 PM에서 다시 열 수 있습니다');
				if(focus?.isConnected)focus.focus({preventScroll:true});else $('close-panel').focus();
			}
		}
		async function send(){
			controls();if($('chat-send').disabled)return;
			const id=current,text=input.value;sending=true;controls();$('chat-dock-notice').textContent='글을 보내는 중입니다.';
			try{
				const data=await window.officeChat.send({action:'chat_send',id,text});
				if(data.ok){input.value='';$('chat-dock-notice').textContent='';update(id,{status:'busy',failure:''})}
				else{
					if(data.conflict){await read(id);await bridge.refresh()}
					$('chat-dock-notice').textContent=data.message||'대화 결과를 확인할 수 없습니다.';
				}
			}catch{
				await read(id);await bridge.refresh();$('chat-dock-notice').textContent='결과를 받지 못했습니다. 다시 보내기 전에 대화 기록을 확인해 주세요.';
			}finally{sending=false;render()}
		}
		function stateChanged(){
			for(const row of rows()){
				const key=JSON.stringify([row.status,row.updated,row.calls,row.card]);
				if(seen.get(row.id)!==key){
					seen.set(row.id,key);
					if(row.status==='busy'){running.add(row.id);read(row.id)}
				}
			}
			if(opened){render();if(!sending&&current)read(current)}
			bubble();syncTimer();
		}
		function viewport(){
			const view=window.visualViewport,inset=view?Math.max(0,innerHeight-view.height-view.offsetTop):0;
			dock.style.bottom=inset>80?'calc('+inset+'px + env(safe-area-inset-bottom))':'';
			dock.style.maxHeight=inset>80?Math.max(0,view.height-16)+'px':'';
		}
		$('chat-toggle').onclick=()=>{expanded=!expanded;render()};$('chat-dock-close').onclick=()=>close();
		$('chat-new').onclick=()=>window.officeChat.openNew();$('chat-end').onclick=()=>window.officeChat.openEnd(record());
		$('chat-question').onclick=()=>{if(!$('chat-question').disabled)window.officeChat.openQuestions(record())};
		$('chat-to-dev').onclick=()=>{const row=record();if(row?.dev_draft&&!$('chat-to-dev').disabled&&bridge.openDevWith(row.dev_draft,row.target))visibility()};
		input.oninput=controls;$('chat-send').onclick=send;
		input.addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)&&!e.isComposing){e.preventDefault();send()}});
		dock.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();close()}});
		document.addEventListener('office-dock-open',e=>{if(e.detail!=='chat')close(false)});
		document.addEventListener('office-state',stateChanged);
		if(typeof MutationObserver!=='undefined')new MutationObserver(visibility).observe(document.body,{subtree:true,attributes:true,attributeFilter:['open','hidden']});
		window.visualViewport?.addEventListener('resize',viewport);window.visualViewport?.addEventListener('scroll',viewport);
		window.addEventListener('resize',viewport);bridge.tickHook(bubble);
		window.officeChatDock={open,close,read,detail:record,update};stateChanged();viewport();
	}
	if(window.officeBridge)init();else document.addEventListener('office-ready',init,{once:true});
})();
