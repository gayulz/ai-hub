'use strict';
(()=>{
	function init(){
		const bridge=window.officeBridge,$=id=>document.getElementById(id),esc=bridge.esc,dialog=$('task-dialog');
		let current=null,focus=null,busy=false,stale=false;
		const rows=()=>bridge.state()?.tasks?.items||[];
		const reason=()=>!bridge.token()?'화면의 세션을 읽지 못했습니다. 다시 열어 주세요.':bridge.state()?.tasks?.status!=='정상'?'할 일 자료를 읽지 못했습니다. 다시 읽어 주세요.':'';
		const date=value=>value?new Date(value).toLocaleString('ko-KR',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}):'';
		function row(value){
			const done=value.status==='done',disabled=busy||!!reason();
			return '<div class="task-row'+(done?' task-done':'')+'"><button class="task-check" role="checkbox" aria-checked="'+done+'" aria-label="'+esc((done?'다시 열기: ':'완료: ')+value.title)+'" data-task-toggle="'+esc(value.id)+'" data-task-kind="'+esc(value.kind)+'"'+(disabled?' disabled':'')+'><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="4"/>'+(done?'<path d="m8 12 3 3 5-6"/>':'')+'</svg></button>'
				+'<button class="task-detail" data-task-id="'+esc(value.id)+'" data-task-kind="'+esc(value.kind)+'"><span class="task-label">'+(value.kind==='task'?'내 할 일':'결정')+(done?' · 완료 '+esc(date(value.completed)):'')+'</span><strong>'+esc(value.title)+'</strong>'+(value.memo?'<span class="task-preview">'+esc(value.memo)+'</span>':'<span class="task-hint">메모 '+(value.kind==='task'?'수정':'남기기')+'</span>')+'</button></div>';
		}
		function controls(){
			const locked=busy||stale||!!reason(),valid=!!$('task-name').value.trim()&&Array.from($('task-name').value).length<=(current?.kind==='decision'?300:200)&&Array.from($('task-memo').value).length<=2000;
			for(const id of ['task-name','task-memo'])$(id).disabled=busy;
			$('task-save').disabled=locked||!valid;$('task-toggle').disabled=locked||!valid;$('task-close').disabled=busy;
			$('task-reload').hidden=!stale;$('task-reload').disabled=busy;dialog.setAttribute('aria-busy',String(busy));
			$('task-remaining').textContent=Array.from($('task-memo').value).length+' / 2000자';
		}
		function show(value,button){
			if(busy||document.querySelector('dialog[open]'))return;
			current=value?{...value}:{id:'t-'+Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join(''),kind:'task',version:0};focus=button||document.activeElement;stale=false;
			$('task-title').textContent=value?'할 일 보기':'할 일 추가';$('task-name').value=value?.title||'';$('task-memo').value=value?.memo||'';
			$('task-name').readOnly=value?.kind==='decision';$('task-name').maxLength=value?.kind==='decision'?300:200;
			$('task-save').textContent=value?'저장':'등록';$('task-toggle').hidden=!value;$('task-toggle').textContent=value?.status==='done'?'다시 열기':'완료 처리';
			$('task-meta').textContent=value?.status==='done'?'완료 '+date(value.completed):value?.kind==='decision'?'기존 결정의 처리 메모를 남길 수 있습니다.':'생각난 일을 적어 두고, 끝내면 완료로 체크하세요.';
			$('task-source').textContent=value?.reply||'';$('task-source').hidden=!value?.reply;
			$('task-status').textContent=reason();dialog.showModal();controls();$('task-name').focus();
		}
		function close(){
			if(busy)return;dialog.close();current=null;
			if(focus?.isConnected)focus.focus({preventScroll:true});else $('close-panel').focus();
		}
		async function send(action,value,modal){
			if(busy||reason()||modal&&stale)return;
			const payload={action,id:value.id,title:modal?$('task-name').value:value.title,memo:modal?$('task-memo').value:value.memo};
			if(action!=='task_create')Object.assign(payload,{kind:value.kind,version:value.version});
			busy=true;if(modal){$('task-status').textContent='저장 중';controls()}
			const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),10000);
			try{
				const response=await fetch('/api/action',{method:'POST',cache:'no-store',signal:controller.signal,headers:{'Content-Type':'application/json','X-Office-Token':bridge.token()},body:JSON.stringify(payload)}),data=await response.json();
				if(!response.ok||!data.ok){
					if(modal){stale=response.status===409;$('task-status').textContent=data.message||'할 일을 저장하지 못했습니다.'}else bridge.toast(data.message||'할 일을 저장하지 못했습니다.');
				}else{
					busy=false;if(modal)close();bridge.toast(data.message);
				}
			}catch{
				if(modal){stale=true;$('task-status').textContent='결과를 받지 못했습니다. 입력은 남아 있습니다. 다시 읽고 저장 여부를 확인해 주세요.'}
				else bridge.toast('결과를 받지 못했습니다. 목록에서 처리 상태를 확인해 주세요.');
			}finally{clearTimeout(timeout);busy=false;await bridge.refresh();if(dialog.open)controls()}
		}
		$('task-name').oninput=controls;$('task-memo').oninput=controls;$('task-close').onclick=close;
		$('task-save').onclick=()=>{controls();if(!$('task-save').disabled)return send(current.version===0&&current.kind==='task'?'task_create':'task_update',current,true)};
		$('task-toggle').onclick=()=>{controls();if(!$('task-toggle').disabled)return send(current.status==='done'?'task_reopen':'task_complete',current,true)};
		$('task-reload').onclick=async()=>{
			if(busy)return;busy=true;controls();
			try{
				await bridge.refresh();const value=rows().find(r=>r.id===current.id&&r.kind===current.kind);
				if(reason()){$('task-status').textContent=reason();return}
				if(value){current={...value};$('task-name').value=value.title;$('task-memo').value=value.memo;$('task-toggle').hidden=false;$('task-toggle').textContent=value.status==='done'?'다시 열기':'완료 처리';$('task-save').textContent='저장';$('task-meta').textContent=value.status==='done'?'완료 '+date(value.completed):'최신 내용을 확인하고 저장하세요.';$('task-source').textContent=value.reply||'';$('task-source').hidden=!value.reply}
				else if(current.kind!=='task'||current.version!==0){$('task-status').textContent='할 일이 없거나 원본 결정이 닫혔습니다. 입력을 확인한 뒤 닫아 주세요.';return}
				stale=false;$('task-status').textContent=value?'최신 내용을 읽었습니다.':'아직 등록되지 않았습니다. 입력을 확인하고 등록하세요.';
			}finally{busy=false;controls()}
		};
		dialog.addEventListener('cancel',event=>{event.preventDefault();close()});
		document.addEventListener('click',event=>{
			const button=event.target.closest('button');if(!button||button.disabled)return;
			if(button.dataset.taskAdd!==undefined){show(null,button);return}
			const id=button.dataset.taskId||button.dataset.taskToggle;if(!id)return;
			const value=rows().find(r=>r.id===id&&r.kind===button.dataset.taskKind);if(!value)return;
			if(button.dataset.taskToggle)send(value.status==='done'?'task_reopen':'task_complete',value,false);else show(value,button);
		});
		document.addEventListener('office-state',()=>{if(dialog.open)controls()});
		window.officeTasks={row};bridge.refresh();
	}
	if(window.officeBridge)init();else document.addEventListener('office-ready',init,{once:true});
})();
