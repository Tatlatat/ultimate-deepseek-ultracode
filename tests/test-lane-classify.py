import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location("gw", Path(__file__).resolve().parent.parent/"reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(spec); spec.loader.exec_module(gw)
def expect(c,m):
    if not c: raise SystemExit(f"FAIL: {m}")
def test():
    expect(gw.classify_lane_type(None, "Read ONLY foo.py and summarize its purpose")=="read", "read intent")
    expect(gw.classify_lane_type(None, "Edit bar.py: add a function baz()")=="edit", "edit intent")
    expect(gw.classify_lane_type(None, "Synthesize and merge these findings into one object")=="synthesize", "synth intent")
    print("PASS: lane classify")
if __name__=="__main__": test()
