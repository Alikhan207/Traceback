import json
from falkordb import FalkorDB

g = FalkorDB(host="localhost", port=6379).select_graph("traceback")

def q(cypher, **p):
    return g.query(cypher, p).result_set

def is_bot(login):
    return "[bot]" in login

def resolve_file(name):
    """Accepts 'app.py' or 'src/flask/app.py'; returns the full path in the graph."""
    rows = q("MATCH (f:File) WHERE f.path = $n OR f.path ENDS WITH $s RETURN f.path",
             n=name, s="/" + name.lstrip("/"))
    if not rows:
        return None
    return sorted((r[0] for r in rows), key=len)[0]

# 1 ---------------------------------------------------------------
def find_experts(file, limit=5):
    """Who knows this file? Score = commits*1 + authored PRs touching it*3 + reviewed PRs*2."""
    f = resolve_file(file)
    if not f:
        return {"error": f"file not found: {file}"}
    people = {}
    def P(l):
        return people.setdefault(l, {"login": l, "commits": 0, "authored_prs": [], "reviewed_prs": []})
    for login, n in q("MATCH (u:Person)-[c:COMMITTED]->(:File {path:$p}) RETURN u.login, c.count", p=f):
        if not is_bot(login):
            P(login)["commits"] = n
    for login, nums in q("MATCH (u:Person)-[:AUTHORED]->(pr:PR)-[:MODIFIES]->(:File {path:$p}) "
                         "RETURN u.login, collect(pr.number)", p=f):
        if not is_bot(login):
            P(login)["authored_prs"] = nums
    for login, nums in q("MATCH (u:Person)-[:REVIEWED]->(pr:PR)-[:MODIFIES]->(:File {path:$p}) "
                         "RETURN u.login, collect(pr.number)", p=f):
        if not is_bot(login):
            P(login)["reviewed_prs"] = nums
    for v in people.values():
        v["score"] = v["commits"] + 3 * len(v["authored_prs"]) + 2 * len(v["reviewed_prs"])
    top = sorted(people.values(), key=lambda v: -v["score"])[:limit]
    path = []
    for v in top:
        if v["commits"]:
            path.append({"from": v["login"], "rel": "COMMITTED", "to": f})
        for n in v["authored_prs"][:3]:
            path += [{"from": v["login"], "rel": "AUTHORED", "to": f"PR #{n}"},
                     {"from": f"PR #{n}", "rel": "MODIFIES", "to": f}]
        for n in v["reviewed_prs"][:3]:
            path.append({"from": v["login"], "rel": "REVIEWED", "to": f"PR #{n}"})
    return {"tool": "find_experts", "file": f, "experts": top, "path": path,
            "method": "commits x1 + authored PRs x3 + reviewed PRs x2, bots excluded"}

# 2 ---------------------------------------------------------------
def blast_radius(file):
    """Which files transitively import this one (up to 4 hops)?"""
    f = resolve_file(file)
    if not f:
        return {"error": f"file not found: {file}"}
    rows = q("MATCH p=(d:File)-[:IMPORTS*1..4]->(:File {path:$p}) WHERE d.path <> $p "
             "RETURN d.path AS dep, min(length(p)) AS hops ORDER BY hops, dep", p=f)
    direct = q("MATCH (d:File)-[:IMPORTS]->(:File {path:$p}) RETURN d.path", p=f)
    return {"tool": "blast_radius", "file": f,
            "affected": [{"file": r[0], "hops": r[1]} for r in rows],
            "path": [{"from": d[0], "rel": "IMPORTS", "to": f} for d in direct]}

# 3 ---------------------------------------------------------------
def riskiest_files(limit=5):
    """Most depended-upon files via PageRank over IMPORTS (falls back to in-degree)."""
    try:
        rows = q("CALL algo.pageRank('File','IMPORTS') YIELD node, score "
                 "RETURN node.path, score ORDER BY score DESC LIMIT $l", l=limit)
        method = "PageRank over IMPORTS"
    except Exception:
        rows = q("MATCH (f:File)<-[:IMPORTS]-(d:File) RETURN f.path, count(d) AS score "
                 "ORDER BY score DESC LIMIT $l", l=limit)
        method = "in-degree over IMPORTS (PageRank unavailable)"
    return {"tool": "riskiest_files", "method": method,
            "files": [{"file": r[0], "score": round(float(r[1]), 4)} for r in rows],
            "path": []}

# 4 ---------------------------------------------------------------
def trace_decision(file, limit=8):
    """File -> PRs that changed it -> issues they closed, with the text that explains why."""
    f = resolve_file(file)
    if not f:
        return {"error": f"file not found: {file}"}
    rows = q("MATCH (:File {path:$p})<-[:MODIFIES]-(pr:PR) "
             "OPTIONAL MATCH (a:Person)-[:AUTHORED]->(pr) "
             "OPTIONAL MATCH (pr)-[:CLOSES]->(i:Issue) "
             "RETURN pr.number, pr.title, pr.url, pr.merged_at, pr.body, a.login, "
             "i.number, i.title, i.url, i.body ORDER BY pr.merged_at DESC", p=f)
    rows = sorted(rows, key=lambda r: r[6] is None)[:limit]   # PRs with a linked issue first
    out, path = [], []
    for r in rows:
        item = {"pr": r[0], "title": r[1], "url": r[2], "merged_at": r[3],
                "description": (r[4] or "")[:500], "author": r[5]}
        path += [{"from": f"PR #{r[0]}", "rel": "MODIFIES", "to": f}]
        if r[6] is not None:
            item["issue"] = {"number": r[6], "title": r[7], "url": r[8], "text": (r[9] or "")[:500]}
            path.append({"from": f"PR #{r[0]}", "rel": "CLOSES", "to": f"Issue #{r[6]}"})
        out.append(item)
    return {"tool": "trace_decision", "file": f, "history": out, "path": path}

# 5 ---------------------------------------------------------------
def _locate(name):
    """Return (label, prop, value) for a file path or a person login."""
    f = resolve_file(name)
    if f:
        return "File", "path", f
    rows = q("MATCH (n:Person {login:$n}) RETURN n.login", n=name)
    if rows:
        return "Person", "login", rows[0][0]
    return None

BOTS = ["dependabot[bot]", "pre-commit-ci[bot]"]

def path_between(a, b, max_hops=5):
    """Shortest connection between two files or people (any relationship, either direction)."""
    la, lb = _locate(a), _locate(b)
    if not la or not lb:
        return {"tool": "path_between", "found": False, "from": a, "to": b, "path": [],
                "error": "could not find one of the two names in the graph"}
    for k in range(1, max_hops + 1):
        rows = q(f"MATCH p=(x:{la[0]} {{{la[1]}:$a}})-[*{k}..{k}]-(y:{lb[0]} {{{lb[1]}:$b}}) "
                 "WHERE NOT any(n IN nodes(p) WHERE coalesce(n.login,'') IN $bots) "
                 "RETURN p LIMIT 1", a=la[2], b=lb[2], bots=BOTS)
        if rows:
            p = rows[0][0]
            def name(n):
                pr = n.properties
                return (pr.get("path") or pr.get("login")
                        or (f"PR #{pr['number']}" if n.labels[0] == "PR" else None)
                        or (f"Issue #{pr['number']}" if n.labels[0] == "Issue" else None)
                        or pr.get("name") or str(n.id))
            nodes = {n.id: name(n) for n in p.nodes()}
            path = [{"from": nodes[e.src_node], "rel": e.relation, "to": nodes[e.dest_node]}
                    for e in p.edges()]
            return {"tool": "path_between", "found": True, "from": la[2], "to": lb[2],
                    "hops": k, "path": path}
    return {"tool": "path_between", "found": False, "from": la[2], "to": lb[2], "path": [],
            "note": f"no connection within {max_hops} hops"}

TOOLS = {"find_experts": find_experts, "blast_radius": blast_radius,
         "riskiest_files": riskiest_files, "trace_decision": trace_decision,
         "path_between": path_between}

if __name__ == "__main__":
    tests = [("find_experts", ["app.py"]), ("blast_radius", ["globals.py"]),
             ("riskiest_files", []), ("trace_decision", ["sessions.py"]),
             ("path_between", ["app.py", "sessions.py"])]
    for name, args in tests:
        print("=" * 20, name, args)
        print(json.dumps(TOOLS[name](*args), indent=2, default=str)[:1800])
