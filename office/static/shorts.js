'use strict';
(()=>{
	function init(){
		const bridge=window.officeBridge,esc=bridge.esc;
		let loading=false;
		const when=value=>{const date=new Date(value);return Number.isNaN(date.getTime())?'시각 확인 불가':date.toLocaleString('ko-KR')};
		function person(){
			const group=bridge.state()?.shorts,reason=!bridge.token()?'화면의 세션을 다시 확인해 주세요.':'';
			return '<button class="outline-button shorts-open" data-shorts-open'+(reason?' disabled':'')+'>제작 요청</button><p class="shorts-notice">'+esc(reason||'이야기와 제작 조건을 접수합니다. 제작은 따로 시작합니다.')+'</p><div class="section"><h3>최근 접수</h3>'+(group?.status==='확인 불가'?'<p class="shorts-notice">'+esc(group.reason||'최근 접수를 확인할 수 없습니다.')+'</p>':'')+(group?.items||[]).slice(0,5).map(row=>'<article class="row shorts-recent"><strong>'+esc(row.id.slice(0,8))+'</strong><p>'+esc(row.story_preview)+'</p><small>'+esc(when(row.created))+'</small></article>').join('')+(!(group?.items||[]).length?'<p class="shorts-notice">최근 접수가 없습니다.</p>':'')+'</div>';
		}
		async function send(payload){
			if(!bridge.token())return {ok:false,message:'화면의 세션을 다시 확인해 주세요.',field:'summary'};
			const response=await fetch('/api/action',{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','X-Office-Token':bridge.token()},body:JSON.stringify(payload)});
			const data=await response.json();
			return {...data,ok:response.ok&&data.ok===true,code:response.status};
		}
		async function open(){
			if(loading||!bridge.token()||document.querySelector('dialog[open]'))return;
			loading=true;
			const focus=document.activeElement,controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),5000);
			try{
				const response=await fetch('/api/shorts/steps',{method:'GET',cache:'no-store',signal:controller.signal});
				const data=await response.json();
				if(!response.ok||!Array.isArray(data.steps)||!data.steps.length||!Array.isArray(data.source_presets)||!Array.isArray(data.save_presets))throw new Error();
				if(document.querySelector('dialog[open]'))return;
				window.officeShortsWizard.open(data,{focus,send,done:async message=>{bridge.toast(message);await bridge.refresh()}});
			}catch{bridge.toast('접수 질문을 읽지 못했습니다. 잠시 뒤 다시 열어 주세요.')}
			finally{clearTimeout(timeout);loading=false}
		}
		window.officeShorts={person,open};
		document.addEventListener('click',e=>{const button=e.target.closest('[data-shorts-open]');if(button&&!button.disabled)open()});
		if(bridge.state())bridge.refresh();
	}
	if(window.officeBridge)init();else document.addEventListener('office-ready',init,{once:true});
})();
