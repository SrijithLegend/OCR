"""Build bike_parts.db: Royal Enfield Classic 350 parts (part_id, part_name, part_description, part_attribute, part_price).
30% of the rows get a WRONG id plus the price that belongs to that other id (a white seat carrying the yellow seat's id and
price). The honest answers go to bike_parts_truth.csv, never into the db, so the fixer can be scored.
Run: python gen_bikes.py   (seeded, same output every time). Prices are rough INR retail figures."""
import csv, itertools, random, sqlite3
from bikeparts import make_id

# (name, base price, description, {attribute: [(option, price delta)]})
CAT = [
 ("Seat Assembly", 3200, "Dual rider seat for the Classic 350", {"Colour": [("Black", 0), ("Brown", 300), ("Tan", 300), ("White", 500), ("Yellow", 500)]}),
 ("Pillion Seat Cushion", 1100, "Pillion rider cushion pad", {"Colour": [("Black", 0), ("Brown", 100)]}),
 ("Front Brake Pad Set", 650, "Disc brake pads, front", {"Material": [("Organic", 0), ("Sintered", 250)]}),
 ("Rear Brake Shoe Set", 520, "Drum brake shoes, rear", {"Material": [("Organic", 0), ("Semi-metallic", 120)]}),
 ("Front Brake Disc", 1850, "Front disc rotor", {"Diameter": [("300 mm", 0), ("280 mm", -150)]}),
 ("Brake Caliper", 2900, "Front brake caliper assembly", {"Finish": [("Black", 0), ("Chrome", 400)]}),
 ("Engine Oil Filter", 210, "Spin-on oil filter", {"Type": [("OEM", 0), ("Washable", 140)]}),
 ("Air Filter Element", 380, "Air cleaner element", {"Type": [("Paper", 0), ("Foam", 120), ("Cotton gauze", 450)]}),
 ("Spark Plug", 240, "Spark plug for the 349 cc engine", {"Type": [("Standard", 0), ("Iridium", 520)]}),
 ("Piston Kit", 2300, "Piston, rings and pin", {"Bore": [("Standard", 0), ("+0.25 mm", 150), ("+0.50 mm", 150)]}),
 ("Cylinder Head Gasket", 330, "Head gasket", {"Material": [("Composite", 0), ("Copper", 280)]}),
 ("Clutch Plate Set", 1450, "Friction and steel clutch plates", {"Type": [("Standard", 0), ("Heavy duty", 600)]}),
 ("Clutch Cable", 290, "Clutch cable", {"Length": [("Standard", 0), ("Long", 90)]}),
 ("Throttle Cable", 270, "Throttle cable", {"Length": [("Standard", 0), ("Long", 90)]}),
 ("Brake Lever", 420, "Hand brake lever", {"Finish": [("Black", 0), ("Chrome", 160), ("Silver", 100)]}),
 ("Clutch Lever", 410, "Hand clutch lever", {"Finish": [("Black", 0), ("Chrome", 160), ("Silver", 100)]}),
 ("Headlight Bulb", 280, "Headlight bulb", {"Type": [("Halogen", 0), ("LED", 650)]}),
 ("Tail Lamp Assembly", 1250, "Tail lamp with bulb holder", {"Lens": [("Red", 0), ("Smoked", 200)]}),
 ("Indicator Lamp", 480, "Turn indicator", {"Lens": [("Amber", 0), ("Clear", 60)], "Position": [("Front", 0), ("Rear", 0)]}),
 ("Battery", 2400, "Maintenance-free battery", {"Rating": [("12 V 8 Ah", 0), ("12 V 9 Ah", 400)]}),
 ("Horn", 450, "Electric horn", {"Finish": [("Black", 0), ("Chrome", 150)]}),
 ("Regulator Rectifier", 1900, "Voltage regulator rectifier", {"Type": [("OEM", 0), ("Upgrade", 500)]}),
 ("Front Tyre", 2700, "Front tyre", {"Size": [("90/90-19", 0), ("100/90-19", 250)], "Pattern": [("Tubeless", 0), ("Tube type", -200)]}),
 ("Rear Tyre", 3300, "Rear tyre", {"Size": [("120/80-18", 0), ("110/80-18", -200)], "Pattern": [("Tubeless", 0), ("Tube type", -200)]}),
 ("Inner Tube", 380, "Inner tube", {"Wheel": [("19 inch front", 0), ("18 inch rear", 40)]}),
 ("Drive Chain", 1450, "Drive chain", {"Links": [("108", 0), ("112", 60)], "Finish": [("Steel", 0), ("Gold", 300)]}),
 ("Front Sprocket", 480, "Engine sprocket", {"Teeth": [("15T", 0), ("16T", 30)]}),
 ("Rear Sprocket", 780, "Rear wheel sprocket", {"Teeth": [("42T", 0), ("45T", 30)]}),
 ("Fork Oil Seal", 260, "Front fork oil seal", {"Type": [("Standard", 0), ("Dust-sealed", 120)]}),
 ("Rear Shock Absorber", 3600, "Rear shock absorber pair", {"Finish": [("Black", 0), ("Chrome", 450)]}),
 ("Rear View Mirror", 640, "Rear view mirror", {"Side": [("Left", 0), ("Right", 0)], "Finish": [("Chrome", 0), ("Black", 80)]}),
 ("Front Mudguard", 2100, "Front fender", {"Colour": [("Black", 0), ("Gunmetal", 300), ("Redditch Red", 300), ("Stealth Black", 300)]}),
 ("Fuel Tank", 9800, "Fuel tank with cap", {"Colour": [("Black", 0), ("Gunmetal Grey", 800), ("Signals Desert Sand", 1000), ("Redditch Red", 800)]}),
 ("Handlebar", 1650, "Handlebar", {"Finish": [("Black", 0), ("Chrome", 350)]}),
 ("Handlebar Grip Set", 360, "Pair of grips", {"Colour": [("Black", 0), ("Brown", 40)]}),
 ("Footrest", 520, "Rider footrest rubber and bracket", {"Finish": [("Black", 0), ("Chrome", 140)]}),
 ("Centre Stand", 1500, "Centre stand", {"Finish": [("Black", 0), ("Chrome", 280)]}),
 ("Side Stand", 640, "Side stand", {"Finish": [("Black", 0), ("Zinc", 90)]}),
 ("Silencer Assembly", 6800, "Exhaust silencer", {"Finish": [("Chrome", 0), ("Black", 600)]}),
 ("Engine Guard", 2400, "Crash guard", {"Finish": [("Black", 0), ("Chrome", 500)]}),
 ("Tank Badge", 430, "Fuel tank badge", {"Finish": [("Chrome", 0), ("Gold", 200)]}),
]


def build(path="bike_parts.db", truth="bike_parts_truth.csv", seed=7):
    rnd = random.Random(seed)
    parts = []  # [name, description, attributes, price, id]
    for name, base, desc, opts in CAT:
        for combo in itertools.product(*[[(k, v, d) for v, d in vs] for k, vs in opts.items()]):
            attrs = "; ".join(f"{k}: {v}" for k, v, _ in combo)
            parts.append([name, f"{desc}. {attrs.replace('; ', ', ')}.", attrs, base + sum(d for *_, d in combo), None])
    for i, p in enumerate(parts, 1):
        p[4] = make_id(p[0], p[2], i)
    # every part is listed once and 30% of them a second time, so some wrong rows have a correct twin in the db
    rows = parts + rnd.sample(parts, len(parts) * 3 // 10)
    rnd.shuffle(rows)
    wrong = set(rnd.sample(range(len(rows)), round(len(rows) * 0.3)))
    if path is None:
        return rows, wrong
    db = sqlite3.connect(path)
    db.executescript("DROP TABLE IF EXISTS parts; CREATE TABLE parts(part_id TEXT, part_name TEXT, part_description TEXT, part_attribute TEXT, part_price REAL)")
    tr = []
    for i, p in enumerate(rows):
        pid, price = p[4], p[3]
        if i in wrong:  # another part's id, with the price that goes with it
            o = rnd.choice([q for q in parts if q[4] != p[4]])
            pid, price = o[4], o[3]
        db.execute("INSERT INTO parts VALUES(?,?,?,?,?)", (pid, p[0], p[1], p[2], price))
        tr.append((i + 1, int(i in wrong), p[4], p[3]))
    db.commit(); db.close()
    with open(truth, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["rowid", "id_wrong", "true_id", "true_price"]); w.writerows(tr)
    return len(rows), len(wrong)


if __name__ == "__main__":
    n, w = build()
    print(f"bike_parts.db: {n} rows, {w} with a wrong id ({w / n:.0%}); answers in bike_parts_truth.csv")
