import os, sys, json, boto3
from tools import TOOLS

REGION = os.getenv("AWS_REGION", "us-east-1")
MODEL_ID = os.getenv("BEDROCK_MODEL", "us.anthropic.claude-sonnet-5-5")
client = boto3.client("bedrock-runtime", region_name=REGION)

def spec(name, desc, props, required):
    return {"toolSpec": {"name": name, "description": desc, "inputSchema": {"json": {
        "type": "object", "properties": props, "required": required}}}}

FILE = {"file": {"type": "string", "description": "File name or path, e.g. 'app.py' or 'src/flask/sessions.py'"}}
TOOL_SPECS = [
    spec("find_experts", "Who knows a file best. Ranks people by commits, authored PRs and reviews from the graph.", FILE, ["file"]),
    spec("blast_radius", "Which files import (directly or transitively) a file, i.e. what could break if it changes.", FILE, ["file"]),
    spec("riskiest_files", "Most depended-upon files in the codebase, ranked by PageRank over the import graph.",
         {"limit": {"type": "integer"}}, []),
    spec("trace_decision", "History behind a file: the PRs that changed it and the issues they closed, with their text. Use for 'why' questions.", FILE, ["file"]),
    spec("path_between", "Shortest connection in the graph between two files or people.",
         {"a": {"type": "string"}, "b": {"type": "string"}}, ["a", "b"]),
]

SYSTEM = """You are Traceback, a friendly assistant that explains the pallets/flask codebase to people who may not be programmers. Every fact comes from a knowledge graph of files, people, pull requests and issues. Always call the graph tools; never answer from general knowledge of Flask. Write the final reply as at most 3 short plain-English sentences, like explaining to a friend: - No jargon, no lists, no tables, no formulas, no LaTeX, no code. Say "files that use it" instead of "imports", and "change request" instead of "pull request". - Lead with the direct answer (a name, a number, or the reason). - For "why" questions, only state a reason the PR/issue text supports. If the text does not say, say so. - Do not list evidence. The app shows detailed cards for people, files and sources below your answer. - Treat rankings as a good hint, not proof."""

def ask(question, history=None, max_steps=6):
    messages = list(history or []) + [{"role": "user", "content": [{"text": question}]}]
    trace = []
    for _ in range(max_steps):
        resp = client.converse(modelId=MODEL_ID, system=[{"text": SYSTEM}], messages=messages,
                               toolConfig={"tools": TOOL_SPECS}, inferenceConfig={"maxTokens": 1500})
        msg = resp["output"]["message"]
        messages.append(msg)
        if resp["stopReason"] != "tool_use":
            answer = "".join(b.get("text", "") for b in msg["content"])
            path = [e for t in trace for e in t["result"].get("path", [])]
            return {"answer": answer, "trace": trace, "path": path, "messages": messages}
        results = []
        for b in msg["content"]:
            if "toolUse" in b:
                tu = b["toolUse"]
                try:
                    out = TOOLS[tu["name"]](**tu["input"])
                except Exception as e:
                    out = {"error": str(e)}
                trace.append({"tool": tu["name"], "input": tu["input"], "result": out})
                results.append({"toolResult": {"toolUseId": tu["toolUseId"],
                                "content": [{"text": json.dumps(out, default=str)}]}})
        messages.append({"role": "user", "content": results})
    return {"answer": "Stopped after too many tool steps.", "trace": trace, "path": [], "messages": messages}

if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "Who knows app.py best, and what could break if I change it?"
    r = ask(q)
    print("\nTOOLS USED:", [(t["tool"], t["input"]) for t in r["trace"]])
    print("\nANSWER:\n", r["answer"])
    print("\nPATH EDGES:", len(r["path"]))
