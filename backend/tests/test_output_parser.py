"""Quick smoke test for output_parser"""
import sys
sys.path.insert(0, ".")

from app.services.agent.output_parser import repair_json, parse_react_output

# JSON repair tests
print("=== JSON Repair ===")
print("trailing comma:", repair_json('{"key": "val",}'))
print("single quotes:", repair_json("{'key': 'val'}"))
print("unquoted keys:", repair_json('{key: "val"}'))
print("missing brace:", repair_json('{"key": "val"'))
print("plain text:", repair_json("hello world"))

# ReAct parsing tests
print("\n=== ReAct Parsing ===")
r1 = parse_react_output("Thought: I know\nFinal Answer: Python is great")
print(f"final: type={r1.type}, content={r1.content}")

r2 = parse_react_output('Thought: need time\nAction: datetime_now\nAction Input: {"tz": "UTC"}')
print(f"action: type={r2.type}, action={r2.action}, input={r2.action_input}")

r3 = parse_react_output("Thought: hmm\nAction: datetme_now\nAction Input: {}", ["datetime_now", "calc"])
print(f"fuzzy: action={r3.action} (should be datetime_now)")

r4 = parse_react_output("just plain text")
print(f"plain: type={r4.type}")

print("\nAll tests done!")
