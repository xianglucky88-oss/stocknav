
from collections import defaultdict, Counter
def _ident(rec):
    nm = str(rec.get("name", ""))
    return nm.split()[0].lower() if nm else str(rec.get("id"))
def process(record):
    return {"merge_key": _ident(record), "src_value": record.get("value")}
def reduce(records):
    by = defaultdict(list)
    for r in records:
        by[r["merge_key"]].append(r)
    for rows in by.values():
        vals = Counter(str(r.get("value")) for r in rows)
        top, hits = vals.most_common(1)[0]
        conflict = int(len(vals) > 1)
        for r in rows:
            r["reconciled_value"] = top
            r["consensus_hits"] = hits
            r["conflict"] = conflict
    return records
