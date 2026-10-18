import os, sys, json
from google import genai
from google.genai import types
from tools import TOOLS

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.7-flash")

def decl(name, desc, props, required):
    return types.FunctionDeclaration(
        name=name, description=desc,
        parameters={"type": "OBJECT", "properties": props, "required": required})

FILE = {"file": {"type": "STRING", "description": "File name or path, e.g. 'app.py' or 'src/flask/sessions.py'"}}
DECLS = [
    decl("find_experts", "Who knows a file best. Ranks people by commits, authored PRs and reviews from the graph.", FILE, ["file"]),
    decl("blast_radius", "Which files import (directly or transitively) a file, i.e. what could break if it changes.", FILE, ["file"]),
    decl("riskiest_files", "Most depended-upon files in the codebase, ranked by PageRank over the import graph.",
         {"limit": {"type": "INTEGER"}}, []),
    decl("trace_decision", "History behind a file: the PRs that changed it and the issues they closed, with their text. Use for 'why' questions.", FILE, ["file"]),
    decl("path_between", "Shortest connection in the graph between two files or people.",
         {"a": {"type": "STRING"}, "b": {"type": "STRING"}}, ["a", "b"]),
]

SYSTEM = """You are Traceback, a friendly assistant that explains the pallets/flask codebase to people who may not be programmers. Every fact comes from a knowledge graph of files, people, pull requests and issues. Always call the graph tools; never answer from general knowledge of Flask. Write the final reply as at most 3 short plain-English sentences, like explaining to a friend: - No jargon, no lists, no tables, no formulas, no LaTeX, no code. Say "files that use it" instead of "imports", and "change request" instead of "pull request". - Lead with the direct answer (a name, a number, or the reason). - For "why" questions, only state a reason the PR/issue text supports. If the text does not say, say so. - Do not list evidence. The app shows detailed cards for people, files and sources below your answer. - Treat rankings as a good hint, not proof."""

CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM,
    tools=[types.Tool(function_declarations=DECLS)],
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    temperature=0.2)

def _clean(args):
    return {k: (int(v) if isinstance(v, float) and v.is_integer() else v) for k, v in dict(args).items()}

def ask(question, history=None, max_steps=6):
    contents = list(history or []) + [types.Content(role="user", parts=[types.Part(text=question)])]
    trace = []
    for _ in range(max_steps):
        resp = client.models.generate_content(model=MODEL, contents=contents, config=CONFIG)
        content = resp.candidates[0].content
        contents.append(content)
        calls = [p.function_call for p in content.parts if p.function_call]
        if not calls:
            answer = "".join(p.text or "" for p in content.parts if p.text)
            path = [e for t in trace for e in t["result"].get("path", [])]
            return {"answer": answer, "trace": trace, "path": path, "messages": contents}
        parts = []
        for fc in calls:
            args = _clean(fc.args)
            try:
                out = TOOLS[fc.name](**args)
            except Exception as e:
                out = {"error": str(e)}
            trace.append({"tool": fc.name, "input": args, "result": out})
            safe = json.loads(json.dumps(out, default=str))
            parts.append(types.Part.from_function_response(name=fc.name, response={"result": safe}))
        contents.append(types.Content(role="user", parts=parts))
    return {"answer": "Stopped after too many tool steps.", "trace": trace, "path": [], "messages": contents}

if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "Who knows app.py best, and what could break if I change it?"
    r = ask(q)
    print("\nTOOLS USED:", [(t["tool"], t["input"]) for t in r["trace"]])
    print("\nANSWER:\n", r["answer"])
    print("\nPATH EDGES:", len(r["path"]))
