from ingest import gh, q, REPO

files = [r[0] for r in q("MATCH (f:File)-[:PART_OF]->(:Module) RETURN f.path").result_set]
for path in files:
    commits = gh(f"/repos/{REPO}/commits", path=path, per_page=100)
    counts = {}
    for c in commits:
        a = c.get("author")
        if a and a.get("login"):
            counts[a["login"]] = counts.get(a["login"], 0) + 1
    for login, n in counts.items():
        q("MATCH (f:File {path:$p}) MERGE (u:Person {login:$u}) "
          "MERGE (u)-[r:COMMITTED]->(f) SET r.count=$n", p=path, u=login, n=n)
    print(f"{path}: {len(commits)} commits, {len(counts)} people")
print("DONE")
