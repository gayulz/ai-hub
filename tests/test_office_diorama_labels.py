"""직원 이름표의 화면 크기별 표시와 배치를 확인한다."""

import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from test_office_diorama import SCENE_HARNESS


ROOT = Path(__file__).resolve().parents[1]


class DioramaLabelTest(unittest.TestCase):
	@unittest.skipUnless(shutil.which("node"), "JavaScript 실행기가 없습니다")
	def test_label_selection_zoom_and_overlapping_positions(self):
		script = SCENE_HARNESS.split("get('#mailbox-marker').dataset.pending=String(count);")[0]
		script = script.replace("updateOwner, resize };", "updateOwner, resize, updateMarkers, view, markerNodes, markerRects };")
		script += r'''
const labelNodes=scene.markerNodes;
const visible=()=>world.staff.filter(p=>!labelNodes.get(p.id).hidden);
const classes=p=>labelNodes.get(p.id).className.split(' ');
assert(visible().length>0);
for(const p of world.staff)assert(classes(p).includes('compact'),p.id+' 기본 확대');
for(const zoom of [.72,1.599]){
	scene.view.zoom=zoom;scene.updateMarkers();
	for(const p of world.staff)assert(classes(p).includes('compact'),p.id+' 축소');
}
scene.view.zoom=1.6;scene.updateMarkers();
for(const p of world.staff)assert(!classes(p).includes('compact'),p.id+' 확대');
scene.view.zoom=1;scene.updateMarkers();
const p=visible().find(p=>p.id!=='owner');
const node=labelNodes.get(p.id);
node.onpointerenter();scene.updateMarkers();
assert(!classes(p).includes('compact'),'호버 시 상세');
node.onpointerleave();scene.updateMarkers();
assert(classes(p).includes('compact'),'호버 해제 시 이름');
node.onfocus();scene.updateMarkers();assert(!classes(p).includes('compact'),'키보드 포커스 시 상세');
node.onblur();scene.updateMarkers();assert(classes(p).includes('compact'),'포커스 해제 시 이름');
while(frames.length){f=frames.shift();f(120)}
node.onclick();
assert(frames.length>0,'선택 후 다시 그려야 합니다');
scene.updateMarkers();assert(!classes(p).includes('compact'),'선택 시 상세');
const group=world.staff.filter(row=>row.id!==p.id&&row.id!=='owner').slice(0,7);
for(const row of group){row.x=p.x;row.y=p.y;row.level=p.level}
scene.view.yaw+=.001;scene.updateMarkers();
assert(group.some(row=>labelNodes.get(row.id).hidden),'자리가 없으면 이름표를 숨겨야 합니다');
const rects=[...scene.markerRects.values()];
for(let i=0;i<rects.length;i++)for(let j=i+1;j<rects.length;j++){
	const a=rects[i],b=rects[j];
	assert(!(a.x<b.x+b.w&&a.x+a.w>b.x&&a.y<b.y+b.h&&a.y+a.h>b.y),'이름표가 겹칩니다');
}
for(const row of world.staff)assert.equal(scene.markerRects.has(row.id),!labelNodes.get(row.id).hidden,row.id);
'''
		env = dict(os.environ)
		env.pop("TEST_NO_WEBGL", None)
		env.pop("TEST_MOTION", None)
		result = subprocess.run([shutil.which("node"), "--no-warnings", "--input-type=module", "-e", script],
			cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
		self.assertEqual(result.returncode, 0, result.stderr[-3000:])

	def test_compact_styles_do_not_hide_expanded_labels_at_any_width(self):
		css = (ROOT / "office/static/office.css").read_text()
		rules = [(header.split(","), body) for header, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]
		def declarations(selector):
			return " ".join(body for selectors, body in rules if selector in [item.strip() for item in selectors])
		for part in ("status-bubble", "team-label"):
			self.assertRegex(declarations(".agent-marker.compact ." + part), r"display\s*:\s*none")
		for selector in (".agent-marker.compact .name-label", ".agent-marker.compact.distant .name-label"):
			style = declarations(selector)
			self.assertRegex(style, r"display\s*:\s*block")
			self.assertRegex(style, r"font-size\s*:\s*10px")
		for selectors, body in rules:
			if not re.search(r"display\s*:\s*none", body):
				continue
			for selector in selectors:
				if ".agent-marker" in selector and (".status-bubble" in selector or ".team-label" in selector):
					self.assertTrue(".compact" in selector or ".distant" in selector, selector)
