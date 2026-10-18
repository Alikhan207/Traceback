import ast, os, re, subprocess, pathlib, requests
from falkordb import FalkorDB

REPO = os.getenv("REPO", "pallets/flask")
N_PRS = int(os.getenv("N_PRS", "5"))
TOKEN = os.environ.get("GITHUB_TOKEN")
if not TOKEN:
    raise SystemExit("GITHUB_TOKEN is not set. Do Step 7 first.")
HEAD = {"Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json"}
g = FalkorDB(host="localhost", port=6379).select_graph("traceback")

import time
SESSION = requests.Session()
SESSION.headers.update(HEAD)

def gh(endpoint, **params):
    for attempt in range(6):
        try:
            r = SESSION.get(f"https://api.github.com{endpoint}", params=params, timeout=30)
            if r.status_code in (403, 429) and r.headers.get("X-RateLimit-Remaining") == "0":
                wait = max(int(r.headers.get("X-RateLimit-Reset", time.time() + 60)) - time.time(), 5)
                print(f"rate limited, sleeping {int(wait)}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.ConnectionError, requests.Timeout) as e:
            print(f"network hiccup ({type(e).__name__}), retry {attempt + 1}/6")
            time.sleep(2 ** attempt)
    raise RuntimeError(f"GitHub request failed after retries: {endpoint}")

def q(cypher, **p):
    return g.query(cypher, p)

# ---------- 1. Files, modules, import graph (from real source) ----------
def load_code():
    dest = pathlib.Path("repo_clone")
    if not dest.exists():
        subprocess.run(["git", "clone", "--depth", "1",
                        f"https://github.com/{REPO}.git", str(dest)], check=True)
    files = {}  # dotted module name -> repo-relative path
    for p in dest.glob("src/**/*.py"):
        rel = p.relative_to(dest).as_posix()
        dotted = ".".join(p.relative_to(dest / "src").with_suffix("").parts)
        dotted = dotted.removesuffix(".__init__")
        files[dotted] = rel
    for dotted, rel in files.items():
        mod = rel.rsplit("/", 1)[0]
        q("MERGE (f:File {path:$p}) SET f.name=$n "
          "MERGE (m:Module {name:$m}) MERGE (f)-[:PART_OF]->(m)",
          p=rel, n=rel.split("/")[-1], m=mod)
    for dotted, rel in files.items():
        tree = ast.parse((dest / rel).read_text(encoding="utf-8"))
        pkg = dotted.split(".") if rel.endswith("__init__.py") else dotted.split(".")[:-1]
        targets = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = pkg[: len(pkg) - (node.level - 1)] if node.level else []
                mod = ".".join(base + ([node.module] if node.module else []))
                names = [mod] + [f"{mod}.{a.name}" for a in node.names]
            else:
                continue
            for n in names:
                if n in files and files[n] != rel:
                    targets.add(files[n])
        for t in targets:
            q("MATCH (a:File {path:$a}), (b:File {path:$b}) "
              "MERGE (a)-[:IMPORTS]->(b)", a=rel, b=t)
    print(f"code graph: {len(files)} files")

# ---------- 2. PRs, people, reviews, linked issues ----------
CLOSES = re.compile(r"(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s+#(\d+)", re.I)

def load_history():
    done, page = 0, 1
    while done < N_PRS:
        prs = gh(f"/repos/{REPO}/pulls", state="closed", per_page=100, page=page)
        if not prs:
            break
        for pr in prs:
            if not pr.get("merged_at"):
                continue
            n = pr["number"]
            if q("MATCH (:PR {number:$n})-[:MODIFIES]->() RETURN count(*)", n=n).result_set[0][0] > 0:
                done += 1
                print(f"PR #{n} already loaded ({done}/{N_PRS})")
                if done >= N_PRS:
                    break
                continue
            q("MERGE (p:PR {number:$n}) SET p.title=$t, p.body=$b, p.url=$u, p.merged_at=$m "
              "MERGE (a:Person {login:$a}) MERGE (a)-[:AUTHORED]->(p)",
              n=n, t=pr["title"], b=(pr["body"] or "")[:4000],
              u=pr["html_url"], m=pr["merged_at"], a=pr["user"]["login"])
            for f in gh(f"/repos/{REPO}/pulls/{n}/files", per_page=100):
                q("MATCH (p:PR {number:$n}) MERGE (f:File {path:$f}) "
                  "MERGE (p)-[:MODIFIES]->(f)", n=n, f=f["filename"])
            for r in gh(f"/repos/{REPO}/pulls/{n}/reviews", per_page=100):
                if r.get("user"):
                    q("MATCH (p:PR {number:$n}) MERGE (u:Person {login:$u}) "
                      "MERGE (u)-[:REVIEWED]->(p)", n=n, u=r["user"]["login"])
            for i in set(CLOSES.findall(pr["body"] or "")):
                try:
                    iss = gh(f"/repos/{REPO}/issues/{i}")
                except requests.HTTPError:
                    continue
                q("MATCH (p:PR {number:$n}) "
                  "MERGE (i:Issue {number:$i}) SET i.title=$t, i.body=$b, i.url=$u "
                  "MERGE (p)-[:CLOSES]->(i) "
                  "MERGE (a:Person {login:$a}) MERGE (i)-[:RAISED_BY]->(a)",
                  n=n, i=int(i), t=iss["title"], b=(iss["body"] or "")[:4000],
                  u=iss["html_url"], a=iss["user"]["login"])
            done += 1
            print(f"PR #{n} ({done}/{N_PRS})")
            if done >= N_PRS:
                break
        page += 1

if __name__ == "__main__":
    for idx in ("File(path)", "PR(number)", "Person(login)"):
        label, prop = idx.split("(")
        try:
            q(f"CREATE INDEX FOR (n:{label}) ON (n.{prop[:-1]})")
        except Exception as e:
            print("index skipped:", e)
    load_code()
    load_history()
    print("DONE")