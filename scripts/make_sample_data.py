"""Generate a SYNTHETIC OCDS release package that reproduces known real-world mess.
Nothing here is real. Replace with a real portal export for the actual demo."""
import json
import random
from pathlib import Path

random.seed(7)
CATS = {"goods": 1_200_000, "works": 8_000_000, "services": 2_500_000}
BUYERS = [("KE-B-001", "Demo County Government A"), ("KE-B-002", "Demo Hospital B"),
          ("KE-B-003", "Demo University C"), ("KE-B-004", None)]  # None = missing buyer name
SUPPLIERS = [(f"KE-S-{i:03d}", f"Demo Supplier {i} Ltd") for i in range(1, 13)]
rels = []
for i in range(1, 81):
    cat = random.choice(list(CATS))
    bid, bname = random.choice(BUYERS)
    value = round(CATS[cat] * random.lognormvariate(0, 0.5))
    year = random.choice([2024, 2025, 2026])
    t = {"title": f"Supply of {cat} lot {i}", "status": "complete", "mainProcurementCategory": cat,
         "procurementMethod": random.choice(["open", "limited", "selective"]),
         "tenderPeriod": {"endDate": f"{year}-{random.randint(1,12):02d}-{random.randint(1,28):02d}T12:00:00Z"},
         "value": {"amount": value, "currency": "KES"}}
    if random.random() < 0.6:
        t["eligibilityCriteria"] = "Valid tax compliance certificate; registered business; 2 similar contracts."
    if random.random() < 0.3:
        t["documents"] = [{"url": f"https://example.org/synthetic/tender-{i}.pdf"}]
    r = {"ocid": f"ocds-synth-{i:04d}", "buyer": {"id": bid, **({"name": bname} if bname else {})}, "tender": t, "awards": []}
    if random.random() < 0.85:
        sup = SUPPLIERS[0] if (bid == "KE-B-001" and random.random() < 0.7) else random.choice(SUPPLIERS)
        amt = round(value * random.uniform(0.8, 1.05))
        r["awards"].append({"id": f"a{i}", "date": t["tenderPeriod"]["endDate"],
                            "value": {"amount": amt, "currency": "KES"}, "suppliers": [{"id": sup[0], "name": sup[1]}]})
    rels.append(r)
rels[10]["awards"][0]["value"]["amount"] = 9_000_000_000_000 if rels[10]["awards"] else None  # extreme outlier
rels[20]["awards"] = [{"id": "a20", "value": {"amount": 500000, "currency": "KES"}, "suppliers": []}]  # no supplier
rels[30]["tender"]["tenderPeriod"] = {}  # no deadline
rels[30]["awards"] = []
from datetime import datetime, timedelta
wrng = random.Random(11)   # separate stream: adding windows must not reshuffle the rest of the sample
for i, r in enumerate(rels):
    end = (r["tender"].get("tenderPeriod") or {}).get("endDate")
    if not end or wrng.random() > 0.85:
        continue                                   # no end date, or start deliberately not recorded
    days = wrng.choice([7, 14, 21, 28, 30, 30, 35, 42, 45])
    if i in (45, 50, 60): days = 2                 # a few very short windows
    if i == 40: days = -5                          # start after the deadline: a data error seen in real feeds
    start = datetime.fromisoformat(end.replace("Z", "+00:00")) - timedelta(days=days)
    r["tender"]["tenderPeriod"]["startDate"] = start.isoformat().replace("+00:00", "Z")
Path(__file__).resolve().parent.parent.joinpath("data", "sample_ocds.json").write_text(
    json.dumps({"publisher": {"name": "SYNTHETIC - not real data"}, "releases": rels}, indent=1))
print(len(rels), "synthetic releases")
