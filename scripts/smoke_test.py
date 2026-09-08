import math, time, sys, datetime, httpx

BASE = "http://127.0.0.1:8009"
c = httpx.Client(base_url=BASE, timeout=30, follow_redirects=True)
P = F = 0

def step(name, ok, detail=""):
    global P, F
    ok = bool(ok)
    P += ok; F += (not ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))

def j(r):
    try: return r.json()
    except Exception: return r.text

# ---- 1. register A ----
ua = f"a_{int(time.time())}@ex.com"
r = c.post("/auth/register", json={"email": ua, "password": "secret1", "name": "Alice Green"})
step("register A", r.status_code == 200 and j(r)["data"].get("accessToken"), f"{r.status_code}")
ta = j(r)["data"]["accessToken"]; aid = j(r)["data"]["userId"]
Ha = {"Authorization": f"Bearer {ta}"}
step("register A -> avatar is data-uri svg", j(r)["data"]["avatarUrl"].startswith("data:image/svg+xml"))

# ---- 2. login A ----
r = c.post("/auth/login", json={"email": ua, "password": "secret1"})
step("login A", r.status_code == 200 and j(r)["data"]["userId"] == aid, f"{r.status_code}")
r = c.post("/auth/login", json={"email": ua, "password": "WRONG"})
step("login A wrong password -> 401", r.status_code == 401)

# ---- 3. me ----
r = c.get("/users/me", headers=Ha)
me = j(r)["data"]
step("GET /users/me", r.status_code == 200 and me["email"] == ua and me["visibility"] == "private", f"{r.status_code} vis={me.get('visibility')}")

# ---- 4. register B, privacy gate ----
ub = f"b_{int(time.time())}@ex.com"
r = c.post("/auth/register", json={"email": ub, "password": "secret1", "name": "Bob Blue"})
tb = j(r)["data"]["accessToken"]; Hb = {"Authorization": f"Bearer {tb}"}
r = c.get(f"/users/{aid}", headers=Hb)
step("B sees A (private) -> 403", r.status_code == 403, f"{r.status_code}")

# ---- 5. session ----
now = datetime.datetime.now(datetime.timezone.utc)
r = c.post("/sessions", headers=Ha, json={"startedAt": now.isoformat()})
step("create session", r.status_code == 200 and j(r)["data"].get("id"), f"{r.status_code}")
sid = j(r)["data"]["id"]

# ---- 6. points: ~40 m radius closed loop, 32 pts, ~6 km/h ----
clat, clng = 28.6139, 77.2090
R = 45.0
dlat = R / 111320.0
dlng = R / (111320.0 * math.cos(math.radians(clat)))
pts = []
N = 32
for i in range(N + 1):                     # +1 -> last point == first (closed)
    ang = 2 * math.pi * (i % N) / N
    t = now + datetime.timedelta(seconds=5 * i)
    pts.append({
        "lat": clat + dlat * math.sin(ang),
        "lng": clng + dlng * math.cos(ang),
        "speedKmh": 6.0, "bearing": 0.0, "seq": i,
        "recordedAt": t.isoformat(),
    })
r = c.post(f"/sessions/{sid}/points", headers=Ha, json={"points": pts})
step("batch upload points", r.status_code == 200 and j(r)["data"]["saved"] == len(pts), f"{r.status_code} {j(r)}")
# idempotent retry
r = c.post(f"/sessions/{sid}/points", headers=Ha, json={"points": pts})
step("re-upload points (idempotent) -> 200", r.status_code == 200, f"{r.status_code}")

# ---- 7. finish + claim ----
end = now + datetime.timedelta(seconds=5 * (N + 1))
r = c.post(f"/sessions/{sid}/finish", headers=Ha,
           json={"endedAt": end.isoformat(), "distanceM": 260.0, "claimTerritory": True})
d = j(r).get("data", {})
step("finish + claim territory", r.status_code == 200 and d.get("claimed") is True,
     f"{r.status_code} {d}")
tid = d.get("territoryId")
area = d.get("areaM2")
step("claimed area > 500 m^2", isinstance(area, (int, float)) and area > 500, f"area={area}")

# ---- 8. territories/mine ----
r = c.get("/territories/mine", headers=Ha)
d = j(r)["data"]
step("GET /territories/mine has the polygon", r.status_code == 200 and any(x["id"] == tid for x in d),
     f"{r.status_code} count={len(d)}")
step("territory geojson is a Polygon", d and d[0]["geojson"]["type"] == "Polygon")

# ---- 9. vector tile covering the area ----
z = 15
n = 2 ** z
xt = int((clng + 180.0) / 360.0 * n)
lat_rad = math.radians(clat)
yt = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
r = c.get(f"/tiles/territories/{z}/{xt}/{yt}.mvt")
step("GET vector tile -> 200", r.status_code == 200, f"{r.status_code} ct={r.headers.get('content-type')}")
step("vector tile is non-empty (contains our polygon)", len(r.content) > 0, f"{len(r.content)} bytes")
# cache hit second time
t0 = time.time(); r2 = c.get(f"/tiles/territories/{z}/{xt}/{yt}.mvt"); dt = (time.time() - t0) * 1000
step("vector tile cache hit", r2.status_code == 200 and r2.content == r.content, f"{dt:.1f} ms")

# ---- 10. todos ----
r = c.post("/todos", headers=Ha, json={"title": "morning run", "rating": 4})
step("create todo", r.status_code == 200 and j(r)["data"].get("id"), f"{r.status_code}")
todo_id = j(r)["data"]["id"]
r = c.get("/todos", headers=Ha)
d = j(r)["data"]
step("GET /todos has item + summary", r.status_code == 200 and d["summary"]["total"] >= 1,
     f"{r.status_code} summary={d.get('summary')}")
r = c.patch(f"/todos/{todo_id}", headers=Ha, json={"status": "complete"})
step("PATCH todo -> complete", r.status_code == 200 and j(r)["data"]["status"] == "complete", f"{r.status_code}")

# ---- 11. stats ----
r = c.get("/stats/summary", headers=Ha)
d = j(r).get("data", {})
step("GET /stats/summary after completing 1/1", r.status_code == 200 and float(d.get("dailyPct", 0)) == 100.0,
     f"{r.status_code} {d}")
r = c.get("/stats/daily", headers=Ha)
step("GET /stats/daily", r.status_code == 200 and isinstance(j(r)["data"], list), f"{r.status_code}")

# ---- 12. flip A public, B can now see ----
r = c.patch("/users/me", headers=Ha, json={"visibility": "public"})
step("PATCH /users/me visibility=public", r.status_code == 200 and j(r)["data"]["visibility"] == "public")
r = c.get(f"/users/{aid}", headers=Hb)
step("B sees A (now public) -> 200", r.status_code == 200, f"{r.status_code}")
step("public view hides contact", "contact" not in j(r)["data"], f"keys={list(j(r)['data'].keys())}")

print(f"\n==== {P} passed, {F} failed ====")
sys.exit(1 if F else 0)
