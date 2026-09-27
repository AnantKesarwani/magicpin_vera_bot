from fastapi.testclient import TestClient
from app.bot import app

c = TestClient(app)
category = {"slug":"dentists","offer_catalog":[{"title":"Dental Cleaning @ ₹299"}],"voice":{"tone":"peer_clinical"}}
merchant = {"merchant_id":"m_001","category_slug":"dentists","identity":{"name":"Dr. Meera's Dental Clinic","owner_first_name":"Meera","city":"Delhi","locality":"Lajpat Nagar","languages":["en","hi"]},"performance":{"views":2410,"calls":18,"ctr":0.021,"delta_7d":{"calls_pct":-0.50}},"offers":[{"title":"Dental Cleaning @ ₹299","status":"active"}],"signals":[]}
trigger = {"id":"trg_1","scope":"merchant","kind":"perf_dip","merchant_id":"m_001","payload":{"metric":"calls","delta_pct":-0.50,"vs_baseline":36},"urgency":4,"suppression_key":"perf:m001:calls:w1","expires_at":"2099-01-01T00:00:00Z"}

assert c.post('/v1/context',json={"scope":"category","context_id":"dentists","version":1,"payload":category,"delivered_at":"2026-01-01T00:00:00Z"}).status_code == 200
assert c.post('/v1/context',json={"scope":"merchant","context_id":"m_001","version":1,"payload":merchant,"delivered_at":"2026-01-01T00:00:00Z"}).status_code == 200
assert c.post('/v1/context',json={"scope":"trigger","context_id":"trg_1","version":1,"payload":trigger,"delivered_at":"2026-01-01T00:00:00Z"}).status_code == 200
r = c.post('/v1/tick',json={"now":"2026-01-01T00:00:00Z","available_triggers":["trg_1"]})
assert r.status_code == 200 and len(r.json()['actions']) == 1
assert '50%' in r.json()['actions'][0]['body']
print('LOCAL TEST PASSED')
print(r.json()['actions'][0])
