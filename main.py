import os, importlib
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel
from falkordb import FalkorDB

_g = FalkorDB(host="localhost", port=6379).select_graph("traceback")

agent = importlib.import_module("agent_gemini" if os.getenv("AGENT", "gemini") == "gemini" else "agent")
app = FastAPI(title="Traceback")

@app.get("/api/stats")
def stats():
    nodes = {r[0]: r[1] for r in _g.query("MATCH (n) RETURN labels(n)[0], count(n)").result_set}
    edges = _g.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]
    return {"nodes": nodes, "edges": edges}

class Q(BaseModel):
    question: str

@app.post("/api/ask")
def ask(body: Q):
    try:
        r = agent.ask(body.question)
    except Exception as e:
        return {"answer": f"Agent error: {e}", "tools": [], "edges": []}
    seen, edges = set(), []
    for e in r["path"]:
        k = (e["from"], e["rel"], e["to"])
        if k not in seen:
            seen.add(k)
            edges.append(e)
    tools = [{"tool": t["tool"], "input": t["input"]} for t in r["trace"]]
    return {"answer": r["answer"], "tools": tools, "edges": edges, "results": r["trace"]}

@app.get("/")
def index():
    return FileResponse("index.html")
