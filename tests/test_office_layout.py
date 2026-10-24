"""브라우저 없이 방별 배치·좌석·외형 보존과 픽셀 그리기를 확인한다."""

import hashlib
import json
import shutil
import subprocess
import unittest
from itertools import combinations
from pathlib import Path


STATIC = Path(__file__).resolve().parents[1] / "office/static"


def digest(value):
	return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def overlaps(a, b):
	return a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]


def bounds(prop):
	x, y = prop["x"], prop["y"]
	if prop["type"] == "desk":
		return dict(x=x, y=y-16, w=64, h=84)
	if prop["type"] == "bossdesk":
		return dict(x=x, y=y-25, w=prop["w"], h=prop["h"]+32)
	return dict(x=x, y=y, w=prop["w"], h=prop["h"]+(8 if prop["type"] == "longtable" else 5 if prop["type"] == "workbench" else 0))


HARNESS = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const root=process.argv[1],scene=JSON.parse(fs.readFileSync(root+'/scene.json','utf8'));
function context(){
	let transform=[1,1,0,0],stack=[];
	return {rects:[],fillStyle:'',save(){stack.push([...transform])},restore(){transform=stack.pop()},
		translate(x,y){transform[2]+=x*transform[0];transform[3]+=y*transform[1]},scale(x,y){transform[0]*=x;transform[1]*=y},
		beginPath(){},rect(){},clip(){},fillRect(x,y,w,h){
			assert([x,y,w,h].every(Number.isFinite),'그리기 좌표는 유한한 수여야 합니다');
			const [sx,sy,tx,ty]=transform;this.rects.push([x*sx+tx,y*sy+ty,w*sx,h*sy,this.fillStyle]);
		}};
}
let surface;
const sandbox={document:{createElement(){surface=context();return {getContext:()=>surface}}}};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(root+'/pixel-art.js','utf8'),sandbox);
const render=props=>{sandbox.createPixelArt({...scene,width:600,height:400,rooms:[],paths:[],water:[],props});return surface.rects};
const empty=render([]).length;
for(const type of ['cubicle','longtable','workbench','bossdesk']){
	const prop=scene.props.find(p=>p.type===type),linked=scene.props.filter(p=>p.type==='chair'&&(prop.seats||[]).includes(p.seat));
	const rects=render([prop,...linked]).slice(empty);
	assert(rects.length>10,type+' 소품이 그려져야 합니다');
	const monitors=rects.filter(r=>r[2]===34&&r[3]===23&&r[4]==='#263836');
	assert.equal(monitors.length,type==='cubicle'?1:type==='workbench'?prop.monitors:type==='bossdesk'?2:0);
	if(type==='longtable')assert.equal(rects.filter(r=>r[2]===24&&r[3]===20).length,prop.seats.length);
}
const cubicle=scene.props.find(p=>p.type==='cubicle');
assert.notDeepEqual(render([{...cubicle,direction:'up'}]),render([{...cubicle,direction:'down'}]));
const art=sandbox.createPixelArt(scene);
function pixels(actor){
	const target=context();art.drawActor(target,actor,0);const output=new Map();
	for(const [x,y,w,h,color] of target.rects){
		for(let yy=Math.floor(Math.min(y,y+h));yy<Math.ceil(Math.max(y,y+h));yy++)
			for(let xx=Math.floor(Math.min(x,x+w));xx<Math.ceil(Math.max(x,x+w));xx++)output.set(xx+','+yy,color);
	}
	return [...output.entries()].sort();
}
for(const person of scene.people){
	const actor={...person,x:50,y:70,face:'down',walking:false};
	assert.notDeepEqual(pixels({...actor,facing:'back'}),pixels({...actor,facing:'front'}));
	assert.deepEqual(pixels({...actor,facing:'back'}),pixels({...actor,facing:undefined,face:'up'}));
}
"""


class OfficeLayoutTest(unittest.TestCase):
	def setUp(self):
		self.scene = json.loads((STATIC / "scene.json").read_text())
		self.rooms = {r["id"]: r for r in self.scene["rooms"]}
		self.people = {p["seat"]: p for p in self.scene["people"]}
		self.seats = [p for p in self.scene["props"] if "seat" in p]

	def assert_inside(self, inner, outer):
		self.assertGreaterEqual(inner["x"], outer["x"])
		self.assertGreaterEqual(inner["y"], outer["y"])
		self.assertLessEqual(inner["x"] + inner["w"], outer["x"] + outer["w"])
		self.assertLessEqual(inner["y"] + inner["h"], outer["y"] + outer["h"])

	def test_every_person_and_unique_matching_seat_are_in_their_room(self):
		self.assertEqual(sorted(p["seat"] for p in self.seats), list(range(1, 27)))
		for seat, person in self.people.items():
			with self.subTest(seat=seat):
				room = self.rooms[person["department"]]
				self.assert_inside(dict(x=person["x"]-18, y=person["y"]-70, w=36, h=72), room)
				prop, = [p for p in self.seats if p["seat"] == seat]
				self.assert_inside(bounds(prop), room)
				self.assertFalse(prop.get("spare", False))
				if prop["type"] == "chair":
					expected = (prop["x"]+12, prop["y"]+25)
				elif prop["type"] == "desk":
					expected = (prop["x"]+32, prop["y"]+62)
				elif prop["type"] == "bossdesk":
					expected = (prop["x"]+prop["w"]/2, prop["y"])
				else:
					expected = (prop["x"]+prop["w"]/2, prop["y"]+(prop["h"]-9 if prop["direction"] == "up" else 35))
				self.assertEqual((person["x"], person["y"]), expected)

	def test_people_leave_horizontal_or_vertical_space_for_nameplates(self):
		for a, b in combinations(self.people.values(), 2):
			with self.subTest(seats=(a["seat"], b["seat"])):
				self.assertFalse(abs(a["x"]-b["x"]) < 90 and abs(a["y"]-b["y"]) < 40)

	def test_furniture_and_spares_leave_paths_and_room_entrances_open(self):
		furniture = [p for p in self.scene["props"] if "seat" in p or p["type"] in ("longtable", "workbench")]
		for prop in furniture:
			with self.subTest(prop=prop):
				box = bounds(prop)
				room, = [r for r in self.rooms.values() if r["x"] <= prop["x"] < r["x"]+r["w"] and r["y"] <= prop["y"] < r["y"]+r["h"]]
				self.assert_inside(box, room)
				for path in self.scene["paths"]:
					self.assertFalse(overlaps(box, path))
				for r in self.rooms.values():
					entrance = dict(x=r["x"]+r["w"]/2-32, y=r["y"]+r["h"]-30, w=64, h=50)
					self.assertFalse(overlaps(box, entrance))
		spares = [p for p in self.seats if p.get("spare") is True]
		self.assertEqual(len(spares), 3)
		self.assertEqual({p["seat"] for p in spares}, {24, 25, 26})
		self.assertEqual(len({next(r["id"] for r in self.rooms.values() if r["x"] <= p["x"] < r["x"]+r["w"] and r["y"] <= p["y"] < r["y"]+r["h"]) for p in spares}), 3)

	def test_rooms_use_boss_facing_pair_cubicle_rows_table_and_wall_bench(self):
		props = self.scene["props"]
		boss, = [p for p in props if p["type"] == "bossdesk"]
		self.assertEqual(boss["seat"], 1)
		self.assertEqual(self.people[1]["facing"], "front")
		ops_table, = [p for p in props if p["type"] == "longtable" and p["seats"] == [2, 3]]
		self.assertLess(boss["y"], ops_table["y"])
		self.assertEqual([self.people[s]["facing"] for s in (2, 3)], ["front", "back"])
		self.assertEqual(self.people[2]["x"], self.people[3]["x"])
		self.assertLess(self.people[2]["y"], ops_table["y"]+ops_table["h"]/2)
		self.assertGreater(self.people[3]["y"], ops_table["y"]+ops_table["h"])
		dev = [p for p in self.seats if p["seat"] in (4, 5, 6, 7, 8, 24)]
		self.assertEqual({p["type"] for p in dev}, {"cubicle"})
		for direction, seats, facing in (("up", (4, 5, 6), "back"), ("down", (7, 8), "front")):
			row = [p for p in dev if p["direction"] == direction]
			self.assertEqual(len(row), 3)
			self.assertEqual(len({p["y"] for p in row}), 1)
			for seat in seats:
				self.assertEqual(self.people[seat]["facing"], facing)
		blog_table, = [p for p in props if p["type"] == "longtable" and len(p["seats"]) == 5]
		for seats, facing in (((9, 10, 11), "front"), ((12, 23), "back")):
			self.assertEqual(len({self.people[s]["y"] for s in seats}), 1)
			for seat in seats:
				self.assertEqual(self.people[seat]["facing"], facing)
				self.assertEqual(self.people[seat]["y"] < blog_table["y"]+blog_table["h"]/2, facing == "front")
		bench, = [p for p in props if p["type"] == "workbench"]
		self.assertLessEqual(bench["y"]-self.rooms["shorts"]["y"], 50)
		self.assertGreaterEqual(bench["monitors"], 3)
		self.assertEqual(len({self.people[s]["y"] for s in (13, 14, 15)}), 1)
		self.assertTrue(all(self.people[s]["facing"] == "back" for s in (13, 14, 15)))
		company = [p for p in self.seats if p["seat"] in (16, 17, 18, 19, 26)]
		self.assertEqual({p["type"] for p in company}, {"cubicle"})
		self.assertEqual({p["direction"] for p in company}, {"up"})
		self.assertEqual(len({p["y"] for p in company}), 1)
		self.assertTrue(all(self.people[s]["facing"] == "back" for s in (16, 17, 18, 19)))

	def test_original_identity_appearance_room_rectangles_and_common_rooms_are_preserved(self):
		keys = ("id", "name", "department", "seat", "appearance", "hair", "shirt", "identity")
		original = [{key:p[key] for key in keys} for p in self.scene["people"][:22]]
		self.assertEqual(digest(original), "666cc9593579c0440ed0ac7f269680b69f6126934aae18bf97e136ec1bb3f627")
		self.assertEqual(digest(self.scene["rooms"]), "8dfa0293965dd22c6017760b8da31df61796840847e6d0de89e44ac61b9638ce")
		fixed_rooms = [self.rooms[key] for key in ("lounge", "meeting", "engines")]
		fixed_props = [{k:v for k,v in p.items() if k != "spare"} for p in self.scene["props"]
			if any(r["x"] <= p["x"] < r["x"]+r["w"] and r["y"] <= p["y"] < r["y"]+r["h"] for r in fixed_rooms)]
		self.assertEqual(digest(fixed_props), "c8e6b0130211323cd1d190408032a574db9f7f6d7173d9b10ac738a8dfd88aae")
		self.assertEqual([(p["seat"], p["x"], p["y"]) for p in self.scene["people"] if p["department"] == "lounge"],
			[(20, 582, 172), (21, 707, 172), (22, 832, 172)])

	@unittest.skipUnless(shutil.which("node"), "JavaScript 실행기 없음")
	def test_new_props_and_distinct_back_pixels_without_browser(self):
		result = subprocess.run([shutil.which("node"), "-e", HARNESS, str(STATIC)], capture_output=True, text=True, timeout=10)
		self.assertEqual(result.returncode, 0, result.stderr)
